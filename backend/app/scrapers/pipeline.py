"""Orchestrator: Google search → fetch → LLM extract → persist.
Idempotent — reruns upsert on dedupe_hash."""
from __future__ import annotations

import hashlib
import logging
from datetime import datetime, timezone
from typing import Iterable

from sqlalchemy import select
from sqlalchemy.orm import Session

from ..config import settings
from ..db import add_ignoring_conflict, session_scope
from ..dedup import dedupe_hash
from ..models import Job, ScrapeRun, Source
from ..schemas import ExtractedJob, SearchResult
from . import google_search, llm_extractor, page_fetcher

log = logging.getLogger(__name__)

_LIMITS = {
    "title": 512,
    "company_or_poster": 256,
    "pay_text": 128,
    "type": 32,
    "pay_period": 16,
    "experience_level": 32,
    "location": 128,
    "platform": 64,
    "extraction_model": 64,
}

_FILL = (
    "title", "company_or_poster", "pay_text", "pay_min", "pay_max", "pay_period",
    "type", "experience_level", "location", "remote", "description",
    "posted_at", "raw_snippet",
)


def _clip(value, field: str):
    if value is None or not isinstance(value, str):
        return value
    limit = _LIMITS.get(field)
    return value[:limit] if limit else value


def source_key_for_query(query: str) -> str:
    """Fit Source.key / Job.source_key, both VARCHAR(64)."""
    slug = query.lower().strip().replace(" ", "_").strip('"')
    raw = f"google:{slug}"
    if len(raw) <= 64:
        return raw
    digest = hashlib.sha256(raw.encode("utf-8")).hexdigest()[:8]
    return f"{raw[:55]}_{digest}"


def _get_or_create_source(db: Session, key: str, kind: str, display_name: str) -> Source:
    s = db.scalar(select(Source).where(Source.key == key))
    if s:
        return s
    s = Source(key=key, kind=kind, display_name=display_name[:128], enabled=True, config={})
    db.add(s)
    db.flush()
    return s


def _fill_existing(existing: Job, extracted: ExtractedJob, now: datetime) -> None:
    existing.last_seen_at = now
    for field in _FILL:
        incoming = getattr(extracted, field, None)
        if incoming is None:
            continue
        if getattr(existing, field) not in (None, ""):
            continue
        setattr(existing, field, _clip(incoming, field))
    if extracted.skills and not existing.skills:
        existing.skills = list(extracted.skills)


def _upsert_job(db: Session, source: Source, result: SearchResult, extracted: ExtractedJob,
                seen_in_batch: set[str]) -> tuple[bool, bool]:
    """Returns (added, updated).

    Identity is the search result URL. An apply link (mailto, form, DM) is not
    the posting, and hashing it collapses unrelated jobs onto one row.
    """
    url = result.url
    h = dedupe_hash(url=url, title=extracted.title, company=extracted.company_or_poster)
    now = datetime.now(timezone.utc)
    if h in seen_in_batch:
        existing = db.scalar(select(Job).where(Job.dedupe_hash == h))
        if existing:
            _fill_existing(existing, extracted, now)
            return (False, True)
        return (False, False)
    seen_in_batch.add(h)
    existing = db.scalar(select(Job).where(Job.dedupe_hash == h))
    if existing:
        _fill_existing(existing, extracted, now)
        return (False, True)

    job = Job(
        dedupe_hash=h,
        source_url=url,
        platform=_clip(result.platform, "platform") or "web",
        title=_clip(extracted.title or result.title or "Unknown", "title"),
        company_or_poster=_clip(extracted.company_or_poster, "company_or_poster"),
        raw_snippet=extracted.raw_snippet or result.snippet,
        description=extracted.description,
        type=_clip(extracted.type, "type"),
        pay_text=_clip(extracted.pay_text, "pay_text"),
        pay_min=extracted.pay_min,
        pay_max=extracted.pay_max,
        pay_period=_clip(extracted.pay_period, "pay_period"),
        experience_level=_clip(extracted.experience_level, "experience_level"),
        location=_clip(extracted.location, "location"),
        remote=extracted.remote,
        skills=extracted.skills or None,
        posted_at=extracted.posted_at,
        first_seen_at=now,
        last_seen_at=now,
        source_key=source.key,
        is_real_job=extracted.is_real_job,
        extraction_model=_clip(
            settings.extraction_model if not settings.dev_fixtures else "heuristic-mock",
            "extraction_model",
        ),
    )
    if add_ignoring_conflict(db, job):
        return (True, False)
    existing = db.scalar(select(Job).where(Job.dedupe_hash == h))
    if existing:
        _fill_existing(existing, extracted, now)
        return (False, True)
    return (False, False)


def run_pipeline_for_query(query: str, num: int = 20) -> dict:
    """Execute the full pipeline for one search query. Returns a stats dict."""
    stats = {"query": query, "search_hits": 0, "extracted": 0, "added": 0, "updated": 0, "errors": 0}
    results = google_search.search(query, num=num)
    stats["search_hits"] = len(results)
    if not results:
        log.warning("no search results for %r (DEV_FIXTURES=%s)", query, settings.dev_fixtures)
        return stats

    source_key = source_key_for_query(query)
    with session_scope() as db:
        source = _get_or_create_source(db, source_key, "google_search", f"Google: {query}")
        run = ScrapeRun(source_id=source.id, status="running")
        db.add(run)
        db.flush()

        seen_in_batch: set[str] = set()
        for result in results:
            try:
                html = page_fetcher.fetch_for_result(result)
                extracted = llm_extractor.extract(result, html)
                if not extracted.is_real_job:
                    continue
                added, updated = _upsert_job(db, source, result, extracted, seen_in_batch)
                stats["extracted"] += 1
                stats["added"] += int(added)
                stats["updated"] += int(updated)
            except Exception as e:
                log.exception("pipeline row failed: %s", e)
                stats["errors"] += 1

        run.finished_at = datetime.now(timezone.utc)
        run.status = "ok" if stats["errors"] == 0 else "failed"
        run.jobs_added = stats["added"]
        run.jobs_updated = stats["updated"]
        if stats["errors"]:
            run.error = f"{stats['errors']} rows failed; see logs"
    return stats


def run_pipeline_for_queries(queries: Iterable[str] | None = None, num: int = 20) -> list[dict]:
    queries = list(queries) if queries else list(google_search.DEFAULT_QUERIES)
    return [run_pipeline_for_query(q, num=num) for q in queries]
