"""Orchestrator: Google search → fetch → LLM extract → persist.
Idempotent — reruns upsert on dedupe_hash."""
from __future__ import annotations

import logging
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
from .llm_extractor import ExtractionError

log = logging.getLogger(__name__)


def _get_or_create_source(db: Session, key: str, kind: str, display_name: str) -> Source:
    s = db.scalar(select(Source).where(Source.key == key))
    if s:
        return s
    s = Source(key=key, kind=kind, display_name=display_name, enabled=True, config={})
    db.add(s)
    db.flush()
    return s


def _upsert_job(db: Session, source: Source, result: SearchResult, extracted: ExtractedJob,
                seen_in_batch: set[str]) -> tuple[bool, bool]:
    """Returns (added, updated)."""
    url = extracted.apply_url or result.url
    h = dedupe_hash(url=url, title=extracted.title, company=extracted.company_or_poster)
    if h in seen_in_batch:
        return (False, False)
    seen_in_batch.add(h)
    existing = db.scalar(select(Job).where(Job.dedupe_hash == h))
    now = datetime.now(timezone.utc)
    if existing:
        existing.last_seen_at = now
        # Fill in anything we didn't have before
        for field in ("company_or_poster", "pay_text", "pay_min", "pay_max", "pay_period",
                      "type", "experience_level", "location", "remote", "description"):
            v = getattr(extracted, field)
            if v is not None and getattr(existing, field) in (None, ""):
                setattr(existing, field, v)
        if extracted.skills and not existing.skills:
            existing.skills = extracted.skills
        return (False, True)

    job = Job(
        dedupe_hash=h,
        source_url=url,
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
        extra={"usage": extracted.usage} if extracted.usage else None,
    )
    sp = db.begin_nested()
    try:
        db.add(job)
        db.flush()
        sp.commit()
        return (True, False)
    except IntegrityError:
        sp.rollback()
        return (False, True)


def run_pipeline_for_query(query: str, num: int = 20) -> dict:
    """Execute the full pipeline for one search query. Returns a stats dict."""
    stats = {"query": query, "search_hits": 0, "extracted": 0, "added": 0, "updated": 0, "errors": 0, "skipped": 0}
    if not settings.dev_fixtures and not settings.anthropic_api_key:
        log.error("ANTHROPIC_API_KEY not set; skipping %r rather than storing unextracted rows", query)
        stats["error"] = "ANTHROPIC_API_KEY not set"
        return stats
    results = google_search.search(query, num=num)
    stats["search_hits"] = len(results)
    if not results:
        log.warning("no search results for %r (DEV_FIXTURES=%s)", query, settings.dev_fixtures)
        return stats

    source_key = f"google:{query.lower().strip().replace(' ', '_').strip(chr(34))}"
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
            except ExtractionError as e:
                log.warning("skipping %s: %s", result.url, e)
                stats["skipped"] += 1
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
    out = []
    for q in queries:
        try:
            out.append(run_pipeline_for_query(q, num=num))
        except Exception as e:  # one bad query (HTTP error, quota) must not stop the rest
            log.exception("query %r failed", q)
            out.append({"query": q, "error": str(e)})
    return out
