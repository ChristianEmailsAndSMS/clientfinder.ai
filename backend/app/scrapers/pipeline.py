"""Orchestrator: Google search → fetch → LLM extract → persist.
Idempotent — reruns upsert on dedupe_hash."""
from __future__ import annotations

import hashlib
import logging
import re
from datetime import datetime, timezone
from typing import Iterable

from sqlalchemy import select
from sqlalchemy.orm import Session
from sqlalchemy.exc import IntegrityError

from ..db import session_scope
from ..models import Job, Source, ScrapeRun
from ..schemas import SearchResult, ExtractedJob
from ..dedup import dedupe_hash
from ..config import settings
from . import google_search, page_fetcher, llm_extractor

log = logging.getLogger(__name__)

_SLUG_RE = re.compile(r"[^a-z0-9]+")


def source_key_for_query(query: str) -> str:
    """Stable source key that fits sources.key / jobs.source_key (String(64))."""
    slug = _SLUG_RE.sub("_", query.lower()).strip("_")
    prefix = "google:"
    full = f"{prefix}{slug}" if slug else prefix.rstrip(":")
    if slug and len(full) <= 64:
        return full
    digest = hashlib.sha256(query.strip().lower().encode("utf-8")).hexdigest()[:8]
    room = 64 - len(prefix) - 1 - len(digest)
    trimmed = slug[:room].strip("_")
    if trimmed:
        return f"{prefix}{trimmed}_{digest}"
    return f"{prefix}{digest}"


def _get_or_create_source(db: Session, key: str, kind: str, display_name: str, query: str) -> Source:
    s = db.scalar(select(Source).where(Source.key == key))
    if s:
        return s
    s = Source(
        key=key,
        kind=kind,
        display_name=display_name[:128],
        enabled=True,
        config={"query": query},
    )
    sp = db.begin_nested()
    try:
        db.add(s)
        db.flush()
        sp.commit()
        return s
    except IntegrityError:
        sp.rollback()
        s = db.scalar(select(Source).where(Source.key == key))
        if s:
            return s
        raise


def _apply_update(existing: Job, extracted: ExtractedJob, now: datetime) -> None:
    existing.last_seen_at = now
    for field in ("company_or_poster", "pay_text", "pay_min", "pay_max", "pay_period",
                  "type", "experience_level", "location", "remote", "description"):
        v = getattr(extracted, field)
        if v is not None and getattr(existing, field) in (None, ""):
            setattr(existing, field, v)
    if extracted.skills and not existing.skills:
        existing.skills = extracted.skills


def _upsert_job(db: Session, source: Source, result: SearchResult, extracted: ExtractedJob,
                seen_in_batch: set[str]) -> tuple[bool, bool]:
    """Returns (added, updated).

    The dedupe key is the search-result URL, not the LLM apply link. Apply URLs
    move between runs; hashing them created a second row for the same posting.
    """
    click_url = extracted.apply_url or result.url
    h = dedupe_hash(url=result.url, title=extracted.title, company=extracted.company_or_poster)
    if h in seen_in_batch:
        return (False, False)
    seen_in_batch.add(h)
    existing = db.scalar(select(Job).where(Job.dedupe_hash == h))
    now = datetime.now(timezone.utc)
    if existing:
        _apply_update(existing, extracted, now)
        return (False, True)

    job = Job(
        dedupe_hash=h,
        source_url=click_url,
        platform=result.platform,
        title=extracted.title or result.title,
        company_or_poster=extracted.company_or_poster,
        raw_snippet=extracted.raw_snippet or result.snippet,
        description=extracted.description,
        type=extracted.type,
        pay_text=extracted.pay_text,
        pay_min=extracted.pay_min,
        pay_max=extracted.pay_max,
        pay_period=extracted.pay_period,
        experience_level=extracted.experience_level,
        location=extracted.location,
        remote=extracted.remote,
        skills=extracted.skills or None,
        posted_at=extracted.posted_at,
        first_seen_at=now,
        last_seen_at=now,
        source_key=source.key,
        is_real_job=extracted.is_real_job,
        extraction_model=(settings.extraction_model if not settings.dev_fixtures else "heuristic-mock"),
    )
    sp = db.begin_nested()
    try:
        db.add(job)
        db.flush()
        sp.commit()
        return (True, False)
    except IntegrityError:
        sp.rollback()
        existing = db.scalar(select(Job).where(Job.dedupe_hash == h))
        if existing:
            _apply_update(existing, extracted, now)
            return (False, True)
        return (False, False)


def run_pipeline_for_query(query: str, num: int = 20) -> dict:
    """Execute the full pipeline for one search query. Returns a stats dict."""
    stats = {"query": query, "search_hits": 0, "extracted": 0, "added": 0, "updated": 0, "errors": 0}
    search_error = None
    try:
        results = google_search.search(query, num=num)
    except Exception as e:
        log.exception("search failed for %r", query)
        results = []
        search_error = str(e)
        stats["errors"] = 1
    stats["search_hits"] = len(results)
    if not results and search_error is None:
        log.warning("no search results for %r (DEV_FIXTURES=%s)", query, settings.dev_fixtures)

    source_key = source_key_for_query(query)
    with session_scope() as db:
        source = _get_or_create_source(db, source_key, "google_search", f"Google: {query}", query)
        run = ScrapeRun(source_id=source.id, status="running")
        db.add(run)
        db.flush()

        if not results:
            run.finished_at = datetime.now(timezone.utc)
            run.status = "failed" if search_error else "ok"
            run.error = search_error
            run.jobs_added = 0
            run.jobs_updated = 0
            return stats

        seen_in_batch: set[str] = set()
        for result in results:
            try:
                html = page_fetcher.fetch_for_result(result)
                extracted = llm_extractor.extract(result, html)
                if not extracted.is_real_job:
                    continue
                added, updated = _upsert_job(db, source, result, extracted, seen_in_batch)
                if added or updated:
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
