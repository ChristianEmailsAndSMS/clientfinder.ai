"""HTML scrape for ProBlogger copywriting job board.
Friendly site, stable DOM. One of our durable sources."""
from __future__ import annotations

import logging
from datetime import datetime, timezone
from urllib.parse import urljoin

import httpx
from bs4 import BeautifulSoup
from sqlalchemy import select

from ..db import add_ignoring_conflict, session_scope
from ..dedup import dedupe_hash
from ..models import Job, ScrapeRun, Source

log = logging.getLogger(__name__)

LISTING_URL = "https://problogger.com/jobs/"
UA = "Mozilla/5.0 (clientfinder.ai)"


def _listing_type(card) -> str:
    blob = " ".join(card.get("class") or []).lower()
    for node in card.select("[class*='type']"):
        blob += " " + " ".join(node.get("class") or []).lower()
        blob += " " + node.get_text(" ", strip=True).lower()
    if "full-time" in blob or "full time" in blob:
        return "full_time"
    return "contract"


def parse_listings(html: str) -> list[dict]:
    """Read WPJobBoard rows. The page's wrapping article is the Post a Job CTA, not a listing."""
    soup = BeautifulSoup(html, "html.parser")
    out = []
    for card in soup.select(".wpjb-grid-row"):
        a = card.select_one(".wpjb-col-title a[href]")
        if not a:
            continue
        title = a.get_text(strip=True)
        href = a["href"]
        if not title or "post-a-job" in href or title.lower().startswith("post a job"):
            continue
        company_el = card.select_one(".wpjb-sub")
        loc_el = card.select_one(".wpjb-icon-location")
        out.append({
            "url": urljoin(LISTING_URL, href),
            "title": title,
            "company": company_el.get_text(" ", strip=True) if company_el else None,
            "location": loc_el.get_text(" ", strip=True) if loc_el else None,
            "snippet": card.get_text(" ", strip=True)[:400],
            "type": _listing_type(card),
        })
    return out


def fetch_listings() -> list[dict]:
    r = httpx.get(LISTING_URL, headers={"User-Agent": UA}, timeout=30, follow_redirects=True)
    r.raise_for_status()
    return parse_listings(r.text)


def _ensure_source(db) -> Source:
    src = db.scalar(select(Source).where(Source.key == "problogger"))
    if not src:
        src = Source(key="problogger", kind="html_scrape", display_name="ProBlogger Jobs", config={})
        db.add(src)
        db.flush()
    return src


def run() -> dict:
    stats = {"source": "problogger", "fetched": 0, "added": 0, "updated": 0}
    error = None
    rows: list[dict] = []
    try:
        rows = fetch_listings()
    except Exception as e:
        log.exception("problogger fetch failed")
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
            url, title = row["url"], row["title"]
            company = (row.get("company") or None)
            h = dedupe_hash(url=url, title=title, company=company)
            if h in seen_in_batch:
                continue
            seen_in_batch.add(h)
            existing = db.scalar(select(Job).where(Job.dedupe_hash == h))
            if existing:
                existing.last_seen_at = now
                stats["updated"] += 1
                continue
            location = row.get("location")
            job = Job(
                dedupe_hash=h,
                source_url=url,
                platform="problogger",
                title=title[:512],
                company_or_poster=(company[:256] if company else None),
                raw_snippet=row["snippet"][:200],
                description=row["snippet"],
                type=row.get("type") or "contract",
                location=(location[:128] if location else None),
                first_seen_at=now, last_seen_at=now,
                source_key="problogger",
                is_real_job=True,
                extraction_model="html_scrape",
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
