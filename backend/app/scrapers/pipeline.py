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
from sqlalchemy.exc import DataError, IntegrityError

from ..db import session_scope
from ..models import Job, Source, ScrapeRun
from ..schemas import SearchResult, ExtractedJob
from ..dedup import dedupe_hash
from ..config import settings
from . import google_search, page_fetcher, llm_extractor

log = logging.getLogger(__name__)

_ALLOWED_PERIODS = {"hour", "week", "month", "project", "year"}


def _clip(value: str | None, limit: int) -> str | None:
    if value is None:
        return None
    text = str(value).strip()
    if not text:
        return None
    return text[:limit]


def _google_source_key(query: str) -> str:
    slug = re.sub(r"[^a-z0-9_]+", "", re.sub(r"\s+", "_", query.lower().strip().strip("\"'")))
    key = f"google:{slug}"
    if len(key) <= 64:
        return key
    digest = hashlib.sha256(query.encode("utf-8")).hexdigest()[:8]
    return f"google:{slug[:48]}_{digest}"


def _get_or_create_source(db: Session, key: str, kind: str, display_name: str) -> Source:
    s = db.scalar(select(Source).where(Source.key == key))
    if s:
        return s
    s = Source(key=key[:64], kind=kind, display_name=display_name[:128], enabled=True, config={})
    db.add(s)
    db.flush()
    return s


def _upsert_job(db: Session, source: Source, result: SearchResult, extracted: ExtractedJob,
                seen_in_batch: set[str]) -> tuple[bool, bool]:
    """Returns (added, updated). Identity is the posting URL, not a shared apply link."""
    url = result.url
    h = dedupe_hash(url=url, title=extracted.title, company=extracted.company_or_poster)
    if h in seen_in_batch:
        return (False, False)
    seen_in_batch.add(h)
    existing = db.scalar(select(Job).where(Job.dedupe_hash == h))
    now = datetime.now(timezone.utc)
    if existing:
        existing.last_seen_at = now
        # Fill in anything we didn't have before
        clipped = {
            "company_or_poster": _clip(extracted.company_or_poster, 256),
            "pay_text": _clip(extracted.pay_text, 128),
            "pay_period": extracted.pay_period if extracted.pay_period in _ALLOWED_PERIODS else None,
            "type": _clip(extracted.type, 32),
            "experience_level": _clip(extracted.experience_level, 32),
            "location": _clip(extracted.location, 128),
            "description": extracted.description,
            "pay_min": extracted.pay_min,
            "pay_max": extracted.pay_max,
            "remote": extracted.remote,
        }
        for field, v in clipped.items():
            if v is not None and getattr(existing, field) in (None, ""):
                setattr(existing, field, v)
        if extracted.skills and not existing.skills:
            existing.skills = extracted.skills
        return (False, True)

    apply_url = extracted.apply_url if extracted.apply_url and extracted.apply_url != url else None
    job = Job(
        dedupe_hash=h,
        source_url=url,
        platform=_clip(result.platform, 64) or "web",
        title=_clip(extracted.title or result.title, 512) or "Unknown",
        company_or_poster=_clip(extracted.company_or_poster, 256),
        raw_snippet=extracted.raw_snippet or result.snippet,
        description=extracted.description,
        type=_clip(extracted.type, 32),
        pay_text=_clip(extracted.pay_text, 128),
        pay_min=extracted.pay_min,
        pay_max=extracted.pay_max,
        pay_period=extracted.pay_period if extracted.pay_period in _ALLOWED_PERIODS else None,
        experience_level=_clip(extracted.experience_level, 32),
        location=_clip(extracted.location, 128),
        remote=extracted.remote,
        skills=extracted.skills or None,
        posted_at=extracted.posted_at,
        first_seen_at=now,
        last_seen_at=now,
        source_key=source.key,
        is_real_job=extracted.is_real_job,
        extraction_model=_clip(extracted.extraction_model, 64) or "heuristic-mock",
        extra={"apply_url": apply_url} if apply_url else None,
    )
    sp = db.begin_nested()
    try:
        db.add(job)
        db.flush()
        sp.commit()
        return (True, False)
    except (IntegrityError, DataError):
        sp.rollback()
        raced = db.scalar(select(Job).where(Job.dedupe_hash == h))
        if raced:
            raced.last_seen_at = now
            return (False, True)
        return (False, False)


def run_pipeline_for_query(query: str, num: int = 20) -> dict:
    """Execute the full pipeline for one search query. Returns a stats dict."""
    stats = {"query": query, "search_hits": 0, "extracted": 0, "added": 0, "updated": 0, "errors": 0}
    try:
        results = google_search.search(query, num=num)
    except Exception as e:
        log.exception("search failed for %r", query)
        stats["errors"] = 1
        stats["error"] = str(e)
        return stats
    stats["search_hits"] = len(results)
    if not results:
        log.warning("no search results for %r (DEV_FIXTURES=%s)", query, settings.dev_fixtures)
        return stats

    source_key = _google_source_key(query)
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
