"""A customer's own live searches. Everything here needs a signed-in account; writes need the CSRF header."""
from __future__ import annotations

from fastapi import APIRouter, BackgroundTasks, Depends, HTTPException, Query, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.orm import Session

from .. import credits, live_search
from ..config import settings
from ..db import get_db
from ..models import Job, User, UserSearch, UserSearchJob
from ..schemas import JobOut
from ..security import csrf_guard, current_user, rate_limit_guard
from .jobs import serialize_jobs

router = APIRouter(prefix="/searches", tags=["searches"], dependencies=[Depends(rate_limit_guard)])


class SearchBody(BaseModel):
    query: str = Field(max_length=300)
    freshness: str = Field("w", max_length=4)
    site: str | None = Field(None, max_length=40)


def _row(s: UserSearch) -> dict:
    return {"id": s.id, "query": s.query, "site": s.site, "freshness": s.freshness, "status": s.status, "cached": s.cached,
            "results": s.results_count, "new_jobs": s.new_jobs, "cost_usd": credits.micro_to_usd(s.cost_micro), "error": s.error,
            "created_at": s.created_at.isoformat(), "finished_at": s.finished_at.isoformat() if s.finished_at else None}


def _refused(e: live_search.SearchRefused) -> JSONResponse:
    return JSONResponse(status_code=e.status, content={"detail": e.message, **e.extra})


@router.get("/config")
def config(user: User = Depends(current_user), db: Session = Depends(get_db)) -> dict:
    ready, why = live_search.live_search_ready()
    return {"ready": ready, "reason": why, "price_usd": live_search.estimated_user_price_usd(), "sites": list(live_search.SITES),
            "cache_hours": settings.search_cache_hours, "daily_limit": settings.user_searches_per_day,
            "left_today": max(0, settings.user_searches_per_day - live_search._used_today(db, user.id)), "balance_usd": credits.micro_to_usd(user.balance_micro)}


@router.post("/estimate")
def estimate(body: SearchBody, request: Request, user: User = Depends(current_user), db: Session = Depends(get_db)):
    csrf_guard(request)
    try:
        return live_search.quote(db, user, body.query, body.freshness, body.site)
    except live_search.SearchRefused as e:
        return _refused(e)


@router.post("", status_code=202)
def start(body: SearchBody, request: Request, background: BackgroundTasks, user: User = Depends(current_user), db: Session = Depends(get_db)):
    csrf_guard(request)
    try:
        s = live_search.start_search(db, user, body.query, body.freshness, body.site)
    except live_search.SearchRefused as e:
        db.rollback()
        return _refused(e)
    db.commit()
    if s.status == "queued":
        background.add_task(live_search.run_search, s.id)
    return _row(s)


@router.get("")
def history(limit: int = Query(20, ge=1, le=50), user: User = Depends(current_user), db: Session = Depends(get_db)) -> list[dict]:
    return [_row(s) for s in db.scalars(select(UserSearch).where(UserSearch.user_id == user.id).order_by(UserSearch.id.desc()).limit(limit))]


def _mine(db: Session, user: User, search_id: int) -> UserSearch:
    s = db.get(UserSearch, search_id)
    if s is None or s.user_id != user.id:                       # same answer for "missing" and "someone else's"
        raise HTTPException(404, "No such search")
    return s


@router.get("/{search_id}")
def one(search_id: int, user: User = Depends(current_user), db: Session = Depends(get_db)) -> dict:
    live_search.expire_stale(db)
    db.commit()
    return _row(_mine(db, user, search_id))


@router.get("/{search_id}/jobs", response_model=list[JobOut])
def jobs(search_id: int, limit: int = Query(50, ge=1, le=100), user: User = Depends(current_user), db: Session = Depends(get_db)):
    s = _mine(db, user, search_id)
    rows = db.scalars(select(Job).join(UserSearchJob, UserSearchJob.job_id == Job.id)
                      .where(UserSearchJob.search_id == s.id, Job.is_real_job.is_(True))
                      .order_by(Job.first_seen_at.desc(), Job.id.desc()).limit(limit)).all()
    return serialize_jobs(db, rows)
