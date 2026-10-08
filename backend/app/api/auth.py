"""Sign up, sign in, sign out, and 2FA enrolment for admins.

Customers: email + password, no code needed.
Admins: the owner's account is created on the server (`scripts/admin_cli.py create-admin`) because without email verification
anyone could register the owner's address first. The first browser sign-in then walks the admin through turning on 2FA."""
from __future__ import annotations

from datetime import timedelta

from fastapi import APIRouter, Depends, HTTPException, Request, Response
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from .. import credits
from ..config import settings
from ..db import get_db
from ..models import User
from ..security import (LOCKOUT_AFTER, LOCKOUT_MINUTES, _ph, admin_emails, aware, check_totp, clear_session_cookie, client_ip,
                        csrf_guard, current_user, encrypt_secret, hash_password, ip_throttled, issue_enrol_token, log_event,
                        new_totp_secret, normalize_email, password_problem, qr_data_uri, read_enrol_token, require_secrets,
                        secrets_ready, set_session_cookie, signup_throttle_reason, totp_uri, utcnow, verify_password)

router = APIRouter(prefix="/auth", tags=["auth"])

GENERIC_LOGIN_ERROR = "Invalid email, password or code"


class LoginBody(BaseModel):
    email: str = Field(max_length=320)
    password: str = Field(max_length=256)
    totp_code: str | None = Field(None, max_length=12)


class SignupBody(BaseModel):
    email: str = Field(max_length=320)
    password: str = Field(max_length=256)


class EnrolStart(BaseModel):
    setup_token: str = Field(max_length=2000)


class EnrolConfirm(BaseModel):
    setup_token: str = Field(max_length=2000)
    totp_code: str = Field(max_length=12)


def _me(user: User) -> dict:
    return {"id": user.id, "email": user.email, "is_admin": user.is_admin and user.email in admin_emails(),
            "totp_enabled": user.totp_enabled, "balance_usd": credits.micro_to_usd(user.balance_micro)}


@router.get("/status")
def status() -> dict:
    ok, why = secrets_ready()
    return {"auth_configured": ok, "problem": None if ok else why}


# ---------- sign up ----------
@router.post("/signup", status_code=201)
def signup(body: SignupBody, request: Request, response: Response, db: Session = Depends(get_db)) -> dict:
    require_secrets()
    csrf_guard(request)
    ip = client_ip(request)
    why = signup_throttle_reason(db, ip) or ("Too many attempts. Try again later." if ip_throttled(db, ip) else None)
    if why:
        raise HTTPException(429, why)
    email = normalize_email(body.email)
    if email in admin_emails():
        raise HTTPException(403, "This address is reserved for the site owner. If it is yours, sign in instead.")
    problem = password_problem(body.password, email, admin=False)
    if problem:
        raise HTTPException(422, problem)
    if db.scalar(select(User.id).where(User.email == email)):
        raise HTTPException(409, "An account with this email already exists. Sign in instead.")
    user = User(email=email, password_hash=hash_password(body.password), is_admin=False, is_active=True)
    db.add(user)
    try:
        db.flush()
    except IntegrityError:                       # two sign-ups for the same address raced
        db.rollback()
        raise HTTPException(409, "An account with this email already exists. Sign in instead.")
    if settings.signup_bonus_usd > 0:
        credits.apply(db, user.id, credits.usd_to_micro(settings.signup_bonus_usd), "grant", "Welcome credit")
    user.last_login_at = utcnow()
    log_event(db, "signup", request, user=user)
    db.commit()
    set_session_cookie(response, user)
    return _me(user)


# ---------- sign in / out ----------
@router.post("/login")
def login(body: LoginBody, request: Request, response: Response, db: Session = Depends(get_db)):
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

    if user.totp_enabled and (settings.admin_require_2fa or not user.is_admin):
        if not body.totp_code:
            raise HTTPException(401, "totp_required")
        if not check_totp(user, body.totp_code):
            _register_failure(db, user, now)
            log_event(db, "totp_fail", request, user=user)
            db.commit()
            raise HTTPException(401, GENERIC_LOGIN_ERROR)
    elif user.is_admin:
        if user.email not in admin_emails():
            raise HTTPException(403, "Admin access required")
    if user.is_admin and not user.totp_enabled and settings.admin_require_2fa:
        # Password is right but 2FA is not set up yet: hand back a short-lived enrolment token (NOT a session).
        log_event(db, "totp_setup_required", request, user=user)
        db.commit()
        return JSONResponse(status_code=401, content={"detail": "totp_setup_required", "setup_token": issue_enrol_token(user)})

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
def logout(request: Request, response: Response) -> dict:
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


# ---------- 2FA enrolment (admins) ----------
def _enrolling_admin(request: Request, db: Session, token: str) -> User:
    require_secrets()
    csrf_guard(request)
    if ip_throttled(db, client_ip(request)):
        raise HTTPException(429, "Too many attempts. Try again later.")
    claims = read_enrol_token(token)
    user = db.get(User, int(claims["sub"])) if claims and str(claims["sub"]).isdigit() else None
    if (not user or not user.is_active or not user.is_admin or user.totp_enabled
            or user.session_version != claims["sv"] or user.email not in admin_emails()):
        raise HTTPException(401, "That sign-in expired. Sign in again.")
    return user


@router.post("/2fa/start")
def twofa_start(body: EnrolStart, request: Request, db: Session = Depends(get_db)) -> dict:
    user = _enrolling_admin(request, db, body.setup_token)
    secret = new_totp_secret()
    user.totp_secret_enc, user.totp_last_step = encrypt_secret(secret), None       # pending until confirmed
    db.commit()
    uri = totp_uri(secret, user.email)
    return {"totp_secret": secret, "otpauth_uri": uri, "qr": qr_data_uri(uri)}


@router.post("/2fa/confirm")
def twofa_confirm(body: EnrolConfirm, request: Request, response: Response, db: Session = Depends(get_db)) -> dict:
    user = _enrolling_admin(request, db, body.setup_token)
    if not user.totp_secret_enc:
        raise HTTPException(409, "Start 2FA setup first")
    if not check_totp(user, body.totp_code):
        _register_failure(db, user, utcnow())
        log_event(db, "totp_fail", request, user=user, detail="enrolment")
        db.commit()
        raise HTTPException(403, "That code did not match. Check your phone's clock and try the next code.")
    user.totp_enabled, user.failed_logins, user.locked_until, user.last_login_at = True, 0, None, utcnow()
    log_event(db, "totp_enabled", request, user=user)
    db.commit()
    set_session_cookie(response, user)
    return _me(user)
