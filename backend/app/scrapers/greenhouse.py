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
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

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

TITLE_KEYWORDS = (
    "copywriter", "copywriting", "email", "lifecycle", "landing page",
    "creative strategist", "content marketing", "growth marketing", "crm manager",
)

_WS = re.compile(r"\s+")


def _matches_title(title: str) -> bool:
    t = (title or "").lower()
    return any(kw in t for kw in TITLE_KEYWORDS)


def _clip(value, limit: int) -> str | None:
    if value is None:
        return None
    text = value if isinstance(value, str) else str(value)
    return text[:limit]


def _plain_description(content) -> str | None:
    """Strip Greenhouse HTML to collapsed text. Empty content stays None."""
    if content is None:
        return None
    raw = content if isinstance(content, str) else str(content)
    if not raw.strip():
        return None
    text = BeautifulSoup(raw, "html.parser").get_text(" ", strip=True)
    text = _WS.sub(" ", text).strip()
    if not text:
        return None
    return text[:4000]


def _ensure_source(db: Session) -> Source:
    src = db.scalar(select(Source).where(Source.key == "greenhouse"))
    if not src:
        src = Source(
            key="greenhouse",
            kind="direct_api",
            display_name="Greenhouse (public boards)",
            config={},
        )
        db.add(src)
        db.flush()
    return src


def fetch_board(company: str) -> list[dict]:
    url = f"https://boards-api.greenhouse.io/v1/boards/{company}/jobs?content=true"
    r = httpx.get(url, timeout=30)
    r.raise_for_status()
    data = r.json()
    return data.get("jobs", [])


def run() -> dict:
    stats = {"source": "greenhouse", "fetched": 0, "matched": 0, "added": 0, "updated": 0, "errors": 0}
    fetch_errors = 0
    all_jobs: list[tuple[str, dict]] = []
    for company in GREENHOUSE_COMPANIES:
        try:
            for j in fetch_board(company):
                all_jobs.append((company, j))
        except Exception as e:
            log.warning("greenhouse fetch failed for %s: %s", company, e)
            fetch_errors += 1
            stats["errors"] += 1

    stats["fetched"] = len(all_jobs)
    with session_scope() as db:
        src = _ensure_source(db)

        scrape = ScrapeRun(source_id=src.id, status="running")
        db.add(scrape)
        db.flush()
        now = datetime.now(timezone.utc)

        seen_in_batch: set[str] = set()
        row_errors = 0
        for company, j in all_jobs:
            title = j.get("title", "") or ""
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

            location = (j.get("location") or {}).get("name")
            posted_at = None
            if j.get("updated_at"):
                try:
                    posted_at = datetime.fromisoformat(j["updated_at"].replace("Z", "+00:00"))
                except Exception:
                    pass

            job = Job(
                dedupe_hash=h,
                source_url=url,
                platform="greenhouse",
                title=_clip(title, 512) or "",
                company_or_poster=_clip(company, 256),
                raw_snippet=_clip(title, 200),
                description=_plain_description(j.get("content")),
                type="full_time",
                location=_clip(location, 128),
                remote=("remote" in location.lower()) if isinstance(location, str) and location else None,
                posted_at=posted_at,
                first_seen_at=now,
                last_seen_at=now,
                source_key="greenhouse",
                is_real_job=True,
                extraction_model="direct_api",
            )
            # Savepoint so one bad row doesn't kill the batch.
            sp = db.begin_nested()
            try:
                db.add(job)
                db.flush()
                sp.commit()
                stats["added"] += 1
            except IntegrityError:
                sp.rollback()
                stats["updated"] += 1  # someone else inserted this hash since our SELECT
                log.debug("race: hash %s already present, skipping", h)
            except Exception:
                sp.rollback()
                row_errors += 1
                stats["errors"] += 1
                log.exception("greenhouse row insert failed")

        scrape.finished_at = datetime.now(timezone.utc)
        scrape.jobs_added = stats["added"]
        scrape.jobs_updated = stats["updated"]
        messages = []
        if fetch_errors:
            messages.append(f"{fetch_errors} companies failed to fetch")
        if row_errors:
            messages.append(f"{row_errors} rows failed")
        if stats["errors"]:
            scrape.status = "failed"
            scrape.error = "; ".join(messages) if messages else f"{stats['errors']} errors"
        else:
            scrape.status = "ok"
    return stats
