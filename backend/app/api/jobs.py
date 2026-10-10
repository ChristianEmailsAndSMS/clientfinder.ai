from datetime import datetime, timedelta, timezone
from fastapi import APIRouter, Depends, Query
from sqlalchemy import select, func, or_
from sqlalchemy.orm import Session

from ..db import get_db
from ..models import Job
from ..schemas import JobOut

router = APIRouter(prefix="/jobs", tags=["jobs"])


def _like_pattern(q: str) -> str:
    escaped = q.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
    return f"%{escaped}%"


@router.get("", response_model=list[JobOut])
def list_jobs(
    db: Session = Depends(get_db),
    q: str | None = Query(None, description="keyword search on title/company/snippet"),
    platform: str | None = None,
    type: str | None = None,
    min_pay: float | None = None,
    max_pay: float | None = None,
    pay_period: str | None = Query(None, description="hour, week, month, project, or year"),
    posted_within_days: int | None = Query(None, ge=1, le=365),
    limit: int = Query(50, ge=1, le=200),
    offset: int = Query(0, ge=0),
):
    # id breaks ties: durable scrapers stamp the same first_seen_at on a whole batch.
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
    # A single rate is stored on one side only ($75/hr → pay_min=75, pay_max=NULL).
    low = func.coalesce(Job.pay_min, Job.pay_max)
    high = func.coalesce(Job.pay_max, Job.pay_min)
    if min_pay is not None:
        stmt = stmt.where(low >= min_pay)
    if max_pay is not None:
        stmt = stmt.where(high <= max_pay)
    if pay_period:
        stmt = stmt.where(Job.pay_period == pay_period)
    if posted_within_days:
        cutoff = datetime.now(timezone.utc) - timedelta(days=posted_within_days)
        # first_seen_at is only a stand-in when the source never gave a post date.
        stmt = stmt.where(func.coalesce(Job.posted_at, Job.first_seen_at) >= cutoff)
    stmt = stmt.limit(limit).offset(offset)
    rows = db.scalars(stmt).all()
    return rows


def _grouped(rows) -> dict:
    return {str(key): count for key, count in rows if key is not None}


@router.get("/stats")
def job_stats(db: Session = Depends(get_db)) -> dict:
    total = db.scalar(select(func.count(Job.id))) or 0
    by_platform = _grouped(db.execute(select(Job.platform, func.count(Job.id)).group_by(Job.platform)).all())
    by_type = _grouped(db.execute(select(Job.type, func.count(Job.id)).group_by(Job.type)).all())
    return {"total": total, "by_platform": by_platform, "by_type": by_type}
