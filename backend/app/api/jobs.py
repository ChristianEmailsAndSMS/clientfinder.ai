from datetime import datetime, timedelta, timezone
from fastapi import APIRouter, Depends, Query
from sqlalchemy import func, or_, select
from sqlalchemy.orm import Session

from ..db import get_db
from ..models import Job
from ..schemas import JobOut

router = APIRouter(prefix="/jobs", tags=["jobs"])


def _like_pattern(q: str) -> str:
    escaped = q.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
    return f"%{escaped}%"


def filtered_jobs_stmt(
    *,
    q: str | None = None,
    platform: str | None = None,
    type: str | None = None,
    min_pay: float | None = None,
    max_pay: float | None = None,
    posted_within_days: int | None = None,
    limit: int = 50,
    offset: int = 0,
):
    stmt = select(Job).order_by(Job.first_seen_at.desc(), Job.id.desc())
    if q:
        like = _like_pattern(q)
        stmt = stmt.where(or_(
            Job.title.ilike(like, escape="\\"),
            Job.company_or_poster.ilike(like, escape="\\"),
            Job.raw_snippet.ilike(like, escape="\\"),
        ))
    if platform:
        stmt = stmt.where(Job.platform == platform)
    if type:
        stmt = stmt.where(Job.type == type)
    # A single extracted amount is stored on one side. Treat the missing side as that amount.
    if min_pay is not None:
        stmt = stmt.where(func.coalesce(Job.pay_max, Job.pay_min) >= min_pay)
    if max_pay is not None:
        stmt = stmt.where(func.coalesce(Job.pay_min, Job.pay_max) <= max_pay)
    if posted_within_days:
        cutoff = datetime.now(timezone.utc) - timedelta(days=posted_within_days)
        # first_seen_at is when we scraped the row, so it must not keep an old post in the window.
        stmt = stmt.where(func.coalesce(Job.posted_at, Job.first_seen_at) >= cutoff)
    return stmt.limit(limit).offset(offset)


@router.get("", response_model=list[JobOut])
def list_jobs(
    db: Session = Depends(get_db),
    q: str | None = Query(None, description="keyword search on title/company/snippet"),
    platform: str | None = None,
    type: str | None = None,
    min_pay: float | None = None,
    max_pay: float | None = None,
    posted_within_days: int | None = Query(None, ge=1, le=365),
    limit: int = Query(50, ge=1, le=200),
    offset: int = Query(0, ge=0),
):
    rows = db.scalars(filtered_jobs_stmt(
        q=q,
        platform=platform,
        type=type,
        min_pay=min_pay,
        max_pay=max_pay,
        posted_within_days=posted_within_days,
        limit=limit,
        offset=offset,
    )).all()
    return rows


def _counts(rows) -> dict:
    return {key: count for key, count in rows if key is not None}


@router.get("/stats")
def job_stats(db: Session = Depends(get_db)) -> dict:
    total = db.scalar(select(func.count(Job.id))) or 0
    by_platform = _counts(db.execute(select(Job.platform, func.count(Job.id)).group_by(Job.platform)).all())
    by_type = _counts(db.execute(select(Job.type, func.count(Job.id)).group_by(Job.type)).all())
    return {"total": total, "by_platform": by_platform, "by_type": by_type}
