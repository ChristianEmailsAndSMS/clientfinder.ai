"""Claude-powered extractor. One prompt, works on any page HTML.

Dev mode (DEV_FIXTURES=1): a deterministic heuristic mock based on the listing
title, snippet, and fetched page text.
Lets the pipeline run offline. Real mode calls Anthropic API."""
from __future__ import annotations

import json
import re
from bs4 import BeautifulSoup

from ..config import settings
from ..schemas import ExtractedJob, SearchResult

EXTRACTION_PROMPT = """You are a job-posting extractor. Read the HTML/text below and return STRICT JSON with the shape:
{
  "is_real_job": bool,          // true if this looks like an actual hiring post (not just a blog about hiring, not spam)
  "title": str,                 // the role title
  "company_or_poster": str|null,// company name OR the social handle who posted it
  "pay_text": str|null,         // raw pay string as written ("$50/hr", "$3K/project", "DOE")
  "pay_min": float|null,        // numeric lower bound in USD
  "pay_max": float|null,        // numeric upper bound in USD
  "pay_period": str|null,       // "hour" | "project" | "year" | null
  "type": str|null,             // "contract" | "full_time" | "hourly" | "fixed" | "social_post"
  "experience_level": str|null, // "entry" | "mid" | "senior" | null
  "location": str|null,
  "remote": bool|null,
  "skills": [str],              // 3-6 skills if detectable
  "apply_url": str|null,        // best link to apply (DM, email, form)
  "posted_at": str|null,        // ISO8601 if explicit, else null
  "raw_snippet": str,           // one-sentence summary (max 200 chars)
  "description": str|null       // 2-4 sentence summary
}

Return JSON ONLY, no prose. Context source: {source_hint}.

PAGE:
---
{page_text}
---"""


def _html_to_text(html: str, max_chars: int = 12000) -> str:
    soup = BeautifulSoup(html, "html.parser")
    for tag in soup(["script", "style", "noscript"]):
        tag.decompose()
    text = soup.get_text(" ", strip=True)
    text = re.sub(r"\s+", " ", text)
    return text[:max_chars]


# Thousands suffix is a bare "k" glued to the number ("$80k"). It must not swallow
# the next word ("$5000 keywords"), which is why no whitespace is allowed before it.
# A "k" on a range ("$120k-$150k", "$120-150k") scales both bounds. "month" is a
# recognized period in addition to hour, project, and year.
_RANGE_DASH = "[-\u2013\u2014]"
_PAY_RE = re.compile(
    r"\$(?P<min>\d[\d,]*)(?P<min_k>k\b)?"
    r"(?:\s*" + _RANGE_DASH + r"\s*\$?(?P<max>\d[\d,]*)(?P<max_k>k\b)?)?"
    r"(?:"
    r"\s*(?:/|per\s+)(?P<slash>hourly|hours?|hrs?|yearly|years?|yrs?|projects?|monthly|months?|mos?)\b"
    r"|"
    r"\s+(?P<word>projects?|yearly|years?|yrs?|hourly|hours?|hrs?|monthly|months?|mos?)\b"
    r")?",
    re.IGNORECASE,
)
_HOUR_UNITS = {"hour", "hours", "hr", "hrs", "hourly"}
_YEAR_UNITS = {"year", "years", "yr", "yrs", "yearly"}
_PROJECT_UNITS = {"project", "projects"}
_MONTH_UNITS = {"month", "months", "mo", "mos", "monthly"}


def _pay_period(unit: str, *, saw_k: bool) -> str | None:
    key = (unit or "").lower()
    if key in _HOUR_UNITS:
        return "hour"
    if key in _YEAR_UNITS:
        return "year"
    if key in _PROJECT_UNITS:
        return "project"
    if key in _MONTH_UNITS:
        return "month"
    if saw_k:
        return "year"
    return None


def _parse_pay(text: str) -> tuple[str | None, float | None, float | None, str | None]:
    """Return pay_text, pay_min, pay_max, pay_period from one blob of text."""
    if not text:
        return (None, None, None, None)
    match = _PAY_RE.search(text.lower())
    if not match:
        return (None, None, None, None)
    try:
        pay_min = float(match.group("min").replace(",", ""))
        raw_max = match.group("max")
        pay_max = float(raw_max.replace(",", "")) if raw_max else None
    except (TypeError, ValueError):
        return (None, None, None, None)
    min_k = bool(match.group("min_k"))
    max_k = bool(match.group("max_k"))
    if min_k:
        pay_min *= 1000
    if pay_max is not None and max_k:
        pay_max *= 1000
        # "$120-150k": the trailing k applies to the shorthand lower bound too.
        if not min_k and pay_min < 1000:
            pay_min *= 1000
    unit = match.group("slash") or match.group("word") or ""
    return (match.group(0), pay_min, pay_max, _pay_period(unit, saw_k=min_k or max_k))


def _heuristic_mock(result: SearchResult, page_text: str) -> ExtractedJob:
    """Deterministic fallback — infer whatever we can from title, snippet, and page text."""
    title = result.title or "Unknown"
    snippet = result.snippet or ""
    page = _html_to_text(page_text or "")
    listing = f"{title} {snippet}"
    # Listing text wins when it already has a pay string so a noisier page
    # cannot replace it or get added on top of it. Page text is the fallback.
    combined = f"{listing} {page}".lower()

    pay_text, pay_min, pay_max, pay_period = _parse_pay(listing)
    if pay_min is None:
        pay_text, pay_min, pay_max, pay_period = _parse_pay(page)

    # Type inference
    typ = None
    if "contract" in combined: typ = "contract"
    elif "full-time" in combined or "full time" in combined: typ = "full_time"
    elif "hourly" in combined or pay_period == "hour": typ = "hourly"
    elif result.platform in ("twitter", "reddit", "linkedin"): typ = "social_post"

    # Remote
    remote = True if "remote" in combined else None

    # Skills — just pattern-match a few
    skills = []
    for kw in ("copywriting", "email", "funnel", "landing page", "shopify", "klaviyo",
              "activecampaign", "mailchimp", "sms", "cro", "seo", "paid ads"):
        if kw in combined:
            skills.append(kw)

    return ExtractedJob(
        is_real_job=True,
        title=title[:512],
        company_or_poster=None,
        pay_text=pay_text,
        pay_min=pay_min,
        pay_max=pay_max,
        pay_period=pay_period,
        type=typ,
        remote=remote,
        skills=skills[:6],
        apply_url=result.url,
        raw_snippet=(snippet or title)[:200],
        description=snippet or None,
    )


def _claude_extract(result: SearchResult, page_html: str) -> ExtractedJob:
    import anthropic
    client = anthropic.Anthropic(api_key=settings.anthropic_api_key)
    page_text = _html_to_text(page_html)
    prompt = EXTRACTION_PROMPT.format(source_hint=result.platform, page_text=page_text)
    resp = client.messages.create(
        model=settings.extraction_model,
        max_tokens=1024,
        messages=[{"role": "user", "content": prompt}],
    )
    text = resp.content[0].text.strip()
    # Trim code fences if the model added them
    if text.startswith("```"):
        text = re.sub(r"^```(?:json)?|```$", "", text, flags=re.MULTILINE).strip()
    data = json.loads(text)
    data.setdefault("apply_url", result.url)
    return ExtractedJob(**data)


def extract(result: SearchResult, page_html: str) -> ExtractedJob:
    if settings.dev_fixtures or not settings.anthropic_api_key:
        return _heuristic_mock(result, page_html)
    try:
        return _claude_extract(result, page_html)
    except Exception:
        # Degrade to heuristic rather than losing the row.
        return _heuristic_mock(result, page_html)
