"""Claude-powered extractor. One prompt, works on any page HTML.

Dev mode (DEV_FIXTURES=1): a deterministic heuristic mock based on page title + snippet.
Lets the pipeline run offline. Real mode calls Anthropic API."""
from __future__ import annotations

import json
import re
from bs4 import BeautifulSoup

from ..config import settings
from ..schemas import ExtractedJob, SearchResult

# Built by concatenation so page text and the JSON example can contain `{` / `}`.
# str.format() treated those braces as placeholders and raised KeyError before Claude ran.
_PROMPT_INTRO = """You are a job-posting extractor. Read the HTML/text below and return STRICT JSON with the shape:
{
  "is_real_job": bool,
  "title": str,
  "company_or_poster": str|null,
  "pay_text": str|null,
  "pay_min": float|null,
  "pay_max": float|null,
  "pay_period": str|null,
  "type": str|null,
  "experience_level": str|null,
  "location": str|null,
  "remote": bool|null,
  "skills": [str],
  "apply_url": str|null,
  "posted_at": str|null,
  "raw_snippet": str,
  "description": str|null
}

Return JSON ONLY, no prose. Context source: """

_PAY_RE = re.compile(
    r"\$([\d,]+)(?:\s*[-–]\s*\$?([\d,]+))?(?:\s*(k))?(?:\s*(?:/|per)\s*(hr|hour|mo|month|yr|year|project)\b)?",
    re.IGNORECASE,
)


def build_extraction_prompt(source_hint: str, page_text: str) -> str:
    return (
        _PROMPT_INTRO
        + (source_hint or "web")
        + ".\n\nPAGE:\n---\n"
        + page_text
        + "\n---"
    )


def _html_to_text(html: str, max_chars: int = 12000) -> str:
    soup = BeautifulSoup(html, "html.parser")
    for tag in soup(["script", "style", "noscript"]):
        tag.decompose()
    text = soup.get_text(" ", strip=True)
    text = re.sub(r"\s+", " ", text)
    return text[:max_chars]


def _page_blob(page_text: str) -> str:
    text = page_text or ""
    if "<" in text and ">" in text:
        text = _html_to_text(text, max_chars=8000)
    return text


def _parse_pay(combined: str) -> tuple[str | None, float | None, float | None, str | None]:
    """Return pay_text, pay_min, pay_max, pay_period.

    The unit is only consumed when it is a real period (hr, year, project, ...).
    A bare slash or the word "per" is not enough, so "$3,000/mo" stays intact
    and "$800-1,500 per page" does not swallow the word "per".
    """
    m = _PAY_RE.search(combined)
    if not m:
        return None, None, None, None
    pay_text = m.group(0)
    pay_min = pay_max = None
    pay_period = None
    try:
        pay_min = float(m.group(1).replace(",", ""))
        if m.group(2):
            pay_max = float(m.group(2).replace(",", ""))
        unit = (m.group(4) or "").lower()
        if m.group(3):
            pay_min *= 1000
            if pay_max:
                pay_max *= 1000
            pay_period = "year"
        if unit in ("hr", "hour"):
            pay_period = "hour"
        elif unit in ("yr", "year"):
            pay_period = "year"
        elif unit == "project":
            pay_period = "project"
    except Exception:
        return pay_text, None, None, None
    return pay_text, pay_min, pay_max, pay_period


def _heuristic_mock(result: SearchResult, page_text: str) -> ExtractedJob:
    """Deterministic fallback — infer whatever we can from title, snippet, and page text."""
    title = result.title or "Unknown"
    snippet = result.snippet or ""
    combined = f"{title} {snippet} {_page_blob(page_text)}".lower()

    pay_text, pay_min, pay_max, pay_period = _parse_pay(combined)

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
    prompt = build_extraction_prompt(result.platform, page_text)
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
    if not isinstance(data, dict):
        raise ValueError("extractor returned non-object JSON")
    skills = data.get("skills")
    if not isinstance(skills, list):
        data["skills"] = []
    else:
        data["skills"] = [str(item) for item in skills if item is not None and not isinstance(item, (dict, list))]
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
