from datetime import datetime, timedelta, timezone

from fastapi import APIRouter, Depends, HTTPException, Query, Response
from sqlalchemy import and_, case, func, or_, select
from sqlalchemy.orm import Session

from .. import geo, platforms
from ..dates import MIN_POSTED, latest_posted
from ..db import get_db
from ..models import Job, JobTag
from ..schemas import JobOut
from ..security import public_guard
from ..tagging import tag_counts

router = APIRouter(prefix="/jobs", tags=["jobs"], dependencies=[Depends(public_guard)])

HOURS_PER_YEAR = 2080            # 40 h x 52 weeks: the usual convention for putting an hourly rate next to a salary


def _yearly(amount):
    """A pay figure on a common per-year scale so $50/hour and $90,000/year can be ordered. Per-project pay and jobs with no
    period have no yearly equivalent: they get NULL and sort last (never mixed in as if a $500 project were $500 a year)."""
    return case((Job.pay_period == "hour", amount * HOURS_PER_YEAR), (Job.pay_period == "year", amount), else_=None)


# "$50+/hr" has only a minimum and "up to $40/hr" only a maximum, so each side falls back to the figure that exists.
_PAY_TOP = func.coalesce(Job.pay_max, Job.pay_min)
_PAY_BOTTOM = func.coalesce(Job.pay_min, Job.pay_max)

# The card shows posted_at (falling back to first_seen_at) and the "Posted" filter uses the same date, so "Newest first" must sort by it too.
# first_seen_at alone put a 40-day-old post that we scraped today above one posted yesterday.
def _shown_date():
    """posted_at when it is believable (see dates.py), else when we first saw the post."""
    return case((and_(Job.posted_at >= MIN_POSTED, Job.posted_at <= latest_posted()), Job.posted_at), else_=Job.first_seen_at)


SORTS = {
    "newest": lambda: (_shown_date().desc(), Job.id.desc()),
    "oldest": lambda: (_shown_date().asc(), Job.id.asc()),
    "pay_high": lambda: (_yearly(_PAY_TOP).desc().nulls_last(), Job.first_seen_at.desc(), Job.id.desc()),
    "pay_low": lambda: (_yearly(_PAY_BOTTOM).asc().nulls_last(), Job.first_seen_at.desc(), Job.id.desc()),
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
    include_unspecified: bool = Query(True, description="with region: also show jobs that do not say where (many social posts)"),
    location: str | None = Query(None, max_length=60, description="text in the job's location, e.g. a city or country"),
    posted_within_days: int | None = Query(None, ge=1, le=365),
    sort: str = Query("newest", pattern="^(newest|oldest|pay_high|pay_low)$"),
    limit: int = Query(30, ge=1, le=100),
    offset: int = Query(0, ge=0, le=10_000),
):
    for val in [q, location, *(platform or []), *(type or []), *(tag or []), *(any_tag or []), *(kind or []), *(region or [])]:
        if val and "\x00" in val:                   # PostgreSQL rejects NUL in text: that was a 500 (SQLite let it through, so tests never saw it)
            raise HTTPException(422, "Search text contains characters we cannot use")
    if (min_pay is not None or max_pay is not None) and not pay_period:
        # $40 an hour and $40,000 a year are not comparable numbers: make the caller say which one they mean.
        raise HTTPException(422, "Choose a pay period (hour, year or project) to filter by minimum or maximum pay")
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
        if include_unspecified and wanted:
            wanted.append("unspecified")           # a post that names no place could be anywhere: show it rather than hide it
        parts = [or_(Job.region.is_(None), Job.region == "") if r == "unspecified" else Job.region.like(f"%{r}%") for r in dict.fromkeys(wanted)]
        conds.append(or_(*parts) if parts else Job.id < 0)
    if location and location.strip():
        conds.append(Job.location.ilike(_like(location.strip()), escape="\\"))
    if pay_period:
        conds.append(Job.pay_period == pay_period)
    if min_pay is not None:
        conds.append(_PAY_TOP >= min_pay)
    if max_pay is not None:
        conds.append(_PAY_BOTTOM <= max_pay)
    if has_pay:
        conds.append(Job.id.in_(select(JobTag.job_id).where(JobTag.tag == "has-pay")))      # same rule as the tag: a real number, not "DOE" or 0
    if remote is not None:
        conds.append(Job.remote.is_(remote))      # "On-site" means the post says on-site; unknown is not on-site
    if posted_within_days:
        cutoff = datetime.now(timezone.utc) - timedelta(days=posted_within_days)
        conds.append(_shown_date() >= cutoff)
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
    with_ww: dict[str, int] = {}            # what the list returns for that region while "also show worldwide" is on
    unspecified = 0
    for r, c in db.execute(select(Job.region, func.count(Job.id)).where(real).group_by(Job.region)).all():
        if not r:
            unspecified += c
        ks = geo.split(r)
        for k in ks:
            region_counts[k] = region_counts.get(k, 0) + c
        for k in geo.BANDS:
            if k in ks or "worldwide" in ks:
                with_ww[k] = with_ww.get(k, 0) + c
    return {
        "total": db.scalar(select(func.count(Job.id)).where(real)) or 0,
        "platforms": plats,
        "kinds": [{"value": k, "label": platforms.KIND_LABEL[k], "count": kinds.get(k, 0)} for k in ("board", "social", "web") if kinds.get(k)],
        "regions": [{"value": k, "label": lbl, "offset": off, "count": region_counts.get(k, 0),
                     "count_with_worldwide": with_ww.get(k, 0)} for k, (lbl, off) in geo.BANDS.items()]
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
    real = Job.is_real_job.is_(True)                # same population as /jobs, /jobs/facets and /public/stats
    total = db.scalar(select(func.count(Job.id)).where(real)) or 0
    by_platform = dict(db.execute(select(Job.platform, func.count(Job.id)).where(real).group_by(Job.platform)).all())
    by_type = {t: n for t, n in db.execute(select(Job.type, func.count(Job.id)).where(real, Job.type.is_not(None)).group_by(Job.type)).all()}
    return {"total": total, "by_platform": by_platform, "by_type": by_type}
