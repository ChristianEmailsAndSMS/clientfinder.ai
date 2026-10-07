from datetime import datetime, timedelta, timezone
from fastapi import APIRouter, Depends, Query
from sqlalchemy import select, func, or_
from sqlalchemy.orm import Session

from ..db import get_db
from ..models import Job
from ..schemas import JobOut

router = APIRouter(prefix="/jobs", tags=["jobs"])


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
    stmt = select(Job).order_by(Job.first_seen_at.desc())
    if q:
        like = f"%{q}%"
        stmt = stmt.where(or_(Job.title.ilike(like), Job.company_or_poster.ilike(like), Job.raw_snippet.ilike(like)))
    if platform:
        stmt = stmt.where(Job.platform == platform)
    if type:
        stmt = stmt.where(Job.type == type)
    if min_pay is not None:
        stmt = stmt.where(Job.pay_max >= min_pay)
    if max_pay is not None:
        stmt = stmt.where(Job.pay_min <= max_pay)
    if posted_within_days:
        cutoff = datetime.now(timezone.utc) - timedelta(days=posted_within_days)
        stmt = stmt.where(or_(Job.posted_at >= cutoff, Job.first_seen_at >= cutoff))
    stmt = stmt.limit(limit).offset(offset)
    rows = db.scalars(stmt).all()
    return rows


@router.get("/stats")
def job_stats(db: Session = Depends(get_db)) -> dict:
    total = db.scalar(select(func.count(Job.id))) or 0
    by_platform = dict(db.execute(select(Job.platform, func.count(Job.id)).group_by(Job.platform)).all())
    by_type = dict(db.execute(select(Job.type, func.count(Job.id)).group_by(Job.type)).all())
    return {"total": total, "by_platform": by_platform, "by_type": by_type}
