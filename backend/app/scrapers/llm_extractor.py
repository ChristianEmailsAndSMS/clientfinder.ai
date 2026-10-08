"""Claude-powered extractor. One prompt, works on any page HTML.

Dev mode (DEV_FIXTURES=1): a deterministic heuristic mock based on page title + snippet.
Lets the pipeline run offline. Real mode calls Anthropic API."""
from __future__ import annotations

import json
import re
from bs4 import BeautifulSoup

import logging

from pydantic import ValidationError

from ..config import settings
from ..schemas import ExtractedJob, SearchResult

log = logging.getLogger(__name__)


class ExtractionError(Exception):
    """The page could not be turned into a job. Callers skip the row; they must not store a guess."""

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


def _heuristic_mock(result: SearchResult, page_text: str) -> ExtractedJob:
    """Deterministic fallback — infer whatever we can from title + snippet without an LLM."""
    title = result.title or "Unknown"
    snippet = result.snippet or ""
    combined = f"{title} {snippet}".lower()

    # Pay inference — look for $X or $X-$Y /hr /year /project
    pay_text = None
    pay_min = pay_max = None
    pay_period = None
    m = re.search(r"\$([\d,]+)(?:\s*[-–]\s*\$?([\d,]+))?\s*(?:/|per\s+)?(hr|hour|yr|year|project|k)?", combined)
    if m:
        pay_text = m.group(0)
        try:
            pay_min = float(m.group(1).replace(",", ""))
            if m.group(2):
                pay_max = float(m.group(2).replace(",", ""))
            unit = (m.group(3) or "").lower()
            if "hr" in unit or "hour" in unit: pay_period = "hour"
            elif "yr" in unit or "year" in unit: pay_period = "year"
            elif "project" in unit: pay_period = "project"
            elif unit == "k":
                # "$80k" convention
                pay_min *= 1000
                if pay_max: pay_max *= 1000
                pay_period = "year"
        except Exception:
            pass

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


def _call_model(prompt: str) -> tuple[str, dict]:
    """One Claude call. Returns (text, usage). Split out so tests can stub it."""
    import anthropic
    client = anthropic.Anthropic(api_key=settings.anthropic_api_key)
    resp = client.messages.create(
        model=settings.extraction_model,
        max_tokens=1024,
        messages=[{"role": "user", "content": prompt}],
    )
    usage = {
        "model": settings.extraction_model,
        "input_tokens": resp.usage.input_tokens,
        "output_tokens": resp.usage.output_tokens,
    }
    return resp.content[0].text, usage


def _claude_extract(result: SearchResult, page_html: str) -> ExtractedJob:
    page_text = _html_to_text(page_html)
    if not page_text:
        raise ExtractionError("page has no text")
    # Not str.format(): the prompt contains literal JSON braces.
    prompt = EXTRACTION_PROMPT.replace("{source_hint}", result.platform).replace("{page_text}", page_text)
    try:
        text, usage = _call_model(prompt)
    except Exception as e:
        raise ExtractionError(f"model call failed: {e}") from e
    log.info("extract %s tokens_in=%s tokens_out=%s", result.url, usage["input_tokens"], usage["output_tokens"])
    text = text.strip()
    # Trim code fences if the model added them
    if text.startswith("```"):
        text = re.sub(r"^```(?:json)?|```$", "", text, flags=re.MULTILINE).strip()
    try:
        data = json.loads(text)
        if not isinstance(data, dict):
            raise TypeError(f"expected a JSON object, got {type(data).__name__}")
        data.setdefault("apply_url", result.url)
        job = ExtractedJob(**data)
    except (json.JSONDecodeError, TypeError, ValidationError) as e:
        raise ExtractionError(f"unparseable model output: {e}") from e
    if job.is_real_job and not job.title.strip():
        raise ExtractionError("real job without a title")
    job.usage = usage
    return job


def extract(result: SearchResult, page_html: str) -> ExtractedJob:
    """Dev mode: deterministic mock. Live mode: Claude, or ExtractionError (never a guess)."""
    if settings.dev_fixtures:
        return _heuristic_mock(result, page_html)
    if not settings.anthropic_api_key:
        raise ExtractionError("ANTHROPIC_API_KEY not set")
    return _claude_extract(result, page_html)
