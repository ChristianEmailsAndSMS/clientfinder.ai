"""Direct ingester for Greenhouse-powered job boards.
Greenhouse exposes a public JSON endpoint: https://boards-api.greenhouse.io/v1/boards/{company}/jobs
Each tracked company becomes a feed. Filter titles to copy/email/marketing keywords.
"""
from __future__ import annotations

import logging
from datetime import datetime, timezone

import httpx
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError

from ..db import session_scope
from ..models import Job, Source, ScrapeRun
from ..dedup import dedupe_hash
from ..security_utils import safe_job_url
from ..tagging import set_tags

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

TITLE_KEYWORDS = (
    "copywriter", "copywriting", "email", "lifecycle", "landing page",
    "creative strategist", "content marketing", "growth marketing", "crm manager",
)


def _matches_title(title: str) -> bool:
    t = (title or "").lower()
    return any(kw in t for kw in TITLE_KEYWORDS)


def fetch_board(company: str) -> list[dict]:
    url = f"https://boards-api.greenhouse.io/v1/boards/{company}/jobs?content=true"
    r = httpx.get(url, timeout=30)
    r.raise_for_status()
    data = r.json()
    return data.get("jobs", [])


def run() -> dict:
    stats = {"source": "greenhouse", "fetched": 0, "matched": 0, "added": 0, "updated": 0, "errors": 0}
    all_jobs: list[tuple[str, dict]] = []
    for company in GREENHOUSE_COMPANIES:
        try:
            for j in fetch_board(company):
                all_jobs.append((company, j))
        except Exception as e:
            log.warning("greenhouse fetch failed for %s: %s", company, e)
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
            if not safe_job_url(url):
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

            location = (j.get("location") or {}).get("name")
            posted_at = None
            if j.get("updated_at"):
                try: posted_at = datetime.fromisoformat(j["updated_at"].replace("Z", "+00:00"))
                except Exception: pass

            job = Job(
                dedupe_hash=h,
                source_url=url,
                platform="greenhouse",
                title=title[:512],
                company_or_poster=company,
                raw_snippet=title[:200],
                description=(j.get("content") or "")[:4000] or None,
                type="full_time",
                location=location,
                remote=("remote" in (location or "").lower()) if location else None,
                posted_at=posted_at,
                first_seen_at=now, last_seen_at=now,
                source_key="greenhouse",
                is_real_job=True,
                extraction_model="direct_api",
            )
            # Savepoint so one unique-violation doesn't kill the batch
            sp = db.begin_nested()
            try:
                db.add(job)
                db.flush()
                set_tags(db, job)
                sp.commit()
                stats["added"] += 1
            except IntegrityError:
                sp.rollback()
                stats["updated"] += 1  # someone else inserted this hash since our SELECT
                log.debug("race: hash %s already present, skipping", h)

        run.finished_at = now
        run.status = "ok" if stats["errors"] == 0 else "failed"
        run.jobs_added = stats["added"]
        run.jobs_updated = stats["updated"]
        if stats["errors"]:
            run.error = f"{stats['errors']} companies failed to fetch"
    return stats
