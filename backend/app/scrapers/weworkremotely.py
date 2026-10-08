"""We Work Remotely RSS. Item titles look like "Company: Role". Per-feed failures are tolerated."""
from __future__ import annotations

import logging
from datetime import datetime, timezone

import feedparser

from .common import CollectResult, NormalizedJob, employment_type, get, html_to_text, ingest, title_matches

log = logging.getLogger(__name__)

FEEDS: tuple[str, ...] = (
    "https://weworkremotely.com/categories/remote-copywriting-jobs.rss",
    "https://weworkremotely.com/categories/remote-marketing-jobs.rss",
    "https://weworkremotely.com/categories/remote-sales-and-marketing-jobs.rss",
)
SOURCE_KEY = "weworkremotely"


def _split_title(raw: str) -> tuple[str | None, str]:
    company, sep, role = raw.partition(": ")
    return (company.strip(), role.strip()) if sep else (None, raw.strip())


def parse_feed(text: str) -> CollectResult:
    feed = feedparser.parse(text)
    out = CollectResult(fetched=len(feed.entries))
    for e in feed.entries:
        company, title = _split_title(e.get("title", ""))
        link = e.get("link")
        if not link or not title_matches(title):
            continue
        ts = e.get("published_parsed")
        out.jobs.append(NormalizedJob(
            url=link, title=title, company=company,
            description=html_to_text(e.get("summary") or e.get("description")),
            type=employment_type(e.get("type")),
            location=e.get("region") or None, remote=True,
            posted_at=datetime(*ts[:6], tzinfo=timezone.utc) if ts else None,
        ))
    return out


def collect() -> CollectResult:
    total = CollectResult()
    for url in FEEDS:
        try:
            part = parse_feed(get(url).text)
        except Exception as e:
            total.errors.append(f"{url}: {e}")
            continue
        total.fetched += part.fetched
        total.jobs.extend(part.jobs)
    return total


def run() -> dict:
    return ingest(SOURCE_KEY, "We Work Remotely RSS", "weworkremotely", collect(), kind="rss")
