"""Admin endpoints. Require a signed-in admin (allow-listed email + 2FA) and the CSRF header on writes.

Long jobs (source runs, requeue) start in the background and return 202; poll GET /admin/sources or
/admin/scrape-runs for the outcome. A per-key lock stops double-starts (single API process)."""
from __future__ import annotations

import json
import threading
from datetime import datetime, timedelta, timezone
from pathlib import Path

from fastapi import APIRouter, BackgroundTasks, Depends, HTTPException, Query, Request
from pydantic import BaseModel, Field
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from ..config import settings
from ..db import get_db
from .. import credits, schema_check, whop
from ..models import AuthEvent, CreditEntry, FailedUrl, Job, PaymentEvent, ScrapeRun, SearchQuery, Source, User, UserSearch
from ..security import aware, client_ip, csrf_guard, current_admin, log_event
from ..scrapers import pipeline, query_plan
from ..scrapers.registry import DURABLE
from ..security import admin_emails
from ..schemas import BodyId, PathId
from ..tagging import retag


def _no_nul_in_query(request: Request) -> None:
    """PostgreSQL text cannot hold a NUL byte: a filter such as ?q=%00x was a 500 there (SQLite lets it through, so tests never saw it)."""
    if any("\x00" in v for v in request.query_params.values()):
        raise HTTPException(422, "Text contains characters we cannot use")


router = APIRouter(prefix="/admin", tags=["admin"], dependencies=[Depends(current_admin), Depends(csrf_guard), Depends(_no_nul_in_query)])

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
    google_keys = _google_keys()
    out = []
    for sid, key, kind, enabled in db.execute(select(Source.id, Source.key, Source.kind, Source.enabled).order_by(Source.key)):
        last = latest.get(sid)
        out.append({
            "key": key, "kind": kind, "enabled": enabled, "running": key in _running,
            "runnable": key in DURABLE or key in google_keys,
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


@router.post("/sources/run-all", status_code=202)
def run_all_sources(background: BackgroundTasks) -> dict:
    """Pull every free source now (job boards and feeds). Google sources are skipped: they cost a search credit each."""
    started = [k for k in DURABLE if _claim(k)]
    for k in started:
        background.add_task(_run_source, k)
    return {"started": started, "skipped_running": [k for k in DURABLE if k not in started]}


@router.post("/regions/rebuild")
def rebuild_regions(db: Session = Depends(get_db)) -> dict:
    """Recompute every job's time-zone band from its location text (after the matching rules improve)."""
    from .. import geo
    n = 0
    for job in db.scalars(select(Job).where(Job.location.is_not(None))).all():
        r = geo.classify(job.location)
        if r != job.region:
            job.region, n = r, n + 1
    db.commit()
    return {"changed": n}


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
    ids: list[BodyId] | None = Field(None, max_length=500, description="specific rows (even dead ones); omit for all pending")
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


def backups_info() -> dict:
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
    return {"status": status, "dir": str(d), "count": len(files), "latest": items, "last_run": last, "offsite": offsite_info(d, now)}


def offsite_info(d: Path, now: float) -> dict:
    """The encrypted off-server copy (deploy/offsite_backup.sh writes offsite.json): none / ok / failed / stale (older than 36h)."""
    try:
        last = json.loads((d / "offsite.json").read_text())
        age = (now - datetime.fromisoformat(str(last["ts"]).replace("Z", "+00:00")).timestamp()) / 3600
    except (OSError, ValueError, KeyError):
        return {"status": "none", "last": None, "age_hours": None}
    status = "failed" if not last.get("ok") else ("ok" if age <= 36 else "stale")
    return {"status": status, "last": last, "age_hours": round(age, 1)}


@router.get("/backups")
def backups() -> dict:
    return backups_info()


# ---------- overview ----------
@router.get("/overview")
def overview(db: Session = Depends(get_db)) -> dict:
    now = datetime.now(timezone.utc)
    total_jobs = db.scalar(select(func.count(Job.id))) or 0
    new_24h = db.scalar(select(func.count(Job.id)).where(Job.first_seen_at >= now - timedelta(hours=24))) or 0
    pending_failed = db.scalar(select(func.count(FailedUrl.id)).where(FailedUrl.status == "pending")) or 0
    srcs = list_sources(db)
    # Only the scheduled feeds can be "failing" or "stale". Customer searches ("usersearch") and Google plan queries run on demand, so
    # no run for a day is normal for them and must not raise an alarm.
    failing = [x["key"] for x in srcs if x["key"] in DURABLE and x["last_run"] and x["last_run"]["status"] == "failed"]
    stale = [x["key"] for x in srcs if x["key"] in DURABLE
             and (not x["last_run"] or (now - datetime.fromisoformat(x["last_run"]["at"])) > timedelta(hours=30))]
    payments_waiting = db.scalar(select(func.count(PaymentEvent.id)).where(PaymentEvent.status.in_(("review", "unmatched")))) or 0
    users = db.scalar(select(func.count(User.id)).where(User.is_admin.is_(False))) or 0
    liability = db.scalar(select(func.coalesce(func.sum(User.balance_micro), 0)).where(User.is_admin.is_(False))) or 0
    used = query_plan.searches_used_this_month(db)
    tot = {"search": [0, 0, 0], "draft": [0, 0, 0]}             # count, charged, our cost: searches and Pitch help drafts kept apart
    for (meta,) in db.execute(select(CreditEntry.meta).where(CreditEntry.kind == "usage", CreditEntry.created_at >= now - timedelta(days=30)).limit(20000)):
        if meta and meta.get("our_cost_micro"):
            t = tot["search" if "search_id" in meta else "draft"]
            t[0] += 1
            t[1] += meta.get("charged_micro", 0)
            t[2] += meta["our_cost_micro"]
    (n, charged, ours), (dn, dcharged, dours) = tot["search"], tot["draft"]
    return {
        "schema": schema_check.check(db.get_bind()),
        "payments_waiting": payments_waiting,
        "customer_searches_30d": {"count": n, "charged_usd": credits.micro_to_usd(charged), "our_cost_usd": credits.micro_to_usd(ours),
                                  "margin_x": round(charged / ours, 3) if ours else None},
        "pitch_drafts_30d": {"count": dn, "charged_usd": credits.micro_to_usd(dcharged), "our_cost_usd": credits.micro_to_usd(dours),
                             "margin_x": round(dcharged / dours, 3) if dours else None},
        "jobs": {"total": total_jobs, "new_24h": new_24h},
        "sources": {"count": len(srcs), "failing": failing, "stale": stale},
        "failed_urls_pending": pending_failed,
        "customers": {"count": users, "credit_liability_usd": credits.micro_to_usd(int(liability))},
        "search_budget": {"used": used, "budget": settings.serpapi_monthly_budget},
        "backups": {"status": (b := backups_info())["status"], "offsite": b["offsite"]["status"]},
        "security": {"admin_emails": sorted(admin_emails())},
    }


# ---------- customers + credits ----------
def _user_row(u: User) -> dict:
    return {"id": u.id, "email": u.email, "is_admin": u.is_admin, "is_active": u.is_active,
            "balance_usd": credits.micro_to_usd(u.balance_micro), "created_at": _iso(u.created_at),
            "unlimited": u.unlimited_credits, "daily_search_limit": u.daily_search_limit, "onboarded": u.onboarded_at is not None,
            "prefs": u.prefs or {},
            "last_login_at": _iso(u.last_login_at), "locked": bool(u.locked_until and aware(u.locked_until) > datetime.now(timezone.utc))}


def _get_user(db: Session, user_id: int) -> User:
    u = db.get(User, user_id)
    if u is None:
        raise HTTPException(404, "No such user")
    return u


@router.get("/users")
def list_users(q: str | None = Query(None, max_length=100), limit: int = Query(100, ge=1, le=500), db: Session = Depends(get_db)) -> list[dict]:
    stmt = select(User).order_by(User.id.desc()).limit(limit)
    if q:
        stmt = stmt.where(User.email.ilike(f"%{q.strip().lower()}%"))
    users = db.scalars(stmt).all()
    ids = [u.id for u in users]
    stats = {uid: n for uid, n in db.execute(
        select(UserSearch.user_id, func.count(UserSearch.id))
        .where(UserSearch.user_id.in_(ids), UserSearch.status == "done", UserSearch.cached.is_(False)).group_by(UserSearch.user_id))}
    spent = {uid: int(s) for uid, s in db.execute(          # everything they were charged: searches AND Pitch help drafts (ledger is the truth)
        select(CreditEntry.user_id, func.coalesce(func.sum(-CreditEntry.delta_micro), 0))
        .where(CreditEntry.user_id.in_(ids), CreditEntry.kind == "usage").group_by(CreditEntry.user_id))}
    return [{**_user_row(u), "searches": stats.get(u.id, 0), "spent_usd": credits.micro_to_usd(spent.get(u.id, 0))} for u in users]


@router.get("/users/{user_id}/ledger")
def user_ledger(user_id: PathId, limit: int = Query(100, ge=1, le=500), db: Session = Depends(get_db)) -> dict:
    u = _get_user(db, user_id)
    rows = db.scalars(select(CreditEntry).where(CreditEntry.user_id == user_id).order_by(CreditEntry.id.desc()).limit(limit)).all()
    return {"user": _user_row(u), "entries": [
        {"id": r.id, "at": _iso(r.created_at), "kind": r.kind, "amount_usd": credits.micro_to_usd(r.delta_micro),
         "balance_after_usd": credits.micro_to_usd(r.balance_after_micro), "reason": r.reason, "ref": r.ref,
         "by": r.actor_user_id} for r in rows]}


class CreditAdjust(BaseModel):
    amount_usd: str | float | int = Field(description="positive dollar amount, up to 6 decimals")
    kind: str = Field(pattern="^(grant|revoke|refund)$")
    reason: str = Field(min_length=3, max_length=200, pattern=r"^[^\x00]*$")     # no NUL byte: PostgreSQL would answer 500


@router.post("/users/{user_id}/credits")
def adjust_credits(user_id: PathId, body: CreditAdjust, request: Request, admin: User = Depends(current_admin),
                   db: Session = Depends(get_db)) -> dict:
    u = _get_user(db, user_id)
    try:
        micro = credits.usd_to_micro(body.amount_usd)
    except ValueError as e:
        raise HTTPException(422, f"Invalid amount: {e}")
    if micro <= 0:
        raise HTTPException(422, "Amount must be greater than zero")
    if micro > credits.usd_to_micro(settings.max_credit_adjust_usd):
        raise HTTPException(422, f"Single adjustments are capped at ${settings.max_credit_adjust_usd:,.2f}")
    delta = -micro if body.kind == "revoke" else micro
    try:
        entry = credits.apply(db, u.id, delta, body.kind, body.reason, actor_user_id=admin.id)
    except credits.InsufficientCredits as e:
        raise HTTPException(409, f"Cannot revoke more than the balance (${credits.micro_to_usd(e.balance_micro):,.4f})")
    log_event(db, "credit_" + body.kind, request, user=admin, detail=f"user {u.id} {delta / credits.MICRO:+.6f} USD: {body.reason}")
    db.commit()
    return {"user": _user_row(u), "entry_id": entry.id}


class LimitsBody(BaseModel):
    unlimited: bool | None = None
    daily_search_limit: int | None = Field(None, ge=0, le=1000, description="new searches per 24h; 0 puts the user back on the site default")


@router.post("/users/{user_id}/limits")
def set_limits(user_id: PathId, body: LimitsBody, request: Request, admin: User = Depends(current_admin), db: Session = Depends(get_db)) -> dict:
    u = _get_user(db, user_id)
    if body.unlimited is None and body.daily_search_limit is None:
        raise HTTPException(400, "Nothing to change: send unlimited and/or daily_search_limit.")
    changes = []
    if body.unlimited is not None:
        u.unlimited_credits = body.unlimited
        changes.append(f"unlimited={body.unlimited}")
    if body.daily_search_limit is not None:
        u.daily_search_limit = body.daily_search_limit or None
        changes.append(f"daily_limit={body.daily_search_limit or 'default'}")
    log_event(db, "user_limits", request, user=admin, detail=f"user {u.id}: {', '.join(changes) or 'no change'}")
    db.commit()
    return _user_row(u)


class ActiveBody(BaseModel):
    active: bool


@router.post("/users/{user_id}/active")
def set_active(user_id: PathId, body: ActiveBody, request: Request, admin: User = Depends(current_admin), db: Session = Depends(get_db)) -> dict:
    u = _get_user(db, user_id)
    if u.id == admin.id:
        raise HTTPException(409, "You cannot suspend your own account")
    u.is_active = body.active
    if not body.active:
        u.session_version += 1                      # kick any open sessions immediately
    log_event(db, "user_active" if body.active else "user_suspended", request, user=admin, detail=f"user {u.id}")
    db.commit()
    return _user_row(u)


@router.post("/users/{user_id}/unlock")
def unlock_user(user_id: PathId, request: Request, admin: User = Depends(current_admin), db: Session = Depends(get_db)) -> dict:
    u = _get_user(db, user_id)
    u.locked_until, u.failed_logins = None, 0
    db.add(AuthEvent(event="lock_cleared", email=u.email, user_id=u.id, ip=client_ip(request)))      # the per-address failure count restarts here
    log_event(db, "user_unlocked", request, user=admin, detail=f"user {u.id}")
    db.commit()
    return _user_row(u)


@router.post("/users/{user_id}/logout-everywhere")
def kick_user(user_id: PathId, request: Request, admin: User = Depends(current_admin), db: Session = Depends(get_db)) -> dict:
    u = _get_user(db, user_id)
    u.session_version += 1
    log_event(db, "user_kicked", request, user=admin, detail=f"user {u.id}")
    db.commit()
    return _user_row(u)


# ---------- security audit log ----------
@router.get("/security/events")
def security_events(limit: int = Query(100, ge=1, le=500), event: str | None = Query(None, max_length=32), db: Session = Depends(get_db)) -> list[dict]:
    q = select(AuthEvent).order_by(AuthEvent.id.desc()).limit(limit)
    if event:
        q = q.where(AuthEvent.event == event)
    return [{"at": _iso(e.created_at), "event": e.event, "email": e.email, "ip": e.ip, "detail": e.detail,
             "user_agent": (e.user_agent or "")[:80]} for e in db.scalars(q).all()]


# ---------- payments ----------
class ResolveBody(BaseModel):
    user_id: BodyId


@router.get("/payments")
def payments(status: str | None = None, limit: int = Query(50, ge=1, le=200), db: Session = Depends(get_db)) -> list[dict]:
    q = select(PaymentEvent).order_by(PaymentEvent.id.desc()).limit(limit)
    if status:
        q = q.where(PaymentEvent.status == status)
    return [{"id": e.id, "created_at": e.created_at, "type": e.event_type, "status": e.status, "note": e.note, "email": e.email,
             "amount_usd": credits.micro_to_usd(e.amount_micro) if e.amount_micro else None, "user_id": e.user_id}
            for e in db.scalars(q)]


@router.post("/payments/{event_id}/dismiss")
def dismiss_payment(event_id: PathId, request: Request, admin: User = Depends(current_admin), db: Session = Depends(get_db)) -> dict:
    """Mark a held payment notice as handled without crediting anything (a refund or dispute you dealt with, or a payment you chose not to credit).
    Without this, the red 'payments need a decision' alarm on Overview could never clear for a refund."""
    ev = db.get(PaymentEvent, event_id)
    if not ev:
        raise HTTPException(404, "No such payment")
    if ev.status not in ("unmatched", "review"):
        raise HTTPException(409, "This payment is not waiting for a decision")
    ev.status, ev.note = "ignored", "dismissed by owner"
    log_event(db, "payment_dismissed", request, user=admin, detail=f"event {ev.id}")
    db.commit()
    return {"ok": True}


@router.post("/payments/{event_id}/credit")
def credit_payment(event_id: PathId, body: ResolveBody, request: Request, admin: User = Depends(current_admin), db: Session = Depends(get_db)) -> dict:
    ev = db.get(PaymentEvent, event_id)
    if not ev or not db.get(User, body.user_id):
        raise HTTPException(404, "No such payment or user")
    try:
        whop.resolve(db, ev, body.user_id, admin.id)
    except ValueError as e:
        raise HTTPException(409, str(e))
    log_event(db, "payment_resolved", request, user=admin, detail=f"event {ev.id} -> user {body.user_id}")
    db.commit()
    return {"ok": True}
