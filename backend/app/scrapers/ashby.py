"""Ashby public job-board API: https://api.ashbyhq.com/posting-api/job-board/{board} (free, no key).

ASHBY_BOARDS is a seed list. UNVERIFIED slugs: run `python scripts/check_sources.py ashby` on the server
and prune the ones that 404. Slug = the part after jobs.ashbyhq.com/."""
from __future__ import annotations

import logging

from .common import CollectResult, NormalizedJob, employment_type, get, ingest, parse_dt, title_matches

log = logging.getLogger(__name__)

ASHBY_BOARDS: tuple[str, ...] = ("ashby", "linear", "ramp", "openai", "notion")
SOURCE_KEY = "ashby"


def parse(board: str, payload: dict) -> CollectResult:
    rows = payload.get("jobs") or []
    out = CollectResult(fetched=len(rows))
    for j in rows:
        title = j.get("title") or ""
        url = j.get("jobUrl") or j.get("applyUrl")
        if j.get("isListed") is False or not url or not title_matches(title):
            continue
        comp = (j.get("compensation") or {}).get("compensationTierSummary")
        location = j.get("location")
        out.jobs.append(NormalizedJob(
            url=url, title=title, company=board,
            description=(j.get("descriptionPlain") or "")[:4000] or None,
            type=employment_type(j.get("employmentType")),
            pay_text=comp or None, location=location,
            remote=True if j.get("isRemote") else (None if not location else "remote" in location.lower() or None),
            posted_at=parse_dt(j.get("publishedAt")),
        ))
    return out


def collect(boards: tuple[str, ...] = ASHBY_BOARDS) -> CollectResult:
    total = CollectResult()
    for b in boards:
        try:
            part = parse(b, get(f"https://api.ashbyhq.com/posting-api/job-board/{b}?includeCompensation=true").json())
        except Exception as e:
            total.errors.append(f"ashby/{b}: {e}")
            continue
        total.fetched += part.fetched
        total.jobs.extend(part.jobs)
    return total


def run() -> dict:
    return ingest(SOURCE_KEY, "Ashby (public boards)", "ashby", collect())
