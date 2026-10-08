"""Customer-run live searches.

A customer types what they want, we run one Google search + extraction through the same pipeline the scheduler uses, store the
jobs in the shared database, and charge credits (our cost x CREDIT_MARKUP). The same search repeated within SEARCH_CACHE_HOURS is
answered from the database for free, so popular searches get cheaper for everyone.

Money rules: nothing is charged unless the search actually ran; the charge is one idempotent ledger entry (ref=usersearch:<id>)."""
from __future__ import annotations

import logging
import re
import math
from decimal import ROUND_CEILING, Decimal
from datetime import timedelta

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from . import credits
from .config import settings
from .db import session_scope
from .models import Job, User, UserSearch, UserSearchJob
from . import pricing
from .pricing import CREDIT_MARKUP
from .scrapers import pipeline, query_plan
from .scrapers.google_search import SearchError  # noqa: F401  (re-exported for callers)
from .security import utcnow
from .security_utils import redact

log = logging.getLogger(__name__)

SOURCE_KEY = "usersearch"
SOURCE_LABEL = "Customer searches"
SITES = ("twitter.com", "x.com", "reddit.com", "linkedin.com/posts", "indeed.com", "upwork.com")
FRESHNESS = ("d", "w", "m")
STALE_AFTER = timedelta(minutes=10)
# input, output tokens of a typical extraction, for estimates only (actual tokens are billed). Haiku 5.5's tokenizer makes ~30% more
# tokens than 4.5 did, and thinking at low effort adds some output.
TYPICAL_TOKENS = (2600, 900)


class SearchRefused(Exception):
    def __init__(self, status: int, message: str, **extra):
        super().__init__(message)
        self.status, self.message, self.extra = status, message, extra


# ---------- inputs ----------
def clean_query(raw: str) -> str:
    q = re.sub(r"\s+", " ", (raw or "")).strip()
    if re.search(r"[\x00-\x1f\x7f]", raw or ""):
        raise SearchRefused(422, "That search contains characters we can't use.")
    if not 3 <= len(q) <= 120:
        raise SearchRefused(422, "Type between 3 and 120 characters, for example: hiring email copywriter klaviyo")
    return q


def query_key(query: str, freshness: str, site: str | None) -> str:
    return f"{query.lower()}|{freshness}|{site or ''}"[:300]


def full_query(query: str, site: str | None) -> str:
    return f"{query} site:{site}" if site else query


# ---------- prices ----------
def our_search_cost_micro() -> int:
    """What one Google search costs US (plan price / searches), in micro-USD, rounded up."""
    return int((Decimal(str(settings.serpapi_cost_per_search_usd)) * credits.MICRO).to_integral_value(ROUND_CEILING))


def search_fee_micro() -> int:
    """What the customer pays for the search itself: our cost x CREDIT_MARKUP, rounded up."""
    return int((Decimal(str(settings.serpapi_cost_per_search_usd)) * credits.MICRO * Decimal(str(CREDIT_MARKUP))).to_integral_value(ROUND_CEILING))


def typical_extraction_micro() -> int:
    m = pricing.charge_micro(settings.extraction_model, *TYPICAL_TOKENS)
    return m if m is not None else 8_000


def estimated_user_price_micro() -> int:
    return search_fee_micro() + settings.user_search_results * typical_extraction_micro()


def estimated_user_price_usd() -> float:
    return estimated_user_price_micro() / credits.MICRO


def live_search_ready() -> tuple[bool, str]:
    if settings.dev_fixtures:
        return True, ""                       # local development only: fixtures, no real searches
    if pricing.rates(settings.extraction_model, 0) is None:
        return False, "Custom search is paused: the extraction model has no price configured, so it cannot be billed correctly."
    if not (settings.serpapi_api_key or settings.serper_api_key):
        return False, "Custom search is being switched on. Browsing the database works now."
    if not settings.anthropic_api_key:
        return False, "Custom search is being switched on. Browsing the database works now."
    return True, ""


# ---------- bookkeeping ----------
def expire_stale(db: Session) -> None:
    cutoff = utcnow() - STALE_AFTER
    for s in db.scalars(select(UserSearch).where(UserSearch.status.in_(("queued", "running")), UserSearch.created_at < cutoff)).all():
        s.status, s.error, s.finished_at = "failed", "Timed out. You were not charged.", utcnow()
    db.flush()          # sessions here do not autoflush: later "is anything running?" checks must see these changes


def find_cached(db: Session, key: str) -> UserSearch | None:
    return db.scalar(select(UserSearch).where(
        UserSearch.query_key == key, UserSearch.status == "done", UserSearch.cached.is_(False),
        UserSearch.finished_at >= utcnow() - timedelta(hours=settings.search_cache_hours),
    ).order_by(UserSearch.id.desc()).limit(1))


def job_ids_for(db: Session, search_id: int) -> list[int]:
    return list(db.scalars(select(UserSearchJob.job_id).where(UserSearchJob.search_id == search_id)))


def quote(db: Session, user: User, query: str, freshness: str, site: str | None) -> dict:
    """What a search would do and cost, without doing it."""
    q = clean_query(query)
    freshness, site = _check_options(freshness, site)
    ready, why = live_search_ready()
    cached = find_cached(db, query_key(q, freshness, site))
    price = 0 if cached else estimated_user_price_micro()
    return {"query": q, "ready": ready, "reason": why, "cached": bool(cached), "cached_results": cached.results_count if cached else 0,
            "price_usd": credits.micro_to_usd(price), "balance_usd": credits.micro_to_usd(user.balance_micro),
            "affordable": user.unlimited_credits or user.balance_micro >= price, "unlimited": user.unlimited_credits,
            "searches_left_today": max(0, daily_limit(user) - _used_today(db, user.id))}


def _check_options(freshness: str, site: str | None) -> tuple[str, str | None]:
    if freshness not in FRESHNESS:
        raise SearchRefused(422, "Choose a time window: past day, week or month.")
    if site and site not in SITES:
        raise SearchRefused(422, "That site is not supported.")
    return freshness, site or None


def daily_limit(user: User) -> int:
    return user.daily_search_limit if user.daily_search_limit else settings.user_searches_per_day


def _used_today(db: Session, user_id: int) -> int:
    return db.scalar(select(func.count(UserSearch.id)).where(
        UserSearch.user_id == user_id, UserSearch.cached.is_(False), UserSearch.status != "failed",
        UserSearch.created_at >= utcnow() - timedelta(hours=24))) or 0


# ---------- starting a search ----------
def start_search(db: Session, user: User, query: str, freshness: str, site: str | None) -> UserSearch:
    q = clean_query(query)
    freshness, site = _check_options(freshness, site)
    ready, why = live_search_ready()
    if not ready:
        raise SearchRefused(503, why)
    expire_stale(db)
    key = query_key(q, freshness, site)

    cached = find_cached(db, key)
    if cached:                                                         # free: answered from our database
        s = UserSearch(user_id=user.id, query=q, query_key=key, freshness=freshness, site=site, status="done", cached=True,
                       results_count=cached.results_count, new_jobs=0, cost_micro=0, finished_at=utcnow())
        db.add(s)
        db.flush()
        for jid in job_ids_for(db, cached.id):
            db.add(UserSearchJob(search_id=s.id, job_id=jid))
        return s

    if _used_today(db, user.id) >= daily_limit(user):
        raise SearchRefused(429, f"Daily limit of {daily_limit(user)} new searches reached. Repeat searches from the last "
                                 f"{settings.search_cache_hours} hours are still free.")
    if db.scalar(select(func.count(UserSearch.id)).where(UserSearch.user_id == user.id, UserSearch.status.in_(("queued", "running")))):
        raise SearchRefused(409, "You already have a search running. Wait for it to finish.")
    if (db.scalar(select(func.count(UserSearch.id)).where(UserSearch.status.in_(("queued", "running")))) or 0) >= settings.user_search_max_concurrent:
        raise SearchRefused(503, "Search is busy right now. Try again in a minute.")
    if query_plan.searches_used_this_month(db) >= settings.serpapi_monthly_budget:
        raise SearchRefused(503, "Custom search is paused for this month. Browsing the database still works.")
    need = estimated_user_price_micro()
    if not user.unlimited_credits and user.balance_micro < need:
        raise SearchRefused(402, "Not enough credit for a search.", needed_usd=credits.micro_to_usd(need),
                            balance_usd=credits.micro_to_usd(user.balance_micro))

    s = UserSearch(user_id=user.id, query=q, query_key=key, freshness=freshness, site=site, status="queued")
    db.add(s)
    db.flush()
    return s


# ---------- running it (background) ----------
def run_search(search_id: int) -> None:
    with session_scope() as db:
        s = db.get(UserSearch, search_id)
        if s is None or s.status != "queued":
            return
        s.status = "running"
        text, fresh, user_id = full_query(s.query, s.site), s.freshness, s.user_id
    try:
        stats = pipeline.run_pipeline_for_query(text, num=settings.user_search_results, freshness=fresh,
                                                source_key=SOURCE_KEY, source_label=SOURCE_LABEL)
        _finish(search_id, user_id, text, fresh, stats)
    except Exception as e:                                             # never leave a search 'running'
        log.exception("user search %s failed", search_id)
        with session_scope() as db:
            s = db.get(UserSearch, search_id)
            if s and s.status in ("queued", "running"):
                s.status, s.error, s.finished_at = "failed", redact(f"Search failed: {type(e).__name__}. You were not charged."), utcnow()


def _finish(search_id: int, user_id: int, text: str, fresh: str, stats: dict) -> None:
    with session_scope() as db:
        s = db.get(UserSearch, search_id)
        s.finished_at = utcnow()
        if stats.get("error") or not stats.get("searched") or stats.get("search_error"):
            s.status = "failed"
            s.error = redact(stats.get("search_error") or stats.get("error") or "Search did not run") + " You were not charged."
            return
        query_plan.log_search(db, query_plan.QuerySpec(text, fresh), results=stats.get("search_hits", 0), new_jobs=stats.get("added", 0))
        tin, tout = stats.get("tokens_in", 0), stats.get("tokens_out", 0)
        extraction = credits.usage_cost_micro(settings.extraction_model, tin, tout) if (tin or tout) else 0
        our_extraction = pricing.cost_micro(settings.extraction_model, tin, tout) if (tin or tout) else 0
        fee, our_cost = search_fee_micro(), our_search_cost_micro() + our_extraction
        # The customer pays at least CREDIT_MARKUP x what the search really cost us (the floor is belt and braces: the two parts
        # already round up individually).
        total = max(fee + extraction, math.ceil(our_cost * CREDIT_MARKUP))
        owner = db.get(User, user_id)
        if owner is not None and owner.unlimited_credits:             # the owner gave this account free searches: nothing is charged
            ids = sorted(set(stats.get("job_ids", [])))
            for jid in ids:
                db.add(UserSearchJob(search_id=search_id, job_id=jid))
            s.status, s.results_count, s.new_jobs, s.cost_micro = "done", len(ids), stats.get("added", 0), 0
            return
        credits.apply(db, user_id, -total, "usage", f"Search: {s.query}"[:200], ref=f"usersearch:{search_id}", allow_negative=True,
                      meta={"search_id": search_id, "fee_micro": fee, "extraction_micro": extraction, "our_cost_micro": our_cost,
                            "charged_micro": total, "tokens_in": tin, "tokens_out": tout, "model": settings.extraction_model})
        ids = sorted(set(stats.get("job_ids", [])))
        for jid in ids:
            db.add(UserSearchJob(search_id=search_id, job_id=jid))
        s.status, s.results_count, s.new_jobs, s.cost_micro = "done", len(ids), stats.get("added", 0), total
