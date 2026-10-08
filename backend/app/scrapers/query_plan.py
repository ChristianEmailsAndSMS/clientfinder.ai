"""What the Google layer searches, how often, and within what budget.

Plan = base phrases (open web) + the strongest phrases scoped to the sites where hiring posts live.
Each query re-runs every GOOGLE_QUERY_CYCLE_HOURS with a past-24h filter (a never-run query backfills a
week first). Every search is logged in `search_queries`; that log decides what is due and counts the
monthly spend, so a restart can never double-spend or blow the SerpAPI budget."""
from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from ..config import settings
from ..db import session_scope
from ..models import SearchQuery

log = logging.getLogger(__name__)

BASE_PHRASES: tuple[str, ...] = (
    '"hiring copywriter"',
    '"hiring email copywriter"',
    '"hiring email marketer"',
    '"hiring creative strategist"',
    '"hiring creative director"',
    '"hiring landing page builder"',
    '"hiring funnel builder"',
    '"looking for a copywriter"',
    '"need a copywriter"',
    '"copywriter wanted"',
)
# Only the highest-yield phrases get site scopes; scoping all of them multiplies cost for little gain.
SCOPED_PHRASES: tuple[str, ...] = (
    '"hiring copywriter"', '"hiring email marketer"', '"looking for a copywriter"', '"need a copywriter"',
)
SITE_SCOPES: tuple[str, ...] = (
    "twitter.com", "x.com", "reddit.com", "linkedin.com/posts", "indeed.com", "upwork.com",
)
ERROR_RETRY_AFTER = timedelta(hours=1)


@dataclass(frozen=True)
class QuerySpec:
    text: str          # exactly what is sent to Google
    freshness: str     # d | w | m

    @property
    def key(self) -> str:
        return self.text


def build_plan() -> list[QuerySpec]:
    texts = list(BASE_PHRASES)
    texts += [f"{p} site:{site}" for site in SITE_SCOPES for p in SCOPED_PHRASES]
    return [QuerySpec(t, "d") for t in texts]


def _provider() -> str:
    return "serper" if (settings.google_search_provider == "serper" and settings.serper_api_key) else "serpapi"


def month_start(now: datetime) -> datetime:
    return now.replace(day=1, hour=0, minute=0, second=0, microsecond=0)


def searches_used_this_month(db: Session, now: datetime | None = None) -> int:
    now = now or datetime.now(timezone.utc)
    return db.scalar(
        select(func.count(SearchQuery.id)).where(
            SearchQuery.provider == _provider(), SearchQuery.ran_at >= month_start(now), SearchQuery.error.is_(None)
        )
    ) or 0


def _last_attempts(db: Session) -> dict[str, SearchQuery]:
    latest = select(SearchQuery.query_key, func.max(SearchQuery.ran_at).label("m")).group_by(SearchQuery.query_key).subquery()
    rows = db.scalars(
        select(SearchQuery).join(latest, (SearchQuery.query_key == latest.c.query_key) & (SearchQuery.ran_at == latest.c.m))
    ).all()
    return {r.query_key: r for r in rows}


def _aware(dt: datetime) -> datetime:
    return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)


def due_queries(db: Session, plan: list[QuerySpec] | None = None, now: datetime | None = None,
                limit: int | None = None) -> list[QuerySpec]:
    """Never-run queries first, then oldest-run first. Errored queries retry after an hour; ok ones after the cycle.
    A never-run query backfills with a week of results instead of a day."""
    plan = plan or build_plan()
    now = now or datetime.now(timezone.utc)
    cycle = timedelta(hours=settings.google_query_cycle_hours)
    last = _last_attempts(db)
    due: list[tuple[datetime, QuerySpec]] = []
    for spec in plan:
        prev = last.get(spec.key)
        if prev is None:
            due.append((datetime.min.replace(tzinfo=timezone.utc), QuerySpec(spec.text, "w")))
            continue
        wait = ERROR_RETRY_AFTER if prev.error else cycle
        if _aware(prev.ran_at) + wait <= now:
            due.append((_aware(prev.ran_at), spec))
    due.sort(key=lambda t: t[0])
    out = [s for _, s in due]
    return out[:limit] if limit else out


def log_search(db: Session, spec: QuerySpec, *, results: int = 0, new_jobs: int = 0, error: str | None = None) -> None:
    db.add(SearchQuery(query_key=spec.key, provider=_provider(), freshness=spec.freshness,
                       results=results, new_jobs=new_jobs, error=error[:1000] if error else None))


def run_due_queries(max_queries: int | None = None) -> list[dict]:
    """One scheduler tick: run due queries, within the per-tick cap and the monthly budget."""
    from . import pipeline  # late import: pipeline imports this module's helpers too

    max_queries = max_queries or settings.google_max_queries_per_tick
    with session_scope() as db:
        used = searches_used_this_month(db)
        remaining = settings.serpapi_monthly_budget - used
        if remaining <= 0:
            log.warning("search budget exhausted (%d/%d this month); skipping tick", used, settings.serpapi_monthly_budget)
            return []
        todo = due_queries(db, limit=min(max_queries, remaining))
    log.info("google tick: %d due (budget used %d/%d)", len(todo), used, settings.serpapi_monthly_budget)

    out: list[dict] = []
    for spec in todo:
        stats = pipeline.run_pipeline_for_query(spec.text, freshness=spec.freshness)
        if not stats.get("searched"):  # preflight refused (e.g. no Anthropic key): nothing was spent, log nothing
            out.append(stats)
            continue
        with session_scope() as db:
            log_search(db, spec, results=stats.get("search_hits", 0), new_jobs=stats.get("added", 0),
                       error=stats.get("search_error"))
        out.append(stats)
    return out
