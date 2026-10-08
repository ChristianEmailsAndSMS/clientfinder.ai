"""Shared plumbing for direct-ingestion sources (free public APIs / RSS, no LLM needed).

A source module implements `collect()` -> CollectResult (network + parsing only, no DB) and
`run()` -> `ingest(...)`. Keeping collect() DB-free lets scripts/check_sources.py test a source
against the live endpoint without writing anything."""
from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import datetime, timezone

import httpx
from bs4 import BeautifulSoup
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError

from ..db import session_scope
from ..dedup import dedupe_hash
from ..models import Job, ScrapeRun, Source

log = logging.getLogger(__name__)

UA = "clientfinder.ai job aggregator (contact: christian@emailsandsms.com)"

# Matched against the job TITLE only. Description matching is far too noisy ("our engineers send email").
TITLE_KEYWORDS = (
    "copywriter", "copywriting", "copy writer", "copy chief", "email marketing", "email marketer",
    "email copy", "email specialist", "email manager", "email strategist", "lifecycle",
    "landing page", "funnel", "creative strategist", "creative director", "growth marketing",
    "crm manager", "direct response", "conversion",
)


def title_matches(title: str | None, extra: tuple[str, ...] = ()) -> bool:
    t = (title or "").lower()
    return any(k in t for k in TITLE_KEYWORDS + extra)


@dataclass
class NormalizedJob:
    url: str
    title: str
    company: str | None = None
    description: str | None = None
    type: str | None = None            # contract | full_time
    pay_text: str | None = None
    pay_min: float | None = None
    pay_max: float | None = None
    pay_period: str | None = None      # hour | year | project
    location: str | None = None
    remote: bool | None = None
    skills: list[str] = field(default_factory=list)
    posted_at: datetime | None = None


@dataclass
class CollectResult:
    fetched: int = 0                   # raw items seen, before keyword filtering
    jobs: list[NormalizedJob] = field(default_factory=list)   # after filtering
    errors: list[str] = field(default_factory=list)           # one entry per failed feed/company


def get(url: str, *, timeout: float = 30.0) -> httpx.Response:
    r = httpx.get(url, headers={"User-Agent": UA, "Accept": "application/json, application/rss+xml, */*"},
                  timeout=timeout, follow_redirects=True)
    r.raise_for_status()
    return r


def html_to_text(html: str | None, limit: int = 4000) -> str | None:
    if not html:
        return None
    text = BeautifulSoup(html, "html.parser").get_text(" ", strip=True)
    return text[:limit] or None


def employment_type(raw: str | None) -> str | None:
    r = (raw or "").lower().replace("_", "-").replace(" ", "-")
    if any(k in r for k in ("contract", "freelance", "temporary", "temp")):
        return "contract"
    if "full" in r:
        return "full_time"
    return None


def parse_dt(value) -> datetime | None:
    """ISO string (naive = UTC), epoch seconds/milliseconds, or None."""
    if value in (None, ""):
        return None
    try:
        if isinstance(value, (int, float)):
            secs = value / 1000 if value > 1e11 else value
            return datetime.fromtimestamp(secs, tz=timezone.utc)
        dt = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
        return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)
    except (ValueError, OverflowError, OSError):
        return None


def ingest(source_key: str, display_name: str, platform: str, result: CollectResult, kind: str = "direct_api") -> dict:
    """Idempotent upsert of a CollectResult. Re-runs only bump last_seen_at."""
    stats = {"source": source_key, "fetched": result.fetched, "matched": len(result.jobs),
             "added": 0, "updated": 0, "errors": len(result.errors)}
    now = datetime.now(timezone.utc)
    with session_scope() as db:
        src = db.scalar(select(Source).where(Source.key == source_key))
        if not src:
            src = Source(key=source_key, kind=kind, display_name=display_name, config={})
            db.add(src)
            db.flush()
        run = ScrapeRun(source_id=src.id, status="running")
        db.add(run)
        db.flush()

        seen: set[str] = set()
        for nj in result.jobs:
            h = dedupe_hash(url=nj.url, title=nj.title, company=nj.company)
            if h in seen:
                continue
            seen.add(h)
            existing = db.scalar(select(Job).where(Job.dedupe_hash == h))
            if existing:
                existing.last_seen_at = now
                stats["updated"] += 1
                continue
            job = Job(
                dedupe_hash=h, source_url=nj.url, platform=platform,
                title=nj.title[:512], company_or_poster=nj.company,
                raw_snippet=(nj.description or nj.title)[:200], description=nj.description,
                type=nj.type, pay_text=nj.pay_text, pay_min=nj.pay_min, pay_max=nj.pay_max,
                pay_period=nj.pay_period, location=nj.location, remote=nj.remote,
                skills=nj.skills or None, posted_at=nj.posted_at,
                first_seen_at=now, last_seen_at=now, source_key=source_key,
                is_real_job=True, extraction_model="direct_api",
            )
            sp = db.begin_nested()  # savepoint: one unique violation must not kill the batch
            try:
                db.add(job)
                db.flush()
                sp.commit()
                stats["added"] += 1
            except IntegrityError:
                sp.rollback()
                stats["updated"] += 1

        run.finished_at = now
        run.jobs_added, run.jobs_updated = stats["added"], stats["updated"]
        # Every feed failing (nothing fetched) is a failed run; some feeds failing is still ok-with-note.
        run.status = "failed" if (result.errors and result.fetched == 0) else "ok"
        if result.errors:
            run.error = "; ".join(result.errors)[:2000]
    return stats
