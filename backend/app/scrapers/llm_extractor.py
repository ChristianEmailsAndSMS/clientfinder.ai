"""Claude-powered extractor. One prompt, works on any page HTML.

Dev mode (DEV_FIXTURES=1): a deterministic heuristic mock based on page title + snippet.
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
