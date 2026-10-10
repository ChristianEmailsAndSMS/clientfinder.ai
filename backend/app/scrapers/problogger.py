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

from ..db import session_scope
from ..models import Job, Source, ScrapeRun
from ..dedup import dedupe_hash

log = logging.getLogger(__name__)

LISTING_URL = "https://problogger.com/jobs/"
UA = "Mozilla/5.0 (clientfinder.ai)"
MAX_PAGES = 20
_SKIP_HREF = ("/post-a-job", "/login", "/dashboard", "/register")


def _abs_url(page_url: str, href: str) -> str:
    return urljoin(page_url, href)


def _is_job_href(href: str) -> bool:
    lowered = href.lower()
    if any(skip in lowered for skip in _SKIP_HREF):
        return False
    return "/jobs/job/" in lowered or "/job/" in lowered


def parse_listing_html(html: str, page_url: str) -> list[dict]:
    """Pull job rows from a ProBlogger listing page.

    The board is WP Job Board. A generic `article` selector matches the page
    wrapper and the first link is "Post a Job", so we only keep job permalinks.
    """
    soup = BeautifulSoup(html, "html.parser")
    cards = soup.select(".wpjb-grid-row.wpjb-click-area")
    if not cards:
        cards = soup.select(".job-listing, .job")
    out = []
    for card in cards:
        link = None
        for candidate in card.find_all("a", href=True):
            if _is_job_href(candidate["href"]):
                link = candidate
                break
        if link is None:
            continue
        title = link.get_text(strip=True)
        if not title or title.lower() in {"post a job", "view", "apply"}:
            continue
        out.append({
            "url": _abs_url(page_url, link["href"]),
            "title": title,
            "snippet": card.get_text(" ", strip=True)[:400],
        })
    return out


def fetch_listings() -> list[dict]:
    out: list[dict] = []
    seen: set[str] = set()
    with httpx.Client(headers={"User-Agent": UA}, timeout=30, follow_redirects=True) as client:
        for page in range(1, MAX_PAGES + 1):
            url = f"{LISTING_URL}?show_results=1&page={page}"
            r = client.get(url)
            r.raise_for_status()
            rows = parse_listing_html(r.text, str(r.url))
            if not rows:
                break
            new = 0
            for row in rows:
                if row["url"] in seen:
                    continue
                seen.add(row["url"])
                out.append(row)
                new += 1
            if new == 0:
                break
    return out


def _touch_existing(db, dedupe: str, now: datetime) -> bool:
    existing = db.scalar(select(Job).where(Job.dedupe_hash == dedupe))
    if not existing:
        return False
    existing.last_seen_at = now
    return True


def run() -> dict:
    stats = {"source": "problogger", "fetched": 0, "added": 0, "updated": 0}
    try:
        rows = fetch_listings()
    except Exception as e:
        log.exception("problogger fetch failed")
        stats["error"] = str(e)
        return stats

    stats["fetched"] = len(rows)
    with session_scope() as db:
        src = db.scalar(select(Source).where(Source.key == "problogger"))
        if not src:
            src = Source(key="problogger", kind="html_scrape", display_name="ProBlogger Jobs", config={})
            db.add(src); db.flush()

        run = ScrapeRun(source_id=src.id, status="running")
        db.add(run); db.flush()
        now = datetime.now(timezone.utc)

        seen_in_batch: set[str] = set()
        for row in rows:
            url, title = row["url"], row["title"]
            h = dedupe_hash(url=url, title=title, company=None)
            if h in seen_in_batch:
                continue
            seen_in_batch.add(h)
            existing = db.scalar(select(Job).where(Job.dedupe_hash == h))
            if existing:
                existing.last_seen_at = now
                stats["updated"] += 1
                continue
            job = Job(
                dedupe_hash=h,
                source_url=url,
                platform="problogger",
                title=title[:512],
                raw_snippet=row["snippet"][:200],
                description=row["snippet"],
                type="contract",  # ProBlogger is predominantly contract/freelance
                first_seen_at=now, last_seen_at=now,
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
                if _touch_existing(db, h, now):
                    stats["updated"] += 1

        run.finished_at = now
        run.status = "ok"
        run.jobs_added = stats["added"]
        run.jobs_updated = stats["updated"]
    return stats
