"""Remotive public API: https://remotive.com/api/remote-jobs (free, no key).

Remotive's API terms (verify at https://github.com/remotive-com/remote-jobs-api): fetch at most a few
times a day, and link back to the Remotive URL (we store their `url` as source_url). Their jobs also
appear ~24h late. At the default 6h scheduler interval this is 4 requests/day.
One request for all categories; we filter by title."""
from __future__ import annotations

import logging

from .common import CollectResult, NormalizedJob, employment_type, get, html_to_text, ingest, parse_dt, title_matches

log = logging.getLogger(__name__)

API_URL = "https://remotive.com/api/remote-jobs"
SOURCE_KEY = "remotive"


def parse(payload: dict) -> CollectResult:
    rows = payload.get("jobs") or []
    out = CollectResult(fetched=len(rows))
    for r in rows:
        title = r.get("title") or ""
        if not r.get("url") or not title_matches(title):
            continue
        out.jobs.append(NormalizedJob(
            url=r["url"], title=title, company=r.get("company_name"),
            description=html_to_text(r.get("description")),
            type=employment_type(r.get("job_type")),
            pay_text=(r.get("salary") or None),
            location=r.get("candidate_required_location") or None,
            remote=True, skills=list(r.get("tags") or [])[:6],
            posted_at=parse_dt(r.get("publication_date")),
        ))
    return out


def collect() -> CollectResult:
    try:
        return parse(get(API_URL).json())
    except Exception as e:
        return CollectResult(errors=[f"remotive: {e}"])


def run() -> dict:
    return ingest(SOURCE_KEY, "Remotive API", "remotive", collect())
