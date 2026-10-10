"""HTML scrape for ProBlogger copywriting job board.
Friendly site, stable DOM. One of our durable sources."""
from __future__ import annotations

import logging
import re
from datetime import datetime, timedelta, timezone
from urllib.parse import urljoin

import httpx
from bs4 import BeautifulSoup
from sqlalchemy import select
from sqlalchemy.exc import DataError, IntegrityError

from ..db import session_scope
from ..models import Job, Source, ScrapeRun
from ..dedup import dedupe_hash

log = logging.getLogger(__name__)

LISTING_URL = "https://problogger.com/jobs/"
UA = "Mozilla/5.0 (clientfinder.ai)"


def _job_type(card_classes: str, type_text: str) -> str | None:
    hay = f"{card_classes} {type_text}".lower()
    if re.search(r"\bfull[-\s]?time\b", hay) or "wpjb-type-full" in hay:
        return "full_time"
    if re.search(r"\b(freelance|contract)\b", hay) or "wpjb-type-freelance" in hay or "wpjb-type-contract" in hay:
        return "contract"
    return None


def _is_remote(location: str | None) -> bool | None:
    if not location:
        return None
    text = location.lower()
    if re.search(r"non[-\s]?remote|\bnot\s+remote\b", text):
        return False
    if re.search(r"\bremote\b", text):
        return True
    return False


def _parse_posted(text: str | None) -> datetime | None:
    if not text:
        return None
    now = datetime.now(timezone.utc)
    for fmt in ("%b, %d", "%b %d", "%B, %d", "%B %d"):
        try:
            parsed = datetime.strptime(text.strip(), fmt)
        except ValueError:
            continue
        posted = parsed.replace(year=now.year, tzinfo=timezone.utc)
        if posted > now + timedelta(days=2):
            posted = posted.replace(year=now.year - 1)
        return posted
    return None


def parse_listings(html: str) -> list[dict]:
    """WP Job Manager rows: div.wpjb-grid-row, title in .wpjb-col-title a."""
    soup = BeautifulSoup(html or "", "html.parser")
    out = []
    for card in soup.select("div.wpjb-grid-row"):
        title_a = card.select_one(".wpjb-col-title a[href]")
        if not title_a:
            continue
        href = title_a.get("href") or ""
        if "/jobs/job/" not in href:
            continue
        title = title_a.get_text(" ", strip=True)
        if not title or title.lower() == "post a job":
            continue
        company_el = card.select_one(".wpjb-col-title .wpjb-sub")
        company = company_el.get_text(" ", strip=True) if company_el else None
        loc_el = card.select_one(".wpjb-icon-location")
        location = loc_el.get_text(" ", strip=True) if loc_el else None
        type_el = card.select_one(".wpjb-col-location .wpjb-sub")
        type_text = type_el.get_text(" ", strip=True) if type_el else ""
        date_el = card.select_one(".wpjb-grid-col-last .wpjb-line-major")
        classes = " ".join(card.get("class") or [])
        url = urljoin(LISTING_URL, href)
        snippet = " — ".join(part for part in (title, company, location, type_text) if part)
        out.append({
            "url": url,
            "title": title,
            "company": company,
            "location": (location or "")[:128] or None,
            "type": _job_type(classes, type_text),
            "remote": _is_remote(location),
            "posted_at": _parse_posted(date_el.get_text(" ", strip=True) if date_el else None),
            "snippet": snippet[:400],
        })
    return out


def fetch_listings() -> list[dict]:
    r = httpx.get(LISTING_URL, headers={"User-Agent": UA}, timeout=30, follow_redirects=True)
    r.raise_for_status()
    return parse_listings(r.text)


def run() -> dict:
    stats = {"source": "problogger", "fetched": 0, "added": 0, "updated": 0}
    try:
        rows = fetch_listings()
    except Exception as e:
        log.exception("problogger fetch failed")
        stats["error"] = str(e)
        _record_failure(str(e))
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
            h = dedupe_hash(url=url, title=title, company=row.get("company"))
            if h in seen_in_batch:
                continue
            seen_in_batch.add(h)
            existing = db.scalar(select(Job).where(Job.dedupe_hash == h))
            if existing:
                existing.last_seen_at = now
                stats["updated"] += 1
                continue
            sp = db.begin_nested()
            try:
                job = Job(
                    dedupe_hash=h,
                    source_url=url,
                    platform="problogger",
                    title=title[:512],
                    company_or_poster=(row.get("company") or "")[:256] or None,
                    raw_snippet=row["snippet"][:200],
                    description=row["snippet"],
                    type=row.get("type"),
                    location=row.get("location"),
                    remote=row.get("remote"),
                    posted_at=row.get("posted_at"),
                    first_seen_at=now, last_seen_at=now,
                    source_key="problogger",
                    is_real_job=True,
                    extraction_model="html_scrape",
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
                log.exception("problogger row failed: %s", url)
                stats["errors"] = stats.get("errors", 0) + 1

        run.finished_at = datetime.now(timezone.utc)
        run.status = "ok" if not stats.get("errors") else "failed"
        run.jobs_added = stats["added"]
        run.jobs_updated = stats["updated"]
        if stats.get("errors"):
            run.error = f"{stats['errors']} rows failed"
    return stats


def _record_failure(error: str) -> None:
    try:
        with session_scope() as db:
            src = db.scalar(select(Source).where(Source.key == "problogger"))
            if not src:
                src = Source(key="problogger", kind="html_scrape", display_name="ProBlogger Jobs", config={})
                db.add(src)
                db.flush()
            now = datetime.now(timezone.utc)
            db.add(ScrapeRun(source_id=src.id, status="failed", started_at=now, finished_at=now, error=error[:2000]))
    except Exception:
        log.exception("could not record failed problogger run")
