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

# Advice, self-promotion, and careers indexes. "We're hiring" stays. "When I'm hiring, I look for" does not.
_SELF_PROMO = re.compile(r"^\s*\[?\s*(for hire|hire me)\b", re.I)
_CAREERS_INDEX = re.compile(r"^\s*(careers at|all open positions)\b", re.I)
_ADVICE = re.compile(
    r"\b(when i(?:'m| am) hiring|why you should hire|why i look for|what i look for when|the rise of the|"
    r"how to hire|how should \w+ hire|how i built|\d+ signs you(?:'re| are)? not)\b",
    re.I,
)
# Title only. A real job can say "how to become" in the description. These headlines are articles.
# "Hiring a creative strategist?" is a question about the work, not an opening. "Hiring a strategist for a brand" is.
_ADVICE_TITLE = re.compile(
    r"\b(?:how to become|checklist for hiring|how to get hired)\b"
    r"|^\s*hiring an?\b[^?\n]{0,80}\?",
    re.I,
)
# A company can be named "Funnel Builder" (or use that phrase as a tagline) and be hiring an
# engineer. Platform, backend, and fullstack titles are the same miss as "software engineer".
_ENGINEERING_ROLE = re.compile(
    r"\b(?:software|platform|fullstack|full[\s-]stack|backend|front[\s-]?end)\s+engineers?\b"
    r"|\bsoftware engineering\b",
    re.I,
)
# "I have 4+ years" under a #hiring tag is someone selling themselves. "We're hiring" plus that line is still a job.
_FIRST_PERSON_YEARS = re.compile(r"\bi have \d+\+?\s*years\b", re.I)
# "Hi, I'm a copywriter" is the same ad. "Hi, I'm a founder and we're hiring" is not, and neither is
# "Hi everyone, I'm hiring" (that line has no "a/an" before a role).
_FIRST_PERSON_INTRO = re.compile(
    r"\bhi[,!]?\s+i(?:'m| am)\s+(?:a|an)\s+(?:\w+\s+){0,3}?(?:copywriters?|writers?|freelancers?|marketers?|strategists?|builders?)\b",
    re.I,
)
_HIRING_SOMEONE = re.compile(
    r"\b(we(?:'re| are) hiring|we(?:'re| are) looking|looking for (?:a|someone)|now hiring|is hiring)\b",
    re.I,
)
# "is anyone hiring" is a person looking for work, often for a friend. "We're hiring" in the same post is an opening.
_JOB_SEEKER = re.compile(r"\bis anyone hiring\b", re.I)


_ROLE_STOPWORDS = frozenset({
    "hiring", "hire", "jobs", "job", "remote", "the", "and", "for", "with", "from", "a", "an",
})


def _role_needles(query: str) -> list[str]:
    """Role words from the Google query, without the site: scope or the word hiring."""
    text = re.sub(r"site:\S+", " ", query or "", flags=re.I)
    phrases = [p.strip().lower() for p in re.findall(r'"([^"]+)"', text)]
    phrases = [p for p in phrases if p and p not in {"hiring", "hire"}]
    if phrases:
        return phrases
    bare = re.sub(r'"', " ", text).lower()
    return [w for w in re.findall(r"[a-z0-9']+", bare) if w not in _ROLE_STOPWORDS and len(w) >= 4]


def not_a_hiring_post(title: str, snippet: str = "", query: str = "", url: str = "") -> bool:
    """True when the search result is commentary, a person advertising themselves, or a careers index."""
    title = title or ""
    blob = f"{title}\n{snippet or ''}"
    if _SELF_PROMO.search(title) or _CAREERS_INDEX.search(title) or _ADVICE.search(blob) or _ADVICE_TITLE.search(title):
        return True
    if (_FIRST_PERSON_YEARS.search(blob) or _FIRST_PERSON_INTRO.search(blob) or _JOB_SEEKER.search(blob)) and not _HIRING_SOMEONE.search(blob):
        return True
    # Title only. A real funnel-builder post can mention working with engineers in the body.
    if re.search(r"funnel builder", query or "", re.I) and _ENGINEERING_ROLE.search(title):
        return True
    # On Reddit the role often appears only in the related-posts snippet of a different thread.
    # LinkedIn titles are the author's name, so this check stays on reddit.com.
    if "reddit.com" in (url or "").lower():
        needles = _role_needles(query)
        if needles and not all(needle in title.lower() for needle in needles):
            return True
    return False


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
    if not_a_hiring_post(result.title, result.snippet, result.source_query, result.url):
        stats["skipped"] += 1
        _resolve_failure(db, result)
        return
    page, fetched = page_fetcher.fetch_page_for_result(result)
    extracted = llm_extractor.extract(result, page)
    _add_usage(stats, extracted.usage)
    if extracted.is_real_job and not_a_hiring_post(extracted.title or "", extracted.raw_snippet or "", result.source_query):
        extracted.is_real_job = False
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
            _add_usage(stats, e.usage)
        _record_failure(db, source.key, result, f"extraction: {e}")
    except Exception as e:
        log.exception("pipeline row failed: %s", e)
        stats["errors"] += 1
        _record_failure(db, source.key, result, f"{type(e).__name__}: {e}")


def _new_stats(query: str) -> dict:
    return {"query": query, "searched": False, "search_hits": 0, "extracted": 0, "added": 0, "updated": 0,
            "skipped": 0, "errors": 0, "tokens_in": 0, "tokens_out": 0, "job_ids": [], "calls": []}


def _add_usage(stats: dict, usage: dict | None) -> None:
    """Count one model call on its own. The long-prompt rate is per call, so calls must not be glued into one prompt."""
    if not usage:
        return
    tin, tout = int(usage.get("input_tokens") or 0), int(usage.get("output_tokens") or 0)
    stats["tokens_in"] += tin
    stats["tokens_out"] += tout
    stats.setdefault("calls", []).append((tin, tout))


def _live_ready(stats: dict, what: str) -> bool:
    if not settings.dev_fixtures and not settings.anthropic_api_key:
        log.error("ANTHROPIC_API_KEY not set; skipping %s rather than storing unextracted rows", what)
        stats["error"] = "ANTHROPIC_API_KEY not set"
        return False
    return True


def run_pipeline_for_query(query: str, num: int = 10, freshness: str | None = None, *, source_key: str | None = None,
                           source_label: str | None = None, exhausted: set[str] | None = None) -> dict:
    """Execute the full pipeline for one search query. Returns a stats dict.
    stats["searched"] is True only if a search was actually attempted (i.e. may have cost a credit)."""
    stats = _new_stats(query)
    if not _live_ready(stats, repr(query)):
        return stats
    stats["searched"] = True
    try:
        used: list[str] = []
        results = google_search.search(query, num=num, freshness=freshness, exhausted=exhausted, used=used)
        if used:
            stats["provider"] = used[-1]
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
    exhausted: set[str] = set()
    for q in queries:
        try:
            out.append(run_pipeline_for_query(q, num=num, exhausted=exhausted))
        except Exception as e:  # one bad query (HTTP error, quota) must not stop the rest
            log.exception("query %r failed", q)
            out.append({"query": q, "error": redact(e)})
    return out
