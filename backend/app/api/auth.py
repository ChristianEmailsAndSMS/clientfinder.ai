"""Login, logout and first-run admin setup.

First-run setup (the "Create your admin account" screen): on the server run `scripts/admin_cli.py setup-code`; it prints a
one-time code. In the browser enter your email (must be in ADMIN_EMAILS), that code and a password, scan the QR code with an
authenticator app, and confirm with a 6-digit code. Nobody without server access can create an admin."""
from __future__ import annotations

import hmac
from datetime import timedelta

from fastapi import APIRouter, Depends, HTTPException, Request, Response
from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.orm import Session

from ..config import settings
from ..credits import micro_to_usd
from ..db import get_db
from ..models import SetupCode, User
from ..security import (LOCKOUT_AFTER, LOCKOUT_MINUTES, _ph, admin_emails, aware, check_totp, clear_session_cookie, client_ip,
                        csrf_guard, current_user, encrypt_secret, hash_password, hash_setup_code, ip_throttled, log_event,
                        new_totp_secret, normalize_email, password_problem, qr_data_uri, require_secrets, secrets_ready,
                        set_session_cookie, totp_uri, utcnow, verify_password)

router = APIRouter(prefix="/auth", tags=["auth"])

SETUP_MAX_ATTEMPTS = 5
GENERIC_LOGIN_ERROR = "Invalid email, password or code"


class LoginBody(BaseModel):
    email: str = Field(max_length=320)
    password: str = Field(max_length=256)
    totp_code: str | None = Field(None, max_length=12)


class SetupStart(BaseModel):
    email: str = Field(max_length=320)
    setup_code: str = Field(max_length=64)
    password: str = Field(max_length=256)


class SetupConfirm(BaseModel):
    email: str = Field(max_length=320)
    setup_code: str = Field(max_length=64)
    totp_code: str = Field(max_length=12)


def _me(user: User) -> dict:
    return {"id": user.id, "email": user.email, "is_admin": user.is_admin, "totp_enabled": user.totp_enabled,
            "balance_usd": micro_to_usd(user.balance_micro)}


# ---------- status ----------
@router.get("/setup/status")
def setup_status(db: Session = Depends(get_db)) -> dict:
    ok, why = secrets_ready()
    has_admin = db.scalar(select(User.id).where(User.is_admin.is_(True), User.is_active.is_(True), User.totp_enabled.is_(True)).limit(1))
    return {"auth_configured": ok, "problem": None if ok else why, "needs_setup": ok and has_admin is None}


# ---------- first-run setup ----------
def _valid_setup_code(db: Session, request: Request, email: str, code: str) -> SetupCode:
    """Return the matching live setup code or raise a generic 403. Wrong guesses burn the code after 5 tries."""
    if email not in admin_emails():
        log_event(db, "setup_fail", request, email=email, detail="email not allowed")
        db.commit()
        raise HTTPException(403, "Invalid setup code")
    row = db.scalar(select(SetupCode).where(SetupCode.email == email, SetupCode.used_at.is_(None)).order_by(SetupCode.id.desc()).limit(1))
    if row is None or aware(row.expires_at) < utcnow() or row.attempts >= SETUP_MAX_ATTEMPTS:
        log_event(db, "setup_fail", request, email=email, detail="no live code")
        db.commit()
        raise HTTPException(403, "Invalid setup code")
    if not hmac.compare_digest(row.code_hash, hash_setup_code(code)):
        row.attempts += 1
        if row.attempts >= SETUP_MAX_ATTEMPTS:
            row.used_at = utcnow()                       # burned: a new code must be issued on the server
        log_event(db, "setup_fail", request, email=email, detail=f"wrong code ({row.attempts}/{SETUP_MAX_ATTEMPTS})")
        db.commit()
        raise HTTPException(403, "Invalid setup code")
    return row


def _setup_guard(request: Request, db: Session) -> None:
    require_secrets()
    csrf_guard(request)
    if ip_throttled(db, client_ip(request)):
        raise HTTPException(429, "Too many attempts. Try again later.")


@router.post("/setup/start")
def setup_start(body: SetupStart, request: Request, db: Session = Depends(get_db)) -> dict:
    _setup_guard(request, db)
    email = normalize_email(body.email)
    _valid_setup_code(db, request, email, body.setup_code)
    existing = db.scalar(select(User).where(User.email == email))
    if existing and existing.is_active and existing.totp_enabled:
        raise HTTPException(409, "This account already exists. Sign in instead.")
    problem = password_problem(body.password, email, admin=True)
    if problem:
        raise HTTPException(422, problem)
    secret = new_totp_secret()
    user = existing or User(email=email, password_hash="")
    user.password_hash, user.is_admin, user.is_active = hash_password(body.password), True, False      # inactive until 2FA is confirmed
    user.totp_secret_enc, user.totp_enabled, user.totp_last_step = encrypt_secret(secret), False, None
    db.add(user)
    log_event(db, "setup_started", request, email=email)
    db.commit()
    uri = totp_uri(secret, email)
    return {"totp_secret": secret, "otpauth_uri": uri, "qr": qr_data_uri(uri)}


@router.post("/setup/confirm")
def setup_confirm(body: SetupConfirm, request: Request, response: Response, db: Session = Depends(get_db)) -> dict:
    _setup_guard(request, db)
    email = normalize_email(body.email)
    code_row = _valid_setup_code(db, request, email, body.setup_code)
    user = db.scalar(select(User).where(User.email == email))
    if user is None or user.is_active or not user.is_admin:
        raise HTTPException(409, "Start setup first")
    if not check_totp(user, body.totp_code):
        code_row.attempts += 1
        if code_row.attempts >= SETUP_MAX_ATTEMPTS:
            code_row.used_at = utcnow()
        log_event(db, "setup_fail", request, email=email, detail="wrong 2FA code")
        db.commit()
        raise HTTPException(403, "That code did not match. Check your phone's clock and try the next code.")
    user.is_active, user.totp_enabled, user.last_login_at = True, True, utcnow()
    code_row.used_at = utcnow()
    log_event(db, "setup_ok", request, user=user)
    db.commit()
    set_session_cookie(response, user)
    return _me(user)


# ---------- login / logout ----------
@router.post("/login")
def login(body: LoginBody, request: Request, response: Response, db: Session = Depends(get_db)) -> dict:
    require_secrets()
    csrf_guard(request)
    ip = client_ip(request)
    if ip_throttled(db, ip):
        raise HTTPException(429, "Too many attempts. Try again later.")
    email = body.email.strip().lower()
    user = db.scalar(select(User).where(User.email == email))
    now = utcnow()
    if user and user.locked_until and aware(user.locked_until) > now:
        log_event(db, "login_locked", request, user=user)
        db.commit()
        raise HTTPException(429, "Too many attempts. Try again later.")

    password_ok = verify_password(user.password_hash if user else None, body.password)     # same work for unknown emails
    if not (password_ok and user and user.is_active):
        if user:
            _register_failure(db, user, now)
        log_event(db, "login_fail", request, email=email)
        db.commit()
        raise HTTPException(401, GENERIC_LOGIN_ERROR)

    if user.totp_enabled:
        if not body.totp_code:
            raise HTTPException(401, "totp_required")
        if not check_totp(user, body.totp_code):
            _register_failure(db, user, now)
            log_event(db, "totp_fail", request, user=user)
            db.commit()
            raise HTTPException(401, GENERIC_LOGIN_ERROR)
    elif user.is_admin:
        raise HTTPException(403, "Admin accounts require 2FA. Run: scripts/admin_cli.py reset-admin")

    user.failed_logins, user.locked_until, user.last_login_at = 0, None, now
    if _ph.check_needs_rehash(user.password_hash):
        user.password_hash = hash_password(body.password)
    log_event(db, "login_ok", request, user=user)
    db.commit()
    set_session_cookie(response, user)
    return _me(user)


def _register_failure(db: Session, user: User, now) -> None:
    user.failed_logins = (user.failed_logins or 0) + 1
    if user.failed_logins >= LOCKOUT_AFTER:
        user.locked_until, user.failed_logins = now + timedelta(minutes=LOCKOUT_MINUTES), 0


@router.get("/me")
def me(user: User = Depends(current_user)) -> dict:
    return _me(user)


@router.post("/logout")
def logout(request: Request, response: Response, db: Session = Depends(get_db)) -> dict:
    csrf_guard(request)
    clear_session_cookie(response)
    return {"ok": True}


@router.post("/logout-all")
def logout_all(request: Request, response: Response, user: User = Depends(current_user), db: Session = Depends(get_db)) -> dict:
    csrf_guard(request)
    user.session_version += 1                      # invalidates every session token this user has
    log_event(db, "logout_all", request, user=user)
    db.commit()
    clear_session_cookie(response)
    return {"ok": True}
