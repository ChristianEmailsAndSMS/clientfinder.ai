"""Endpoints the logged-out home page uses. They expose only aggregate numbers and a small REDACTED preview
(no company, no link, no description), so the home page feels alive without handing the product to scrapers."""
from __future__ import annotations

import threading
import time
from datetime import datetime, timedelta, timezone

from fastapi import APIRouter, Depends
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from ..config import settings
from ..db import get_db
from ..models import Job, ScrapeRun, Source
from ..security import rate_limit_guard

router = APIRouter(prefix="/public", tags=["public"], dependencies=[Depends(rate_limit_guard)])

_cache: dict[str, tuple[float, object]] = {}
_lock = threading.Lock()
TTL = 60.0


def _cached(key: str, build):
    now = time.monotonic()
    with _lock:
        hit = _cache.get(key)
        if hit and now - hit[0] < TTL:
            return hit[1]
    value = build()
    with _lock:
        _cache[key] = (now, value)
    return value


@router.get("/stats")
def public_stats(db: Session = Depends(get_db)) -> dict:
    def build():
        now = datetime.now(timezone.utc)
        real = Job.is_real_job.is_(True)
        from ..live_search import SOURCE_KEY
        # "Live: updated X ago" is about our own collection. A customer's paid search also writes a scrape_run, and must not make
        # a dead scheduler look alive.
        last = db.scalar(select(func.max(ScrapeRun.finished_at)).join(Source, Source.id == ScrapeRun.source_id)
                         .where(ScrapeRun.status == "ok", Source.key != SOURCE_KEY))
        if last is not None and last.tzinfo is None:
            last = last.replace(tzinfo=timezone.utc)
        return {
            "jobs_total": db.scalar(select(func.count(Job.id)).where(real)) or 0,
            "jobs_new_24h": db.scalar(select(func.count(Job.id)).where(real, Job.first_seen_at >= now - timedelta(hours=24))) or 0,
            "sources": db.scalar(select(func.count(func.distinct(Job.platform))).where(real)) or 0,
            "live": bool(last and now - last < timedelta(hours=30)),
            "last_update": last.isoformat() if last else None,
        }
    return _cached("stats", build)


@router.get("/preview")
def public_preview(db: Session = Depends(get_db)) -> list[dict]:
    def build():
        rows = db.execute(select(Job.title, Job.platform, Job.type, Job.pay_text, Job.remote, Job.first_seen_at)
                          .where(Job.is_real_job.is_(True)).order_by(Job.first_seen_at.desc()).limit(6)).all()
        return [{"title": t[:90], "platform": p, "type": ty, "pay": pay, "remote": r,
                 "seen_at": (s if s.tzinfo else s.replace(tzinfo=timezone.utc)).isoformat()} for t, p, ty, pay, r, s in rows]
    return _cached("preview", build)


@router.get("/config")
def public_config(db: Session = Depends(get_db)) -> dict:
    from ..live_search import estimated_user_price_usd, live_search_ready
    ready, _ = live_search_ready()
    from .auth import welcome_bonus_available
    bonus = settings.signup_bonus_usd if welcome_bonus_available(db) else 0.0         # not advertised once today's cap is used
    return {"live_search": ready, "search_price_usd": estimated_user_price_usd(), "signup_bonus_usd": bonus}
