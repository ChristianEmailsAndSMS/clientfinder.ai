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
from ..models import FailedUrl, Job, Source, ScrapeRun
from ..schemas import SearchResult, ExtractedJob
from ..dedup import dedupe_hash
from ..tagging import set_tags
from ..config import settings
from . import google_search, page_fetcher, llm_extractor
from ..security_utils import redact, safe_job_url
from .common import clip
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
                seen_in_batch: set[str], fetched: bool = True) -> tuple[bool, bool, int | None]:
    """Returns (added, updated, job_id)."""
    # The model reads attacker-controlled pages: only ever store a plain http(s) link (never javascript:, data:, etc.)
    url = safe_job_url(extracted.apply_url) or safe_job_url(result.url)
    if url is None:
        return (False, False, None)
    h = result_hash(result)
    if h in seen_in_batch:
        return (False, False, None)
    seen_in_batch.add(h)
    existing = db.scalar(select(Job).where(Job.dedupe_hash == h))
    now = datetime.now(timezone.utc)
    if existing:
        existing.last_seen_at = now
        # Fill in anything we didn't have before
        for field in ("company_or_poster", "pay_text", "pay_min", "pay_max", "pay_period",
                      "type", "experience_level", "location", "remote", "description"):
            v = clip(getattr(extracted, field), field)
            if v is not None and getattr(existing, field) in (None, ""):
                setattr(existing, field, v)
        if extracted.skills and not existing.skills:
            existing.skills = extracted.skills
        set_tags(db, existing)
        return (False, True, existing.id)

    job = Job(
        dedupe_hash=h,
        source_url=url,
        platform=result.platform,
        title=clip(extracted.title or result.title, "title"),
        company_or_poster=clip(extracted.company_or_poster, "company_or_poster"),
        raw_snippet=extracted.raw_snippet or result.snippet,
        description=extracted.description,
        type=clip(extracted.type, "type"),
        pay_text=clip(extracted.pay_text, "pay_text"),
        pay_min=extracted.pay_min,
        pay_max=extracted.pay_max,
        pay_period=clip(extracted.pay_period, "pay_period"),
        experience_level=clip(extracted.experience_level, "experience_level"),
        location=clip(extracted.location, "location"),
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
        set_tags(db, job)
        sp.commit()
        return (True, False, job.id)
    except IntegrityError:
        sp.rollback()
        return (False, True, db.scalar(select(Job.id).where(Job.dedupe_hash == h)))


def _source_key(query: str) -> str:
    key = "google:" + query.lower().strip().replace(" ", "_").replace('"', "")
    if len(key) <= 64:
        return key
    return key[:55] + "_" + hashlib.sha1(key.encode()).hexdigest()[:8]


MAX_FAILURE_ATTEMPTS = 5   # after this many failures a URL is marked dead and no longer auto-requeued


def _record_failure(db: Session, source_key: str, result: SearchResult, error: str) -> None:
    h = result_hash(result)
    now = datetime.now(timezone.utc)
    fu = db.scalar(select(FailedUrl).where(FailedUrl.url_hash == h))
    if fu:
        fu.attempts = (fu.attempts or 0) + 1 if fu.status != "resolved" else 1
        fu.error, fu.last_failed_at = redact(error)[:1000], now
        fu.status = "dead" if fu.attempts >= MAX_FAILURE_ATTEMPTS else "pending"
        return
    db.add(FailedUrl(url_hash=h, url=result.url, title=result.title[:512], snippet=result.snippet,
                     platform=result.platform, source_key=source_key, source_query=result.source_query[:256],
                     error=redact(error)[:1000], attempts=1, status="pending", first_failed_at=now, last_failed_at=now))


def _resolve_failure(db: Session, result: SearchResult) -> None:
    fu = db.scalar(select(FailedUrl).where(FailedUrl.url_hash == result_hash(result)))
    if fu and fu.status != "resolved":
        fu.status = "resolved"


def _process(db: Session, source: Source, result: SearchResult, seen: set[str], stats: dict) -> None:
    h = result_hash(result)
    known = db.scalar(select(Job).where(Job.dedupe_hash == h))
    if known:  # already stored (maybe by a direct feed): no fetch, no Claude call
        if h not in seen:
            known.last_seen_at = datetime.now(timezone.utc)
            stats["updated"] += 1
            stats["job_ids"].append(known.id)
        seen.add(h)
        _resolve_failure(db, result)
        return
    page, fetched = page_fetcher.fetch_page_for_result(result)
    extracted = llm_extractor.extract(result, page)
    if extracted.usage:
        stats["tokens_in"] += extracted.usage.get("input_tokens", 0)
        stats["tokens_out"] += extracted.usage.get("output_tokens", 0)
    if not extracted.is_real_job:
        stats["skipped"] += 1
        _resolve_failure(db, result)
        return
    added, updated, job_id = _upsert_job(db, source, result, extracted, seen, fetched)
    if job_id is not None:
        stats["job_ids"].append(job_id)
    stats["extracted"] += 1
    stats["added"] += int(added)
    stats["updated"] += int(updated)
    _resolve_failure(db, result)


def _run_one(db: Session, source: Source, result: SearchResult, seen: set[str], stats: dict) -> None:
    """One result, isolated in a savepoint so a failure cannot poison the batch; failures are persisted."""
    try:
        with db.begin_nested():
            _process(db, source, result, seen, stats)
    except ExtractionError as e:
        log.warning("skipping %s: %s", result.url, e)
        stats["skipped"] += 1
        if e.usage:                                          # the model WAS called: those tokens cost money, so they are counted
            stats["tokens_in"] += e.usage.get("input_tokens", 0)
            stats["tokens_out"] += e.usage.get("output_tokens", 0)
        _record_failure(db, source.key, result, f"extraction: {e}")
    except Exception as e:
        log.exception("pipeline row failed: %s", e)
        stats["errors"] += 1
        _record_failure(db, source.key, result, f"{type(e).__name__}: {e}")


def _new_stats(query: str) -> dict:
    return {"query": query, "searched": False, "search_hits": 0, "extracted": 0, "added": 0, "updated": 0,
            "skipped": 0, "errors": 0, "tokens_in": 0, "tokens_out": 0, "job_ids": []}


def _live_ready(stats: dict, what: str) -> bool:
    if not settings.dev_fixtures and not settings.anthropic_api_key:
        log.error("ANTHROPIC_API_KEY not set; skipping %s rather than storing unextracted rows", what)
        stats["error"] = "ANTHROPIC_API_KEY not set"
        return False
    return True


def run_pipeline_for_query(query: str, num: int = 10, freshness: str | None = None, *, source_key: str | None = None,
                           source_label: str | None = None) -> dict:
    """Execute the full pipeline for one search query. Returns a stats dict.
    stats["searched"] is True only if a search was actually attempted (i.e. may have cost a credit)."""
    stats = _new_stats(query)
    if not _live_ready(stats, repr(query)):
        return stats
    stats["searched"] = True
    try:
        results = google_search.search(query, num=num, freshness=freshness)
    except Exception as e:
        log.exception("search failed for %r", query)
        stats["search_error"] = redact(e)
        return stats
    stats["search_hits"] = len(results)
    if not results:
        log.info("no search results for %r (freshness=%s)", query, freshness)
        return stats

    with session_scope() as db:
        source = _get_or_create_source(db, source_key or _source_key(query), "google_search", (source_label or f"Google: {query}")[:128])
        run = ScrapeRun(source_id=source.id, status="running")
        db.add(run)
        db.flush()
        seen: set[str] = set()
        for result in results:
            _run_one(db, source, result, seen, stats)
        run.finished_at = datetime.now(timezone.utc)
        run.status = "ok" if stats["errors"] == 0 else "failed"
        run.jobs_added = stats["added"]
        run.jobs_updated = stats["updated"]
        if stats["errors"]:
            run.error = f"{stats['errors']} rows failed; see failed_urls"
    log.info("query %r: %s", query, stats)
    return stats


def requeue_failed(limit: int = 50, ids: list[int] | None = None) -> dict:
    """Reprocess failed URLs (fetch + extract + store). Costs no search credit, only fetch/Claude.
    Without ids: pending rows, oldest failure first. With ids: those rows even if dead (one more try)."""
    stats = _new_stats("requeue")
    stats["requeued"] = 0
    if not _live_ready(stats, "requeue"):
        return stats
    with session_scope() as db:
        q = select(FailedUrl).order_by(FailedUrl.last_failed_at)
        q = q.where(FailedUrl.id.in_(ids), FailedUrl.status != "resolved") if ids else q.where(FailedUrl.status == "pending")
        rows = db.scalars(q.limit(limit)).all()
        seen: set[str] = set()
        for fu in rows:
            src = db.scalar(select(Source).where(Source.key == fu.source_key)) or _get_or_create_source(
                db, fu.source_key, "google_search", fu.source_key)
            result = SearchResult(url=fu.url, title=fu.title, snippet=fu.snippet, source_query=fu.source_query, platform=fu.platform)
            if fu.status == "dead":
                fu.status, fu.attempts = "pending", MAX_FAILURE_ATTEMPTS - 1   # one more try, then dead again
            stats["requeued"] += 1
            _run_one(db, src, result, seen, stats)
    log.info("requeue: %s", stats)
    return stats


def run_pipeline_for_queries(queries: Iterable[str] | None = None, num: int = 10) -> list[dict]:
    queries = list(queries) if queries else list(google_search.DEFAULT_QUERIES)
    out = []
    for q in queries:
        try:
            out.append(run_pipeline_for_query(q, num=num))
        except Exception as e:  # one bad query (HTTP error, quota) must not stop the rest
            log.exception("query %r failed", q)
            out.append({"query": q, "error": redact(e)})
    return out
