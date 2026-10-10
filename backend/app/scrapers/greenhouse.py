"""Direct ingester for Greenhouse-powered job boards.
Greenhouse exposes a public JSON endpoint: https://boards-api.greenhouse.io/v1/boards/{company}/jobs
Each tracked company becomes a feed. Filter titles to copy/email/marketing keywords.
"""
from __future__ import annotations

import html
import logging
import re
from datetime import datetime, timezone

import httpx
from bs4 import BeautifulSoup
from sqlalchemy import select

from ..db import add_ignoring_conflict, session_scope
from ..dedup import dedupe_hash
from ..models import Job, ScrapeRun, Source

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

# Phrases, not bare "email" / "lifecycle" — those match infrastructure roles.
TITLE_KEYWORDS = (
    "copywriter", "copywriting", "email marketing", "email copywriter", "email copy",
    "lifecycle marketing", "landing page", "creative strategist", "content marketing",
    "growth marketing", "crm manager",
)


def _matches_title(title: str) -> bool:
    t = (title or "").lower()
    return any(kw in t for kw in TITLE_KEYWORDS)


def plain_content(content: str | None, limit: int = 4000) -> str | None:
    if not content:
        return None
    text = BeautifulSoup(html.unescape(content), "html.parser").get_text(" ", strip=True)
    text = re.sub(r"\s+", " ", text).strip()
    return text[:limit] or None


def _parse_dt(value) -> datetime | None:
    if not value or not isinstance(value, str):
        return None
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00"))
    except Exception:
        return None


def posted_at_of(job: dict) -> datetime | None:
    return _parse_dt(job.get("first_published")) or _parse_dt(job.get("updated_at"))


def _meta_value(item: dict) -> str:
    val = item.get("value")
    if isinstance(val, dict):
        val = val.get("label") or val.get("name") or ""
    return str(val or "").strip().lower()


def is_remote(job: dict, location: str | None) -> bool | None:
    for item in job.get("metadata") or []:
        if not isinstance(item, dict):
            continue
        if str(item.get("name") or "").strip().lower() != "location type":
            continue
        val = _meta_value(item)
        if val == "remote":
            return True
        if val in {"on-site", "onsite", "hybrid", "in-office"}:
            return False
    if not location:
        return None
    loc = location.lower()
    if "remote-friendly" in loc or "remote friendly" in loc:
        return None
    if re.search(r"\bremote\b", loc):
        return True
    return None


def fetch_board(company: str) -> list[dict]:
    url = f"https://boards-api.greenhouse.io/v1/boards/{company}/jobs?content=true"
    r = httpx.get(url, timeout=30)
    r.raise_for_status()
    data = r.json()
    return data.get("jobs", [])


def _ensure_source(db) -> Source:
    src = db.scalar(select(Source).where(Source.key == "greenhouse"))
    if not src:
        src = Source(key="greenhouse", kind="direct_api", display_name="Greenhouse (public boards)", config={})
        db.add(src)
        db.flush()
    return src


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
        src = _ensure_source(db)
        run = ScrapeRun(source_id=src.id, status="running")
        db.add(run)
        db.flush()
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
            company_name = (j.get("company_name") or company or "")[:256]
            h = dedupe_hash(url=url, title=title, company=company_name)
            if h in seen_in_batch:
                continue
            seen_in_batch.add(h)

            existing = db.scalar(select(Job).where(Job.dedupe_hash == h))
            if existing:
                existing.last_seen_at = now
                stats["updated"] += 1
                continue

            location = (j.get("location") or {}).get("name")
            if isinstance(location, str):
                location = location[:128]
            else:
                location = None
            description = plain_content(j.get("content"))
            job = Job(
                dedupe_hash=h,
                source_url=url,
                platform="greenhouse",
                title=title[:512],
                company_or_poster=company_name or None,
                raw_snippet=(description or title)[:200],
                description=description,
                type="full_time",
                location=location,
                remote=is_remote(j, location),
                posted_at=posted_at_of(j),
                first_seen_at=now, last_seen_at=now,
                source_key="greenhouse",
                is_real_job=True,
                extraction_model="direct_api",
            )
            if add_ignoring_conflict(db, job):
                stats["added"] += 1
            else:
                existing = db.scalar(select(Job).where(Job.dedupe_hash == h))
                if existing:
                    existing.last_seen_at = now
                stats["updated"] += 1
                log.debug("race: hash %s already present, skipping", h)

        run.finished_at = datetime.now(timezone.utc)
        run.status = "ok" if stats["errors"] == 0 else "failed"
        run.jobs_added = stats["added"]
        run.jobs_updated = stats["updated"]
        if stats["errors"]:
            run.error = f"{stats['errors']} companies failed to fetch"
    return stats
