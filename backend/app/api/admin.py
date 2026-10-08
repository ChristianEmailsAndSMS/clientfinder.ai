"""Admin endpoints. Guarded by the X-Admin-Token header (settings.admin_token); disabled when unset.

Long jobs (source runs, requeue) start in the background and return 202; poll GET /admin/sources or
/admin/scrape-runs for the outcome. A per-key lock stops double-starts (single API process)."""
from __future__ import annotations

import hmac
import json
import threading
from datetime import datetime, timedelta, timezone
from pathlib import Path

from fastapi import APIRouter, BackgroundTasks, Depends, Header, HTTPException, Query
from pydantic import BaseModel, Field
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from ..config import settings
from ..db import get_db
from ..models import FailedUrl, Job, ScrapeRun, Source
from ..scrapers import pipeline, query_plan
from ..scrapers.registry import DURABLE
from ..tagging import retag


def require_admin(x_admin_token: str | None = Header(default=None)) -> None:
    if not settings.admin_token:
        raise HTTPException(503, "admin API disabled: set ADMIN_TOKEN")
    if not x_admin_token or not hmac.compare_digest(x_admin_token.encode(), settings.admin_token.encode()):
        raise HTTPException(401, "invalid admin token")


router = APIRouter(prefix="/admin", tags=["admin"], dependencies=[Depends(require_admin)])

_running: set[str] = set()
_lock = threading.Lock()


def _claim(name: str) -> bool:
    with _lock:
        if name in _running:
            return False
        _running.add(name)
        return True


def _release(name: str) -> None:
    with _lock:
        _running.discard(name)


def _google_keys() -> dict[str, str]:
    """sources.key -> query text for every query in the plan."""
    return {pipeline._source_key(q.text): q.text for q in query_plan.build_plan()}


def _aware(dt: datetime | None) -> datetime | None:
    return dt if dt is None or dt.tzinfo else dt.replace(tzinfo=timezone.utc)


def _iso(dt: datetime | None) -> str | None:
    dt = _aware(dt)
    return dt.isoformat() if dt else None


# ---------- sources ----------
@router.get("/sources")
def list_sources(db: Session = Depends(get_db)) -> list[dict]:
    now = datetime.now(timezone.utc)
    runs = db.execute(select(ScrapeRun.source_id, ScrapeRun.started_at, ScrapeRun.status, ScrapeRun.jobs_added,
                             ScrapeRun.error).order_by(ScrapeRun.started_at.desc())).all()
    latest: dict[int, tuple] = {}
    day: dict[int, list[int]] = {}
    for sid, started, status, added, err in runs:
        latest.setdefault(sid, (started, status, added, err))
        if _aware(started) >= now - timedelta(hours=24):
            c = day.setdefault(sid, [0, 0])
            c[0] += 1
            c[1] += 1 if status == "failed" else 0
    totals = dict(db.execute(select(Job.source_key, func.count(Job.id)).group_by(Job.source_key)).all())
    out = []
    for sid, key, kind, enabled in db.execute(select(Source.id, Source.key, Source.kind, Source.enabled).order_by(Source.key)):
        last = latest.get(sid)
        out.append({
            "key": key, "kind": kind, "enabled": enabled, "running": key in _running,
            "total_jobs": totals.get(key, 0),
            "runs_24h": day.get(sid, [0, 0])[0], "failed_24h": day.get(sid, [0, 0])[1],
            "last_run": None if not last else {"at": _iso(last[0]), "status": last[1], "jobs_added": last[2], "error": last[3]},
        })
    return out


def _run_source(key: str) -> None:
    try:
        if key in DURABLE:
            DURABLE[key].run()
        else:
            query_plan.run_query_now(_google_keys()[key])
    finally:
        _release(key)


@router.post("/sources/{key}/run", status_code=202)
def run_source_now(key: str, background: BackgroundTasks) -> dict:
    if key not in DURABLE and key not in _google_keys():
        raise HTTPException(404, f"unknown source {key!r}")
    if not _claim(key):
        raise HTTPException(409, f"{key} is already running")
    background.add_task(_run_source, key)
    return {"started": key, "note": "a Google source spends one search credit"} if key not in DURABLE else {"started": key}


@router.get("/scrape-runs")
def scrape_runs(limit: int = Query(50, ge=1, le=500), status: str | None = None, db: Session = Depends(get_db)) -> list[dict]:
    q = select(ScrapeRun, Source.key).join(Source, Source.id == ScrapeRun.source_id).order_by(ScrapeRun.started_at.desc()).limit(limit)
    if status:
        q = q.where(ScrapeRun.status == status)
    return [{"id": r.id, "source": key, "started_at": _iso(r.started_at), "finished_at": _iso(r.finished_at),
             "status": r.status, "jobs_added": r.jobs_added, "jobs_updated": r.jobs_updated, "error": r.error}
            for r, key in db.execute(q).all()]


# ---------- failed URLs ----------
@router.get("/failed-urls")
def failed_urls(status: str = Query("pending", pattern="^(pending|dead|resolved|all)$"),
                limit: int = Query(100, ge=1, le=500), db: Session = Depends(get_db)) -> dict:
    q = select(FailedUrl).order_by(FailedUrl.last_failed_at.desc()).limit(limit)
    if status != "all":
        q = q.where(FailedUrl.status == status)
    counts = dict(db.execute(select(FailedUrl.status, func.count(FailedUrl.id)).group_by(FailedUrl.status)).all())
    return {"counts": counts, "items": [
        {"id": f.id, "url": f.url, "platform": f.platform, "source": f.source_key, "error": f.error,
         "attempts": f.attempts, "status": f.status, "last_failed_at": _iso(f.last_failed_at)}
        for f in db.scalars(q).all()]}


class RequeueBody(BaseModel):
    ids: list[int] | None = Field(None, description="specific rows (even dead ones); omit for all pending")
    limit: int = Field(50, ge=1, le=500)


def _run_requeue(ids: list[int] | None, limit: int) -> None:
    try:
        pipeline.requeue_failed(limit=limit, ids=ids)
    finally:
        _release("requeue")


@router.post("/failed-urls/requeue", status_code=202)
def requeue(body: RequeueBody, background: BackgroundTasks) -> dict:
    if not _claim("requeue"):
        raise HTTPException(409, "a requeue is already running")
    background.add_task(_run_requeue, body.ids, body.limit)
    return {"started": "requeue", "note": "costs fetch + Claude tokens, no search credits"}


# ---------- maintenance ----------
@router.post("/retag")
def retag_all(db: Session = Depends(get_db)) -> dict:
    return {"retagged": retag(db)}


@router.get("/backups")
def backups() -> dict:
    d = Path(settings.backup_dir)
    files = sorted(d.glob("clientfinder-*.dump"), key=lambda p: p.stat().st_mtime, reverse=True) if d.is_dir() else []
    now = datetime.now(timezone.utc).timestamp()
    items = [{"file": f.name, "bytes": f.stat().st_size, "age_hours": round((now - f.stat().st_mtime) / 3600, 1)} for f in files[:10]]
    status = "none" if not items else ("ok" if items[0]["age_hours"] <= 36 else "stale")
    last = None
    try:
        last = json.loads((d / "last_run.json").read_text())
    except (OSError, ValueError):
        pass
    return {"status": status, "dir": str(d), "count": len(files), "latest": items, "last_run": last}
