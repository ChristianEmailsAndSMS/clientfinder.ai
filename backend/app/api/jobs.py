from datetime import datetime, timedelta, timezone
from fastapi import APIRouter, Depends, Query
from sqlalchemy import select, func, or_
from sqlalchemy.orm import Session

from ..db import get_db
from ..models import Job
from ..schemas import JobOut

router = APIRouter(prefix="/jobs", tags=["jobs"])


def _like_pattern(text: str) -> str:
    """Wrap user text for ILIKE. Escape \\, %, and _ so they match literally."""
    escaped = text.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
    return f"%{escaped}%"


def _group_counts(rows) -> dict[str, int]:
    """Turn grouped (key, count) rows into a JSON object.

    NULL keys are not valid JSON object keys. Keep the count under "unknown",
    and add it to an existing "unknown" bucket instead of dropping it.
    """
    counts: dict[str, int] = {}
    for key, count in rows:
        label = "unknown" if key is None else key
        counts[label] = counts.get(label, 0) + int(count)
    return counts


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
        like = _like_pattern(q)
        stmt = stmt.where(
            or_(
                Job.title.ilike(like, escape="\\"),
                Job.company_or_poster.ilike(like, escape="\\"),
                Job.raw_snippet.ilike(like, escape="\\"),
            )
        )
    if platform:
        stmt = stmt.where(Job.platform == platform)
    if type:
        stmt = stmt.where(Job.type == type)
    # Missing bound equals the known one. Both NULL stays NULL and fails the comparison.
    if min_pay is not None:
        effective_max = func.coalesce(Job.pay_max, Job.pay_min)
        stmt = stmt.where(effective_max >= min_pay)
    if max_pay is not None:
        effective_min = func.coalesce(Job.pay_min, Job.pay_max)
        stmt = stmt.where(effective_min <= max_pay)
    if posted_within_days:
        cutoff = datetime.now(timezone.utc) - timedelta(days=posted_within_days)
        stmt = stmt.where(or_(Job.posted_at >= cutoff, Job.first_seen_at >= cutoff))
    stmt = stmt.limit(limit).offset(offset)
    rows = db.scalars(stmt).all()
    return rows


@router.get("/stats")
def job_stats(db: Session = Depends(get_db)) -> dict:
    total = db.scalar(select(func.count(Job.id))) or 0
    by_platform = _group_counts(db.execute(select(Job.platform, func.count(Job.id)).group_by(Job.platform)).all())
    by_type = _group_counts(db.execute(select(Job.type, func.count(Job.id)).group_by(Job.type)).all())
    return {"total": total, "by_platform": by_platform, "by_type": by_type}
