"""HTML scrape for ProBlogger copywriting job board.
Friendly site, stable DOM. One of our durable sources."""
from __future__ import annotations

import logging
from urllib.parse import urljoin
from datetime import datetime, timezone

import httpx
from bs4 import BeautifulSoup
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError

from ..db import session_scope
from ..models import Job, Source, ScrapeRun
from ..dedup import dedupe_hash
from ..security_utils import safe_job_url
from ..tagging import set_tags

log = logging.getLogger(__name__)

LISTING_URL = "https://problogger.com/jobs/"
UA = "Mozilla/5.0 (clientfinder.ai)"


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
            "url": urljoin(LISTING_URL, a["href"]),   # hrefs can be relative
            "title": title,
            "snippet": card.get_text(" ", strip=True)[:400],
        })
    return out


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
            if not safe_job_url(url):
                continue
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
                set_tags(db, job)
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
