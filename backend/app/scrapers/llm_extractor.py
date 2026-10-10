"""Claude-powered extractor. One prompt, works on any page HTML.

Dev mode (DEV_FIXTURES=1): a deterministic heuristic mock based on page title + snippet.
Lets the pipeline run offline. Real mode calls Anthropic API."""
from __future__ import annotations

import json
import logging
import re
from datetime import datetime

from bs4 import BeautifulSoup

from ..config import settings
from ..schemas import ExtractedJob, SearchResult

log = logging.getLogger(__name__)

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

# k/m/b only count when glued to the number ("$120k", not "$50 kitchen").
_PAY_RE = re.compile(
    r"\$\s*([\d,]+(?:\.\d+)?)\s*([kmb])?"
    r"(?:\s*[-–—]\s*\$?\s*([\d,]+(?:\.\d+)?)\s*([kmb])?)?"
    r"(?:\s*(?:/|per)\s*|\s+)?"
    r"(hr|hrs|hour|hours|yr|year|years|mo|month|months|project|page|fixed)?\b",
    re.IGNORECASE,
)
_MULT = {"k": 1_000, "m": 1_000_000, "b": 1_000_000_000}
_PERIOD = {
    "hr": "hour", "hrs": "hour", "hour": "hour", "hours": "hour",
    "yr": "year", "year": "year", "years": "year",
    "mo": "month", "month": "month", "months": "month",
    "project": "project", "page": "project", "fixed": "project",
}


def build_extraction_prompt(source_hint: str, page_text: str) -> str:
    """Substitute the two placeholders without str.format.

    The example JSON uses braces, and page text can too. format() raises KeyError
    on those and the live Claude path never runs.
    """
    return (
        EXTRACTION_PROMPT
        .replace("{source_hint}", source_hint)
        .replace("{page_text}", page_text)
    )


def _html_to_text(html: str, max_chars: int = 12000) -> str:
    soup = BeautifulSoup(html, "html.parser")
    for tag in soup(["script", "style", "noscript"]):
        tag.decompose()
    text = soup.get_text(" ", strip=True)
    text = re.sub(r"\s+", " ", text)
    return text[:max_chars]


def _money(raw: str, suffix: str | None) -> float:
    n = float(raw.replace(",", ""))
    if suffix:
        n *= _MULT[suffix.lower()]
    return n


def infer_pay(text: str) -> tuple[str | None, float | None, float | None, str | None]:
    """Return pay_text, pay_min, pay_max, pay_period from a title + snippet."""
    matches = list(_PAY_RE.finditer(text or ""))
    if not matches:
        return None, None, None, None

    def rank(m: re.Match) -> tuple[int, int]:
        # A match that names a unit beats an earlier bare dollar amount.
        return (1 if m.group(5) else 0, -m.start())

    m = max(matches, key=rank)
    try:
        pay_min = _money(m.group(1), m.group(2))
        pay_max = _money(m.group(3), m.group(4)) if m.group(3) else None
    except Exception:
        return None, None, None, None

    lo_suf = (m.group(2) or "").lower()
    hi_suf = (m.group(4) or "").lower()
    # "$120-150k" applies the suffix to both sides.
    if pay_max is not None:
        if hi_suf and not lo_suf and pay_min < 1000:
            pay_min *= _MULT[hi_suf]
        elif lo_suf and not hi_suf and pay_max < 1000:
            pay_max *= _MULT[lo_suf]

    unit = (m.group(5) or "").lower()
    period = _PERIOD.get(unit)
    if period is None and (lo_suf in _MULT or hi_suf in _MULT):
        period = "year"
    return m.group(0).strip(), pay_min, pay_max, period


def _heuristic_mock(result: SearchResult, page_text: str) -> ExtractedJob:
    """Deterministic fallback — infer whatever we can from title + snippet without an LLM."""
    title = result.title or "Unknown"
    snippet = result.snippet or ""
    combined = f"{title} {snippet}".lower()
    pay_text, pay_min, pay_max, pay_period = infer_pay(combined)

    typ = None
    if "contract" in combined: typ = "contract"
    elif "full-time" in combined or "full time" in combined: typ = "full_time"
    elif "hourly" in combined or pay_period == "hour": typ = "hourly"
    elif result.platform in ("twitter", "reddit", "linkedin"): typ = "social_post"

    remote = True if "remote" in combined else None

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


def parse_model_payload(text: str, fallback_url: str) -> ExtractedJob:
    """Pull the JSON object out of a model response and coerce the loose fields."""
    raw = (text or "").strip()
    fenced = re.search(r"```(?:json)?\s*(.*?)```", raw, flags=re.IGNORECASE | re.DOTALL)
    if fenced:
        raw = fenced.group(1).strip()
    start, end = raw.find("{"), raw.rfind("}")
    if start < 0 or end < start:
        raise ValueError("extractor returned no JSON object")
    data = json.loads(raw[start:end + 1])
    if not isinstance(data, dict):
        raise ValueError("extractor JSON was not an object")

    if "is_real_job" not in data:
        data["is_real_job"] = False
    skills = data.get("skills") or []
    if not isinstance(skills, list):
        skills = []
    data["skills"] = [str(item) for item in skills if item]
    posted = data.get("posted_at")
    if isinstance(posted, str):
        try:
            datetime.fromisoformat(posted.replace("Z", "+00:00"))
        except ValueError:
            data["posted_at"] = None
    if not data.get("apply_url"):
        data["apply_url"] = fallback_url
    return ExtractedJob(**data)


def _claude_extract(result: SearchResult, page_html: str) -> ExtractedJob:
    import anthropic
    client = anthropic.Anthropic(api_key=settings.anthropic_api_key)
    page_text = _html_to_text(page_html)
    prompt = build_extraction_prompt(result.platform, page_text)
    resp = client.messages.create(
        model=settings.extraction_model,
        max_tokens=1024,
        messages=[{"role": "user", "content": prompt}],
    )
    text = resp.content[0].text.strip()
    return parse_model_payload(text, result.url)


def extract(result: SearchResult, page_html: str) -> ExtractedJob:
    if settings.dev_fixtures or not settings.anthropic_api_key:
        return _heuristic_mock(result, page_html)
    try:
        return _claude_extract(result, page_html)
    except Exception:
        log.exception("claude extract failed for %s", result.url)
        raise
