"""Direct ingester for Greenhouse-powered job boards.
Greenhouse exposes a public JSON endpoint: https://boards-api.greenhouse.io/v1/boards/{company}/jobs
Each tracked company becomes a feed. Filter titles to copy/email/marketing keywords.
"""
from __future__ import annotations

import logging
import re
from datetime import datetime, timezone

import httpx
from bs4 import BeautifulSoup
from sqlalchemy import select
from sqlalchemy.exc import DataError, IntegrityError

from ..db import session_scope
from ..models import Job, Source, ScrapeRun
from ..dedup import dedupe_hash

log = logging.getLogger(__name__)

# Seed list of boards known to have a public Greenhouse endpoint.
# Verified against boards-api.greenhouse.io at scaffold time (2026-10-07).
# Add/remove freely — fetches degrade gracefully (per-company try/except).
GREENHOUSE_COMPANIES: tuple[str, ...] = (
    "stripe",
    "airtable",
    "figma",
    "anthropic",
    "glossier",
    # Add more real board slugs as you find them. Many companies no longer host on Greenhouse directly.
)

# Phrases, not bare "email" / "lifecycle" — those match deliverability and product roles.
TITLE_PHRASES = (
    "copywriter", "copywriting", "email marketing", "email copy",
    "lifecycle marketing", "landing page", "funnel",
    "creative strategist", "content marketing", "content writer",
    "growth marketing", "crm manager",
)


def _matches_title(title: str) -> bool:
    t = (title or "").lower()
    if any(phrase in t for phrase in TITLE_PHRASES):
        return True
    return bool(re.search(r"\bcrm\b", t))


def _location_name(location) -> str | None:
    if isinstance(location, dict):
        name = location.get("name")
    elif isinstance(location, str):
        name = location
    else:
        name = None
    if not name:
        return None
    return str(name).strip()[:128] or None


def _is_remote(location: str | None) -> bool | None:
    if not location:
        return None
    text = location.lower()
    if re.search(r"non[-\s]?remote|\bnot\s+remote\b", text):
        return False
    if re.search(r"\bremote\b", text):
        return True
    return False


def _plain(html: str | None) -> str | None:
    if not html:
        return None
    text = BeautifulSoup(html, "html.parser").get_text(" ", strip=True)
    return text[:4000] or None


def fetch_board(company: str) -> list[dict]:
    url = f"https://boards-api.greenhouse.io/v1/boards/{company}/jobs?content=true"
    r = httpx.get(url, timeout=30)
    r.raise_for_status()
    data = r.json()
    return data.get("jobs", [])


def run() -> dict:
    stats = {"source": "greenhouse", "fetched": 0, "matched": 0, "added": 0, "updated": 0, "errors": 0}
    all_jobs: list[tuple[str, dict]] = []
    company_errors = 0
    for company in GREENHOUSE_COMPANIES:
        try:
            for j in fetch_board(company):
                all_jobs.append((company, j))
        except Exception as e:
            log.warning("greenhouse fetch failed for %s: %s", company, e)
            company_errors += 1
            stats["errors"] += 1

    stats["fetched"] = len(all_jobs)
    with session_scope() as db:
        src = db.scalar(select(Source).where(Source.key == "greenhouse"))
        if not src:
            src = Source(key="greenhouse", kind="direct_api", display_name="Greenhouse (public boards)", config={})
            db.add(src); db.flush()

        run = ScrapeRun(source_id=src.id, status="running")
        db.add(run); db.flush()
        now = datetime.now(timezone.utc)

        seen_in_batch: set[str] = set()
        for company, j in all_jobs:
            title = j.get("title", "")
            if not _matches_title(title):
                continue
            stats["matched"] += 1
            url = j.get("absolute_url")
            if not url:
                continue
            h = dedupe_hash(url=url, title=title, company=company)
            if h in seen_in_batch:
                continue
            seen_in_batch.add(h)

            existing = db.scalar(select(Job).where(Job.dedupe_hash == h))
            if existing:
                existing.last_seen_at = now
                stats["updated"] += 1
                continue

            location = _location_name(j.get("location"))
            # updated_at is the last edit, not the publish date. Keep it off posted_at.
            extra = None
            if j.get("updated_at"):
                extra = {"updated_at": j["updated_at"]}

            sp = db.begin_nested()
            try:
                job = Job(
                    dedupe_hash=h,
                    source_url=url,
                    platform="greenhouse",
                    title=title[:512],
                    company_or_poster=company[:256],
                    raw_snippet=title[:200],
                    description=_plain(j.get("content")),
                    type="full_time",
                    location=location,
                    remote=_is_remote(location),
                    posted_at=None,
                    first_seen_at=now, last_seen_at=now,
                    source_key="greenhouse",
                    is_real_job=True,
                    extraction_model="direct_api",
                    extra=extra,
                )
                db.add(job)
                db.flush()
                sp.commit()
                stats["added"] += 1
            except (IntegrityError, DataError):
                sp.rollback()
                stats["updated"] += 1
                log.debug("race or invalid row for hash %s", h)
            except Exception:
                sp.rollback()
                log.exception("greenhouse row failed: %s", url)
                stats["row_errors"] = stats.get("row_errors", 0) + 1

        run.finished_at = datetime.now(timezone.utc)
        # A single board 404 should not mark a run that inserted jobs as failed.
        boards_all_failed = company_errors >= len(GREENHOUSE_COMPANIES) and stats["added"] == 0
        run.status = "failed" if boards_all_failed else "ok"
        run.jobs_added = stats["added"]
        run.jobs_updated = stats["updated"]
        if stats["errors"] or stats.get("row_errors"):
            run.error = f"{stats['errors']} companies failed, {stats.get('row_errors', 0)} rows failed"
    return stats
