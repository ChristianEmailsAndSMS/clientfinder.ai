"""Claude-powered extractor. One prompt, works on any page HTML.

Dev mode (DEV_FIXTURES=1): a deterministic heuristic mock based on page title + snippet.
Lets the pipeline run offline. Real mode calls Anthropic API."""
from __future__ import annotations

import json
import logging
import re
from datetime import datetime, timezone

from bs4 import BeautifulSoup

from ..config import settings
from ..schemas import ExtractedJob, SearchResult

log = logging.getLogger(__name__)

# Tokens, not str.format: the JSON example contains literal braces.
EXTRACTION_PROMPT = """You are a job-posting extractor. Read the HTML/text below and return STRICT JSON with the shape:
{
  "is_real_job": bool,          // true if this looks like an actual hiring post (not just a blog about hiring, not spam)
  "title": str,                 // the role title
  "company_or_poster": str|null,// company name OR the social handle who posted it
  "pay_text": str|null,         // raw pay string as written ("$50/hr", "$3K/project", "DOE")
  "pay_min": float|null,        // numeric lower bound in USD
  "pay_max": float|null,        // numeric upper bound in USD
  "pay_period": str|null,       // "hour" | "week" | "month" | "project" | "year" | null
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

Return JSON ONLY, no prose. Context source: <<SOURCE_HINT>>.

PAGE:
---
<<PAGE_TEXT>>
---"""

_MONEY_RE = re.compile(
    r"\$\s*(?P<a>\d[\d,]*(?:\.\d+)?)(?:(?P<a_suf>[kKmM])\b)?"
    r"(?:\s*[-–—]\s*\$?\s*(?P<b>\d[\d,]*(?:\.\d+)?)(?:(?P<b_suf>[kKmM])\b)?)?"
    r"(?P<unit>(?:\s*(?:/|per)\s*|\s+)(?P<unit_word>hr|hrs|hour|hours|yr|yrs|year|years|yearly|annually|annual|mo|mos|month|months|monthly|wk|wks|week|weeks|weekly|project|page|fixed)\b)?",
    re.IGNORECASE,
)

_UNIT_PERIOD = {
    "hr": "hour", "hrs": "hour", "hour": "hour", "hours": "hour",
    "yr": "year", "yrs": "year", "year": "year", "years": "year",
    "yearly": "year", "annually": "year", "annual": "year",
    "mo": "month", "mos": "month", "month": "month", "months": "month", "monthly": "month",
    "wk": "week", "wks": "week", "week": "week", "weeks": "week", "weekly": "week",
    "project": "project", "page": "project", "fixed": "project",
}

_NEGATIVE_NEAR = re.compile(
    r"\b(course|revenue|arr|mrr|shopify store|we run|worth)\b",
    re.IGNORECASE,
)
_PAY_CONTEXT = re.compile(r"\b(retainer|salary|compensation|budget|hourly|contract)\b", re.IGNORECASE)

_SKILLS = (
    ("copywriting", ("copywriting", "copywriter")),
    ("email", ("email",)),
    ("funnel", ("funnel",)),
    ("landing page", ("landing page",)),
    ("shopify", ("shopify",)),
    ("klaviyo", ("klaviyo",)),
    ("activecampaign", ("activecampaign",)),
    ("mailchimp", ("mailchimp",)),
    ("sms", ("sms",)),
    ("cro", ("cro",)),
    ("seo", ("seo",)),
    ("paid ads", ("paid ads",)),
)


def _html_to_text(html: str, max_chars: int = 12000) -> str:
    soup = BeautifulSoup(html or "", "html.parser")
    ld_bits: list[str] = []
    for tag in soup.find_all("script", attrs={"type": re.compile(r"ld\+json", re.I)}):
        raw = tag.get_text(" ", strip=True)
        if raw:
            ld_bits.append(raw)
        tag.decompose()
    for tag in soup(["script", "style", "noscript"]):
        tag.decompose()
    main = soup.find("main") or soup.find("article")
    body = main.get_text(" ", strip=True) if main else ""
    if len(body) < 200:
        body = soup.get_text(" ", strip=True)
    text = re.sub(r"\s+", " ", " ".join(ld_bits + [body])).strip()
    return text[:max_chars]


def _scale(num: str, suffix: str | None) -> float:
    value = float(num.replace(",", ""))
    if not suffix:
        return value
    suf = suffix.lower()
    if suf == "k":
        return value * 1000
    if suf == "m":
        return value * 1_000_000
    return value


def _parse_pay(text: str) -> tuple[str | None, float | None, float | None, str | None]:
    """Pick the compensation span, not the first dollar amount on the page."""
    best = None
    best_score = 0
    for match in _MONEY_RE.finditer(text or ""):
        suffix_for_low = match.group("a_suf")
        if match.group("b") and match.group("b_suf") and not match.group("a_suf"):
            suffix_for_low = match.group("b_suf")
        pay_min = _scale(match.group("a"), suffix_for_low)
        pay_max = _scale(match.group("b"), match.group("b_suf")) if match.group("b") else None
        unit_word = (match.group("unit_word") or "").lower()
        period = _UNIT_PERIOD.get(unit_word)
        if period is None and (match.group("a_suf") or match.group("b_suf")):
            period = "year"
        elif period is None and pay_min >= 10000 and (pay_max is None or pay_max >= 10000):
            period = "year"
        before = text[max(0, match.start() - 24): match.start()]
        after = text[match.end(): match.end() + 24]
        # A word sitting on an earlier amount ("$497 course") should not sink the next one.
        if "$" in before:
            before = ""
        score = 0
        if unit_word:
            score += 5
        if match.group("a_suf") or match.group("b_suf"):
            score += 2
        if pay_max is not None:
            score += 1
        if _NEGATIVE_NEAR.search(before) or _NEGATIVE_NEAR.search(after.split("$", 1)[0]):
            score -= 6
        if _PAY_CONTEXT.search(text[match.end(): match.end() + 32]):
            score += 4
        suffix = f"{match.group('a_suf') or ''}{match.group('b_suf') or ''}".lower()
        if "m" in suffix:
            score -= 3
        if not unit_word and not (match.group("a_suf") or match.group("b_suf")) and pay_min < 10000:
            score -= 2
        if score > best_score:
            best_score = score
            best = (match.group(0).strip(), pay_min, pay_max, period)
    if not best:
        return None, None, None, None
    return best


def _infer_remote(text: str) -> bool | None:
    if re.search(r"non[-\s]?remote|\bnot\s+remote\b", text):
        return False
    if re.search(r"\bremote\b", text):
        return True
    return None


def _infer_type(text: str, pay_period: str | None, platform: str) -> str | None:
    if re.search(r"\b(full[-\s]?time|fulltime)\b", text):
        return "full_time"
    if re.search(r"\b(freelance|contractor|contract)\b", text):
        return "contract"
    if re.search(r"\bfixed\b", text):
        return "fixed"
    if re.search(r"\bhourly\b", text) or pay_period == "hour":
        return "hourly"
    if platform in ("twitter", "reddit", "linkedin"):
        return "social_post"
    return None


def _heuristic_mock(result: SearchResult, page_html: str) -> ExtractedJob:
    """Deterministic fallback — infer whatever we can from title + snippet without an LLM."""
    title = result.title or "Unknown"
    snippet = result.snippet or ""
    page_text = _html_to_text(page_html) if page_html and "<" in page_html else (page_html or "")
    combined_raw = f"{title} {snippet} {page_text}"
    combined = combined_raw.lower()

    pay_text, pay_min, pay_max, pay_period = _parse_pay(combined_raw)
    typ = _infer_type(combined, pay_period, result.platform)
    remote = _infer_remote(combined)

    skills = []
    for label, needles in _SKILLS:
        if any(needle in combined for needle in needles):
            skills.append(label)

    return ExtractedJob(
        is_real_job=True,
        title=title[:512],
        company_or_poster=None,
        pay_text=pay_text[:128] if pay_text else None,
        pay_min=pay_min,
        pay_max=pay_max,
        pay_period=pay_period,
        type=typ,
        remote=remote,
        skills=skills[:6],
        apply_url=result.url,
        raw_snippet=(snippet or title)[:200],
        description=snippet or None,
        extraction_model="heuristic-mock",
    )


def _build_prompt(result: SearchResult, page_text: str) -> str:
    return (
        EXTRACTION_PROMPT
        .replace("<<SOURCE_HINT>>", result.platform or "web")
        .replace("<<PAGE_TEXT>>", page_text)
    )


def _loads_extraction(text: str) -> dict:
    raw = (text or "").strip()
    fenced = re.search(r"```(?:json)?\s*(\{.*\})\s*```", raw, flags=re.IGNORECASE | re.DOTALL)
    if fenced:
        raw = fenced.group(1)
    else:
        start = raw.find("{")
        end = raw.rfind("}")
        if start >= 0 and end > start:
            raw = raw[start:end + 1]
    data = json.loads(raw)
    if not isinstance(data, dict):
        raise ValueError("extractor returned non-object JSON")
    skills = data.get("skills")
    if not isinstance(skills, list):
        data["skills"] = []
    posted = data.get("posted_at")
    if isinstance(posted, str):
        try:
            parsed = datetime.fromisoformat(posted.replace("Z", "+00:00"))
        except ValueError:
            data["posted_at"] = None
        else:
            if parsed.tzinfo is None:
                parsed = parsed.replace(tzinfo=timezone.utc)
            data["posted_at"] = parsed
    elif posted is not None and not isinstance(posted, datetime):
        data["posted_at"] = None
    return data


def _claude_extract(result: SearchResult, page_html: str) -> ExtractedJob:
    import anthropic
    client = anthropic.Anthropic(api_key=settings.anthropic_api_key)
    page_text = _html_to_text(page_html)
    prompt = _build_prompt(result, page_text)
    resp = client.messages.create(
        model=settings.extraction_model,
        max_tokens=1024,
        messages=[{"role": "user", "content": prompt}],
    )
    data = _loads_extraction(resp.content[0].text)
    data.setdefault("apply_url", result.url)
    data["extraction_model"] = settings.extraction_model
    return ExtractedJob(**data)


def extract(result: SearchResult, page_html: str) -> ExtractedJob:
    if settings.dev_fixtures or not settings.anthropic_api_key:
        return _heuristic_mock(result, page_html)
    try:
        return _claude_extract(result, page_html)
    except Exception:
        log.exception("claude extraction failed for %s; using heuristic", result.url)
        return _heuristic_mock(result, page_html)
