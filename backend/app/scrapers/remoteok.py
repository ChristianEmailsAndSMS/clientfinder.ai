"""Direct ingester for RemoteOK — free public API, no key needed.
Filters to copywriting / email / marketing roles."""
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

API_URL = "https://remoteok.com/api"
UA = "clientfinder.ai (contact: christian@emailsandsms.com)"

TARGET_KEYWORDS = (
    "copywriter", "copywriting", "email marketing", "email marketer",
    "email copywriter", "landing page", "funnel", "creative strategist",
    "growth marketing", "lifecycle marketing",
)


def _tag_text(tags) -> str:
    if isinstance(tags, list):
        return " ".join(str(tag) for tag in tags)
    return str(tags or "")


def _matches(obj: dict) -> bool:
    # Description HTML mentions "landing page" in unrelated roles. Match the title and tags.
    hay = f"{obj.get('position', '')} {_tag_text(obj.get('tags'))}".lower()
    return any(kw in hay for kw in TARGET_KEYWORDS)


def _plain(value) -> str:
    if not value:
        return ""
    return BeautifulSoup(str(value), "html.parser").get_text(" ", strip=True)


def _amount(value) -> float | None:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    if number <= 0:
        return None
    return number


def _pay_text(pay_min: float | None, pay_max: float | None) -> str | None:
    if pay_min and pay_max:
        return f"${pay_min:,.0f}-${pay_max:,.0f}"
    if pay_min:
        return f"${pay_min:,.0f}"
    if pay_max:
        return f"${pay_max:,.0f}"
    return None


def _job_type(title: str, tags) -> str | None:
    hay = f"{title} {_tag_text(tags)}".lower()
    if re.search(r"\b(contract|freelance|contractor)\b", hay):
        return "contract"
    if re.search(r"\bfull[-\s]?time\b", hay):
        return "full_time"
    return None


def _clip(value, limit: int) -> str | None:
    if value is None:
        return None
    text = str(value).strip()
    return text[:limit] or None


def fetch_jobs() -> list[dict]:
    r = httpx.get(API_URL, headers={"User-Agent": UA, "Accept": "application/json"}, timeout=30)
    r.raise_for_status()
    data = r.json()
    if not isinstance(data, list):
        raise RuntimeError("RemoteOK response was not a job list")
    # RemoteOK's first element is a metadata header
    return [row for row in data if isinstance(row, dict) and row.get("position")]


def run() -> dict:
    stats = {"source": "remoteok", "fetched": 0, "matched": 0, "added": 0, "updated": 0}
    try:
        rows = fetch_jobs()
    except Exception as e:
        log.exception("remoteok fetch failed")
        stats["error"] = str(e)
        _record_failure("remoteok", "direct_api", "RemoteOK API", str(e))
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
                try: posted_at = datetime.fromisoformat(str(row["date"]).replace("Z", "+00:00"))
                except Exception: pass
            pay_min = _amount(row.get("salary_min"))
            pay_max = _amount(row.get("salary_max"))
            description = _plain(row.get("description"))
            tags = row.get("tags") if isinstance(row.get("tags"), list) else None
            sp = db.begin_nested()
            try:
                job = Job(
                    dedupe_hash=h,
                    source_url=url,
                    platform="remoteok",
                    title=title[:512],
                    company_or_poster=_clip(company, 256),
                    raw_snippet=description[:200] or None,
                    description=description[:4000] or None,
                    type=_job_type(title, tags),
                    pay_text=_pay_text(pay_min, pay_max),
                    pay_min=pay_min,
                    pay_max=pay_max,
                    pay_period="year" if pay_min or pay_max else None,
                    location=_clip(row.get("location"), 128),
                    remote=True,
                    skills=tags or None,
                    posted_at=posted_at,
                    first_seen_at=now, last_seen_at=now,
                    source_key="remoteok",
                    is_real_job=True,
                    extraction_model="direct_api",
                )
                db.add(job)
                db.flush()
                sp.commit()
                stats["added"] += 1
            except (IntegrityError, DataError):
                sp.rollback()
                stats["updated"] += 1
            except Exception:
                sp.rollback()
                log.exception("remoteok row failed: %s", url)
                stats["errors"] = stats.get("errors", 0) + 1

        run.finished_at = datetime.now(timezone.utc)
        run.status = "ok" if not stats.get("errors") else "failed"
        run.jobs_added = stats["added"]
        run.jobs_updated = stats["updated"]
        if stats.get("errors"):
            run.error = f"{stats['errors']} rows failed"
    return stats


def _record_failure(key: str, kind: str, display_name: str, error: str) -> None:
    try:
        with session_scope() as db:
            src = db.scalar(select(Source).where(Source.key == key))
            if not src:
                src = Source(key=key, kind=kind, display_name=display_name, config={})
                db.add(src)
                db.flush()
            now = datetime.now(timezone.utc)
            db.add(ScrapeRun(
                source_id=src.id,
                status="failed",
                started_at=now,
                finished_at=now,
                error=error[:2000],
            ))
    except Exception:
        log.exception("could not record failed %s run", key)
