"""Direct ingester for RemoteOK — free public API, no key needed.
Filters to copywriting / email / marketing roles."""
from __future__ import annotations

import logging
from datetime import datetime, timezone

import httpx
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError

from ..db import session_scope
from ..models import Job, Source, ScrapeRun
from ..dedup import dedupe_hash

log = logging.getLogger(__name__)

API_URL = "https://remoteok.com/api"
UA = "clientfinder.ai (contact: christian@emailsandsms.com)"

TARGET_KEYWORDS = (
    "copywriter", "copywriting", "email marketing", "email marketer",
    "email copywriter", "landing page", "funnel", "creative strategist",
    "growth marketing", "lifecycle marketing",
)


def _matches(obj: dict) -> bool:
    hay = " ".join(str(obj.get(k, "")) for k in ("position", "tags", "description", "company")).lower()
    return any(kw in hay for kw in TARGET_KEYWORDS)


def fetch_jobs() -> list[dict]:
    r = httpx.get(API_URL, headers={"User-Agent": UA, "Accept": "application/json"}, timeout=30)
    r.raise_for_status()
    data = r.json()
    # RemoteOK's first element is a metadata header
    return [row for row in data if isinstance(row, dict) and row.get("position")]


def run() -> dict:
    stats = {"source": "remoteok", "fetched": 0, "matched": 0, "added": 0, "updated": 0}
    try:
        rows = fetch_jobs()
    except Exception as e:
        log.exception("remoteok fetch failed")
        stats["error"] = str(e)
        return stats

    stats["fetched"] = len(rows)
    with session_scope() as db:
        src = db.scalar(select(Source).where(Source.key == "remoteok"))
        if not src:
            src = Source(key="remoteok", kind="direct_api", display_name="RemoteOK API", config={})
            db.add(src); db.flush()

        run = ScrapeRun(source_id=src.id, status="running")
        db.add(run); db.flush()

        now = datetime.now(timezone.utc)
        seen_in_batch: set[str] = set()
        for row in rows:
            if not _matches(row):
                continue
            stats["matched"] += 1
            url = row.get("url") or row.get("apply_url")
            if not url:
                continue
            title = row.get("position", "")
            company = row.get("company")
            h = dedupe_hash(url=url, title=title, company=company)
            if h in seen_in_batch:
                continue
            seen_in_batch.add(h)
            existing = db.scalar(select(Job).where(Job.dedupe_hash == h))
            if existing:
                existing.last_seen_at = now
                stats["updated"] += 1
                continue
            posted_at = None
            if row.get("date"):
                try: posted_at = datetime.fromisoformat(row["date"].replace("Z", "+00:00"))
                except Exception: pass
            pay_min = row.get("salary_min")
            pay_max = row.get("salary_max")
            job = Job(
                dedupe_hash=h,
                source_url=url,
                platform="remoteok",
                title=title[:512],
                company_or_poster=company,
                raw_snippet=(row.get("description") or "")[:200],
                description=row.get("description"),
                type="full_time",
                pay_text=f"${pay_min:,}-${pay_max:,}" if pay_min and pay_max else None,
                pay_min=pay_min, pay_max=pay_max, pay_period="year",
                location=row.get("location"),
                remote=True,
                skills=row.get("tags") or None,
                posted_at=posted_at,
                first_seen_at=now, last_seen_at=now,
                source_key="remoteok",
                is_real_job=True,
                extraction_model="direct_api",
            )
            sp = db.begin_nested()
            try:
                db.add(job)
                db.flush()
                sp.commit()
                stats["added"] += 1
            except IntegrityError:
                sp.rollback()
                stats["updated"] += 1

        run.finished_at = now
        run.status = "ok"
        run.jobs_added = stats["added"]
        run.jobs_updated = stats["updated"]
    return stats
