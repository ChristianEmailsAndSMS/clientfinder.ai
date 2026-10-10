"""Direct ingester for RemoteOK — free public API, no key needed.
Filters to copywriting / email / marketing roles."""
from __future__ import annotations

import logging
import math
from datetime import datetime, timezone

import httpx
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

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


def _clip(value, limit: int) -> str | None:
    if value is None:
        return None
    text = value if isinstance(value, str) else str(value)
    return text[:limit]


def _coerce_salary(value) -> tuple[float | None, bool]:
    """Return (number, failed). A blank value is missing, not a failure."""
    if value is None:
        return None, False
    if isinstance(value, str):
        cleaned = value.strip().replace(",", "").replace("$", "")
        if not cleaned:
            return None, False
        value = cleaned
    elif isinstance(value, bool):
        return None, True
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None, True
    if not math.isfinite(number):
        return None, True
    return number, False


def _fmt_amount(number: float) -> str:
    if number.is_integer():
        return f"{int(number):,}"
    return f"{number:,.2f}"


def _pay_fields(raw_min, raw_max) -> tuple[float | None, float | None, str | None, str | None]:
    """Coerce RemoteOK salaries. Unparseable values drop pay instead of raising.

    The public API sometimes sends salary_min/salary_max as numeric strings.
    Formatting those with ``:,`` raises ValueError, and a string in a Float
    column aborts the batch. Return pay_period ``year`` only when coercion
    succeeded (including when both ends are simply absent).
    """
    pay_min, bad_min = _coerce_salary(raw_min)
    pay_max, bad_max = _coerce_salary(raw_max)
    if bad_min or bad_max:
        return None, None, None, None
    pay_text = None
    if pay_min is not None and pay_max is not None:
        pay_text = _clip(f"${_fmt_amount(pay_min)}-${_fmt_amount(pay_max)}", 128)
    return pay_min, pay_max, pay_text, "year"


def _ensure_source(db: Session) -> Source:
    src = db.scalar(select(Source).where(Source.key == "remoteok"))
    if not src:
        src = Source(key="remoteok", kind="direct_api", display_name="RemoteOK API", config={})
        db.add(src)
        db.flush()
    return src


def fetch_jobs() -> list[dict]:
    r = httpx.get(API_URL, headers={"User-Agent": UA, "Accept": "application/json"}, timeout=30)
    r.raise_for_status()
    data = r.json()
    # RemoteOK's first element is a metadata header
    return [row for row in data if isinstance(row, dict) and row.get("position")]


def _record_fetch_failure(exc: Exception) -> None:
    with session_scope() as db:
        src = _ensure_source(db)
        db.add(ScrapeRun(
            source_id=src.id,
            status="failed",
            error=str(exc),
            finished_at=datetime.now(timezone.utc),
            jobs_added=0,
            jobs_updated=0,
        ))


def run() -> dict:
    stats = {"source": "remoteok", "fetched": 0, "matched": 0, "added": 0, "updated": 0, "errors": 0}
    try:
        rows = fetch_jobs()
    except Exception as e:
        log.exception("remoteok fetch failed")
        stats["error"] = str(e)
        stats["errors"] = 1
        _record_fetch_failure(e)
        return stats

    stats["fetched"] = len(rows)
    with session_scope() as db:
        src = _ensure_source(db)

        scrape = ScrapeRun(source_id=src.id, status="running")
        db.add(scrape)
        db.flush()

        now = datetime.now(timezone.utc)
        seen_in_batch: set[str] = set()
        for row in rows:
            if not _matches(row):
                continue
            stats["matched"] += 1
            url = row.get("url") or row.get("apply_url")
            if not url:
                continue
            title = row.get("position", "") or ""
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
                try:
                    posted_at = datetime.fromisoformat(row["date"].replace("Z", "+00:00"))
                except Exception:
                    pass
            description = row.get("description")
            pay_min, pay_max, pay_text, pay_period = _pay_fields(row.get("salary_min"), row.get("salary_max"))
            job = Job(
                dedupe_hash=h,
                source_url=url,
                platform="remoteok",
                title=_clip(title, 512) or "",
                company_or_poster=_clip(company, 256),
                raw_snippet=_clip(description or "", 200),
                description=_clip(description, 4000),
                type="full_time",
                pay_text=pay_text,
                pay_min=pay_min,
                pay_max=pay_max,
                pay_period=pay_period,
                location=_clip(row.get("location"), 128),
                remote=True,
                skills=row.get("tags") or None,
                posted_at=posted_at,
                first_seen_at=now,
                last_seen_at=now,
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
            except Exception:
                sp.rollback()
                stats["errors"] += 1
                log.exception("remoteok row insert failed")

        scrape.finished_at = datetime.now(timezone.utc)
        scrape.jobs_added = stats["added"]
        scrape.jobs_updated = stats["updated"]
        if stats["errors"]:
            scrape.status = "failed"
            scrape.error = f"{stats['errors']} rows failed"
        else:
            scrape.status = "ok"
    return stats
