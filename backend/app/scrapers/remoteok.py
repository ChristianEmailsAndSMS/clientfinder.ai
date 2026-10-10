"""Direct ingester for RemoteOK — free public API, no key needed.
Filters to copywriting / email / marketing roles."""
from __future__ import annotations

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

API_URL = "https://remoteok.com/api"
UA = "clientfinder.ai (contact: christian@emailsandsms.com)"

TARGET_KEYWORDS = (
    "copywriter", "copywriting", "email marketing", "email marketer",
    "email copywriter", "landing page", "funnel", "creative strategist",
    "growth marketing", "lifecycle marketing",
)


def tags_of(obj: dict) -> list[str]:
    raw = obj.get("tags") or []
    if isinstance(raw, str):
        return [raw]
    if not isinstance(raw, list):
        return []
    return [str(tag) for tag in raw if tag]


def _matches(obj: dict) -> bool:
    # Match the role, not boilerplate buried in the HTML description.
    hay = " ".join([str(obj.get("position") or ""), " ".join(tags_of(obj))]).lower()
    return any(kw in hay for kw in TARGET_KEYWORDS)


def job_type(tags: list[str]) -> str:
    tokens = set(" ".join(tags).lower().replace("-", " ").split())
    if "intern" in tokens or "internship" in tokens:
        return "internship"
    if "parttime" in tokens or {"part", "time"} <= tokens:
        return "part_time"
    return "full_time"


def plain_text(html: str | None, limit: int | None = None) -> str | None:
    if not html:
        return None
    text = BeautifulSoup(str(html), "html.parser").get_text(" ", strip=True)
    text = re.sub(r"\s+", " ", text).strip()
    if not text:
        return None
    return text[:limit] if limit else text


def pay_fields(pay_min, pay_max):
    """RemoteOK sends 0 when pay is undisclosed. That is not a $0 salary."""
    def positive(value):
        try:
            n = float(value)
        except (TypeError, ValueError):
            return None
        return n if n > 0 else None

    lo, hi = positive(pay_min), positive(pay_max)
    if lo is None and hi is None:
        return None, None, None, None
    if lo is not None and hi is not None:
        text = f"${lo:,.0f}-${hi:,.0f}"
    elif lo is not None:
        text = f"${lo:,.0f}"
    else:
        text = f"${hi:,.0f}"
    return text[:128], lo, hi, "year"


def fetch_jobs() -> list[dict]:
    r = httpx.get(API_URL, headers={"User-Agent": UA, "Accept": "application/json"}, timeout=30)
    r.raise_for_status()
    data = r.json()
    # RemoteOK's first element is a metadata header
    return [row for row in data if isinstance(row, dict) and row.get("position")]


def _ensure_source(db) -> Source:
    src = db.scalar(select(Source).where(Source.key == "remoteok"))
    if not src:
        src = Source(key="remoteok", kind="direct_api", display_name="RemoteOK API", config={})
        db.add(src)
        db.flush()
    return src


def _location(row: dict) -> str | None:
    loc = row.get("location")
    if isinstance(loc, dict):
        loc = loc.get("name") or loc.get("city")
    if loc is None:
        return None
    text = str(loc).strip()
    return text[:128] or None


def run() -> dict:
    stats = {"source": "remoteok", "fetched": 0, "matched": 0, "added": 0, "updated": 0}
    error = None
    rows: list[dict] = []
    try:
        rows = fetch_jobs()
    except Exception as e:
        log.exception("remoteok fetch failed")
        error = str(e)
        stats["error"] = error

    stats["fetched"] = len(rows)
    with session_scope() as db:
        src = _ensure_source(db)
        run = ScrapeRun(source_id=src.id, status="running")
        db.add(run)
        db.flush()
        if error:
            run.finished_at = datetime.now(timezone.utc)
            run.status = "failed"
            run.error = error[:4000]
            return stats

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
            if company is not None:
                company = str(company)[:256]
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
                    posted_at = datetime.fromisoformat(str(row["date"]).replace("Z", "+00:00"))
                except Exception:
                    pass
            tags = tags_of(row)
            pay_text, pay_min, pay_max, pay_period = pay_fields(row.get("salary_min"), row.get("salary_max"))
            description = plain_text(row.get("description"), limit=8000)
            job = Job(
                dedupe_hash=h,
                source_url=url,
                platform="remoteok",
                title=title[:512],
                company_or_poster=company or None,
                raw_snippet=plain_text(row.get("description"), limit=200),
                description=description,
                type=job_type(tags),
                pay_text=pay_text,
                pay_min=pay_min,
                pay_max=pay_max,
                pay_period=pay_period,
                location=_location(row),
                remote=True,
                skills=tags or None,
                posted_at=posted_at,
                first_seen_at=now, last_seen_at=now,
                source_key="remoteok",
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

        run.finished_at = datetime.now(timezone.utc)
        run.status = "ok"
        run.jobs_added = stats["added"]
        run.jobs_updated = stats["updated"]
    return stats
