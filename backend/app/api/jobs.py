from datetime import datetime, timedelta, timezone

from fastapi import APIRouter, Depends, Query, Response
from sqlalchemy import func, or_, select
from sqlalchemy.orm import Session

from .. import geo, platforms
from ..db import get_db
from ..models import Job, JobTag
from ..schemas import JobOut
from ..security import public_guard
from ..tagging import tag_counts

router = APIRouter(prefix="/jobs", tags=["jobs"], dependencies=[Depends(public_guard)])

SORTS = {
    "newest": lambda: (Job.first_seen_at.desc(), Job.id.desc()),
    "oldest": lambda: (Job.first_seen_at.asc(), Job.id.asc()),
    "pay_high": lambda: (Job.pay_max.desc().nulls_last(), Job.first_seen_at.desc()),
    "pay_low": lambda: (Job.pay_min.asc().nulls_last(), Job.first_seen_at.desc()),
}


def _like(q: str) -> str:
    """Escape LIKE wildcards so a visitor's '%' or '_' is matched literally instead of matching everything."""
    return "%" + q.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_") + "%"


def serialize_jobs(db: Session, rows) -> list[JobOut]:
    tags_by_job: dict[int, list[str]] = {}
    if rows:
        for job_id, t in db.execute(select(JobTag.job_id, JobTag.tag).where(JobTag.job_id.in_([r.id for r in rows])).order_by(JobTag.tag)):
            tags_by_job.setdefault(job_id, []).append(t)
    out = []
    for r in rows:
        item = JobOut.model_validate(r)
        item.tags = tags_by_job.get(r.id, [])
        out.append(item)
    return out


@router.get("", response_model=list[JobOut])
def list_jobs(
    response: Response,
    db: Session = Depends(get_db),
    q: str | None = Query(None, max_length=100, description="keyword search on title/company/snippet"),
    platform: list[str] | None = Query(None, description="repeatable; any of these platforms"),
    type: list[str] | None = Query(None, description="repeatable; any of these job types"),
    pay_period: str | None = Query(None, pattern="^(hour|year|project)$"),
    min_pay: float | None = Query(None, ge=0, le=10_000_000),
    max_pay: float | None = Query(None, ge=0, le=10_000_000),
    has_pay: bool | None = None,
    remote: bool | None = None,
    tag: list[str] | None = Query(None, description="repeatable; a job must have ALL given tags"),
    any_tag: list[str] | None = Query(None, description="repeatable; a job must have AT LEAST ONE of these tags"),
    kind: list[str] | None = Query(None, description="repeatable: board | social | web"),
    region: list[str] | None = Query(None, description="repeatable time-zone bands (see /jobs/facets); 'unspecified' = no place stated"),
    include_worldwide: bool = Query(True, description="with region: also show jobs that say worldwide / anywhere"),
    location: str | None = Query(None, max_length=60, description="text in the job's location, e.g. a city or country"),
    posted_within_days: int | None = Query(None, ge=1, le=365),
    sort: str = Query("newest", pattern="^(newest|oldest|pay_high|pay_low)$"),
    limit: int = Query(30, ge=1, le=100),
    offset: int = Query(0, ge=0, le=10_000),
):
    conds = [Job.is_real_job.is_(True)]
    if q and q.strip():
        like = _like(q.strip())
        conds.append(or_(Job.title.ilike(like, escape="\\"), Job.company_or_poster.ilike(like, escape="\\"),
                         Job.raw_snippet.ilike(like, escape="\\")))
    if platform:
        conds.append(Job.platform.in_(platform))
    if type:
        conds.append(Job.type.in_(type))
    if kind:
        known = [p for (p,) in db.execute(select(Job.platform).distinct())]
        allowed = [p for k in kind if k in platforms.KIND_LABEL for p in platforms.platforms_of(k, known)]
        conds.append(Job.platform.in_(allowed or [""]))
    if region:
        wanted = [r for r in region if r in geo.REGION_KEYS]
        if include_worldwide and wanted and wanted != ["unspecified"]:
            wanted.append("worldwide")
        parts = [Job.region.is_(None) if r == "unspecified" else Job.region.like(f"%{r}%") for r in dict.fromkeys(wanted)]
        conds.append(or_(*parts) if parts else Job.id < 0)
    if location and location.strip():
        conds.append(Job.location.ilike(_like(location.strip()), escape="\\"))
    if pay_period:
        conds.append(Job.pay_period == pay_period)
    if min_pay is not None:
        conds.append(Job.pay_max >= min_pay)
    if max_pay is not None:
        conds.append(Job.pay_min <= max_pay)
    if has_pay:
        conds.append(or_(Job.pay_min.is_not(None), Job.pay_max.is_not(None), Job.pay_text.is_not(None)))
    if remote is not None:
        conds.append(Job.remote.is_(True) if remote else or_(Job.remote.is_(False), Job.remote.is_(None)))
    if posted_within_days:
        cutoff = datetime.now(timezone.utc) - timedelta(days=posted_within_days)
        conds.append(or_(Job.posted_at >= cutoff, (Job.posted_at.is_(None)) & (Job.first_seen_at >= cutoff)))
    if any_tag:
        conds.append(Job.id.in_(select(JobTag.job_id).where(JobTag.tag.in_([t.lower() for t in any_tag]))))
    for t in tag or []:
        conds.append(Job.id.in_(select(JobTag.job_id).where(JobTag.tag == t.lower())))

    response.headers["X-Total-Count"] = str(db.scalar(select(func.count(Job.id)).where(*conds)) or 0)
    rows = db.scalars(select(Job).where(*conds).order_by(*SORTS[sort]()).limit(limit).offset(offset)).all()
    return serialize_jobs(db, rows)


@router.get("/facets")
def facets(db: Session = Depends(get_db)) -> dict:
    """Everything the filter sidebar needs in one call: value + count for each filter."""
    real = Job.is_real_job.is_(True)
    def counts(col):
        return [{"value": v, "count": c} for v, c in db.execute(
            select(col, func.count(Job.id)).where(real, col.is_not(None)).group_by(col).order_by(func.count(Job.id).desc())).all()]
    plats = counts(Job.platform)
    for p in plats:
        p["kind"] = platforms.kind_of(p["value"])
    kinds: dict[str, int] = {}
    for p in plats:
        kinds[p["kind"]] = kinds.get(p["kind"], 0) + p["count"]
    region_counts: dict[str, int] = {}
    unspecified = 0
    for r, c in db.execute(select(Job.region, func.count(Job.id)).where(real).group_by(Job.region)).all():
        if not r:
            unspecified += c
        for k in geo.split(r):
            region_counts[k] = region_counts.get(k, 0) + c
    return {
        "total": db.scalar(select(func.count(Job.id)).where(real)) or 0,
        "platforms": plats,
        "kinds": [{"value": k, "label": platforms.KIND_LABEL[k], "count": kinds.get(k, 0)} for k in ("board", "social", "web") if kinds.get(k)],
        "regions": [{"value": k, "label": lbl, "offset": off, "count": region_counts.get(k, 0)} for k, (lbl, off) in geo.BANDS.items()]
                   + [{"value": "unspecified", "label": "Location not stated", "offset": "", "count": unspecified}],
        "types": counts(Job.type),
        "pay_periods": counts(Job.pay_period),
        "tags": [{"value": t, "count": c} for t, c in tag_counts(db)],
        "newest_at": (db.scalar(select(func.max(Job.first_seen_at)).where(real)) or datetime.now(timezone.utc)).isoformat(),
    }


@router.get("/tags")
def job_tags(db: Session = Depends(get_db)) -> list[dict]:
    """Tag counts for building filters, most common first."""
    return [{"tag": t, "count": c} for t, c in tag_counts(db)]


@router.get("/stats")
def job_stats(db: Session = Depends(get_db)) -> dict:
    total = db.scalar(select(func.count(Job.id))) or 0
    by_platform = dict(db.execute(select(Job.platform, func.count(Job.id)).group_by(Job.platform)).all())
    by_type = dict(db.execute(select(Job.type, func.count(Job.id)).group_by(Job.type)).all())
    return {"total": total, "by_platform": by_platform, "by_type": by_type}
