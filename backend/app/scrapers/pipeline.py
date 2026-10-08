"""Orchestrator: Google search → fetch → LLM extract → persist.
Idempotent — reruns upsert on dedupe_hash."""
from __future__ import annotations

import hashlib
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


def result_hash(result: SearchResult) -> str:
    """Identity of a job = the page we found it on, so direct feeds and Google agree and re-runs are free."""
    return dedupe_hash(url=result.url, title=result.title)


def _upsert_job(db: Session, source: Source, result: SearchResult, extracted: ExtractedJob,
                seen_in_batch: set[str], fetched: bool = True) -> tuple[bool, bool]:
    """Returns (added, updated)."""
    url = extracted.apply_url or result.url
    h = result_hash(result)
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
        extra={"usage": extracted.usage, "page_fetched": fetched},
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


def _source_key(query: str) -> str:
    key = "google:" + query.lower().strip().replace(" ", "_").replace('"', "")
    if len(key) <= 64:
        return key
    return key[:55] + "_" + hashlib.sha1(key.encode()).hexdigest()[:8]


def run_pipeline_for_query(query: str, num: int = 10, freshness: str | None = None) -> dict:
    """Execute the full pipeline for one search query. Returns a stats dict.
    stats["searched"] is True only if a search was actually attempted (i.e. may have cost a credit)."""
    stats = {"query": query, "searched": False, "search_hits": 0, "extracted": 0, "added": 0, "updated": 0,
             "skipped": 0, "errors": 0, "tokens_in": 0, "tokens_out": 0}
    if not settings.dev_fixtures and not settings.anthropic_api_key:
        log.error("ANTHROPIC_API_KEY not set; skipping %r rather than storing unextracted rows", query)
        stats["error"] = "ANTHROPIC_API_KEY not set"
        return stats
    stats["searched"] = True
    try:
        results = google_search.search(query, num=num, freshness=freshness)
    except Exception as e:
        log.exception("search failed for %r", query)
        stats["search_error"] = str(e)
        return stats
    stats["search_hits"] = len(results)
    if not results:
        log.info("no search results for %r (freshness=%s)", query, freshness)
        return stats

    with session_scope() as db:
        source = _get_or_create_source(db, _source_key(query), "google_search", f"Google: {query}"[:128])
        run = ScrapeRun(source_id=source.id, status="running")
        db.add(run)
        db.flush()

        seen_in_batch: set[str] = set()
        now = datetime.now(timezone.utc)
        for result in results:
            try:
                h = result_hash(result)
                known = db.scalar(select(Job).where(Job.dedupe_hash == h))
                if known:  # already stored (maybe by a direct feed): no fetch, no Claude call
                    if h not in seen_in_batch:
                        known.last_seen_at = now
                        stats["updated"] += 1
                    seen_in_batch.add(h)
                    continue
                html, fetched = page_fetcher.fetch_page_for_result(result)
                extracted = llm_extractor.extract(result, html)
                if extracted.usage:
                    stats["tokens_in"] += extracted.usage.get("input_tokens", 0)
                    stats["tokens_out"] += extracted.usage.get("output_tokens", 0)
                if not extracted.is_real_job:
                    stats["skipped"] += 1
                    continue
                added, updated = _upsert_job(db, source, result, extracted, seen_in_batch, fetched)
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
    log.info("query %r: %s", query, stats)
    return stats


def run_pipeline_for_queries(queries: Iterable[str] | None = None, num: int = 10) -> list[dict]:
    queries = list(queries) if queries else list(google_search.DEFAULT_QUERIES)
    out = []
    for q in queries:
        try:
            out.append(run_pipeline_for_query(q, num=num))
        except Exception as e:  # one bad query (HTTP error, quota) must not stop the rest
            log.exception("query %r failed", q)
            out.append({"query": q, "error": str(e)})
    return out
