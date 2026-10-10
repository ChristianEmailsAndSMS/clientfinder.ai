"""HTML scrape for ProBlogger copywriting job board.
Friendly site, stable DOM. One of our durable sources."""
from __future__ import annotations

import logging
from datetime import datetime, timezone
from urllib.parse import urljoin

import httpx
from bs4 import BeautifulSoup
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from ..db import session_scope
from ..models import Job, Source, ScrapeRun
from ..dedup import dedupe_hash

log = logging.getLogger(__name__)

LISTING_URL = "https://problogger.com/jobs/"
UA = "Mozilla/5.0 (clientfinder.ai)"


def _clip(value, limit: int) -> str | None:
    if value is None:
        return None
    text = value if isinstance(value, str) else str(value)
    return text[:limit]


def _absolute_url(href: str) -> str:
    return urljoin(LISTING_URL, href)


def _ensure_source(db: Session) -> Source:
    src = db.scalar(select(Source).where(Source.key == "problogger"))
    if not src:
        src = Source(key="problogger", kind="html_scrape", display_name="ProBlogger Jobs", config={})
        db.add(src)
        db.flush()
    return src


def fetch_listings() -> list[dict]:
    r = httpx.get(LISTING_URL, headers={"User-Agent": UA}, timeout=30, follow_redirects=True)
    r.raise_for_status()
    soup = BeautifulSoup(r.text, "html.parser")
    out = []
    # ProBlogger's board uses .job-listing or similar containers; be defensive about selectors.
    for card in soup.select("article, .job, .job-listing"):
        a = card.find("a", href=True)
        if not a:
            continue
        title = a.get_text(strip=True)
        if not title:
            continue
        out.append({
            "url": _absolute_url(a["href"]),
            "title": title,
            "snippet": card.get_text(" ", strip=True)[:400],
        })
    return out


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
    stats = {"source": "problogger", "fetched": 0, "added": 0, "updated": 0, "errors": 0}
    try:
        rows = fetch_listings()
    except Exception as e:
        log.exception("problogger fetch failed")
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
            raw_url = row.get("url") or ""
            if not raw_url:
                continue
            url = _absolute_url(raw_url)
            title = row.get("title") or ""
            h = dedupe_hash(url=url, title=title, company=None)
            if h in seen_in_batch:
                continue
            seen_in_batch.add(h)
            existing = db.scalar(select(Job).where(Job.dedupe_hash == h))
            if existing:
                existing.last_seen_at = now
                stats["updated"] += 1
                continue
            snippet = row.get("snippet")
            job = Job(
                dedupe_hash=h,
                source_url=url,
                platform="problogger",
                title=_clip(title, 512) or "",
                raw_snippet=_clip(snippet or "", 200),
                description=_clip(snippet, 4000),
                type="contract",  # ProBlogger is predominantly contract/freelance
                first_seen_at=now,
                last_seen_at=now,
                source_key="problogger",
                is_real_job=True,
                extraction_model="html_scrape",
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
                log.exception("problogger row insert failed")

        scrape.finished_at = datetime.now(timezone.utc)
        scrape.jobs_added = stats["added"]
        scrape.jobs_updated = stats["updated"]
        if stats["errors"]:
            scrape.status = "failed"
            scrape.error = f"{stats['errors']} rows failed"
        else:
            scrape.status = "ok"
    return stats
