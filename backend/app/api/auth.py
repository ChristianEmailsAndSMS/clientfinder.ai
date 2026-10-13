"""Sign up, sign in, sign out, and 2FA enrolment for admins.

Customers: email + password, no code needed.
Admins: the owner's account is created on the server (`scripts/admin_cli.py create-admin`) because without email verification
anyone could register the owner's address first. The first browser sign-in then walks the admin through turning on 2FA."""
from __future__ import annotations

from datetime import timedelta

from fastapi import APIRouter, Depends, HTTPException, Request, Response
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field
from sqlalchemy import func, select, text, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from .. import credits
from ..config import settings
from ..db import get_db
from ..models import AuthEvent, CreditEntry, User
from ..security import (LOCKOUT_AFTER, LOCKOUT_MINUTES, _ph, admin_emails, aware, check_totp, clear_session_cookie, client_ip,
                        csrf_guard, current_user, session_user, encrypt_secret, hash_password, ip_throttled, issue_enrol_token, loggable_email, log_event,
                        new_totp_secret, normalize_email, password_problem, qr_data_uri, read_enrol_token, require_secrets,
                        secrets_ready, set_session_cookie, signup_throttle_reason, totp_uri, utcnow, verify_password)

router = APIRouter(prefix="/auth", tags=["auth"])

GENERIC_LOGIN_ERROR = "Invalid email, password or code"
LOCKED_MESSAGE = f"Too many sign-in attempts. Wait {LOCKOUT_MINUTES} minutes, then try again."      # same words for every address, real or not


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
            "totp_enabled": user.totp_enabled, "balance_usd": credits.micro_to_usd(user.balance_micro),
            "unlimited": user.unlimited_credits, "onboarded": user.onboarded_at is not None, "prefs": user.prefs or {}}


@router.get("/status")
def status(user: User | None = Depends(session_user)) -> dict:
    ok, why = secrets_ready()
    # signed_in lets the pages skip /auth/me for visitors (that call is a 401 in their browser console)
    return {"auth_configured": ok, "problem": None if ok else why, "signed_in": user is not None}


# ---------- welcome credit ----------
def mailbox_key(email: str) -> str:
    """One key per real mailbox: a+promo@x.com, gmail dots and googlemail.com are the same inbox as the plain address."""
    local, _, domain = email.strip().lower().rpartition("@")
    local = local.split("+", 1)[0]
    if domain in ("gmail.com", "googlemail.com"):
        local, domain = local.replace(".", ""), "gmail.com"
    return f"{local}@{domain}"


WELCOME_LOCK_KEY = 7310120001      # arbitrary app-wide number for the Postgres advisory lock


def welcome_bonus_available(db: Session) -> bool:
    """Is the welcome credit switched on and the site-wide daily cap not yet used up?"""
    if settings.signup_bonus_usd <= 0:
        return False
    cap = settings.signup_bonus_daily_cap
    if cap <= 0:
        return True
    if db.get_bind().dialect.name == "postgresql":
        # Serialise the count-then-grant: without this, sign-ups sent at the same moment all read the same count and all get the credit.
        # The lock lasts until this sign-up commits. (SQLite has one writer at a time, so it needs nothing.)
        db.execute(text("SELECT pg_advisory_xact_lock(:k)"), {"k": WELCOME_LOCK_KEY})
    since = utcnow() - timedelta(hours=24)
    given = db.scalar(select(func.count(CreditEntry.id)).where(CreditEntry.reason == "Welcome credit", CreditEntry.created_at >= since)) or 0
    return given < cap


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

    def reject(status: int, message: str):
        log_event(db, "signup_fail", request, detail=str(status))       # counted by ip_throttled: the 409 answer is an "is this email registered?" oracle
        db.commit()
        raise HTTPException(status, message)
    if email in admin_emails():
        reject(403, "This address is reserved for the site owner. If it is yours, sign in instead.")
    problem = password_problem(body.password, email, admin=False)
    if problem:
        reject(422, problem)
    if db.scalar(select(User.id).where(User.email == email)):
        reject(409, "An account with this email already exists. Sign in instead.")
    user = User(email=email, password_hash=hash_password(body.password), is_admin=False, is_active=True)
    db.add(user)
    try:
        db.flush()
    except IntegrityError:                       # two sign-ups for the same address raced
        db.rollback()
        reject(409, "An account with this email already exists. Sign in instead.")
    welcome_note = None                          # set only when a credit was advertised but not given, so the page can say why
    if welcome_bonus_available(db):
        entry = credits.apply(db, user.id, credits.usd_to_micro(settings.signup_bonus_usd), "grant", "Welcome credit", ref=credits.make_ref("welcome", mailbox_key(email)))
        if entry.user_id != user.id:             # idempotent replay: this mailbox already claimed it on another account
            welcome_note = "The welcome credit was already used for this mailbox, so this account starts at $0.00."
    elif settings.signup_bonus_usd > 0:
        welcome_note = "Today's free welcome credits have all been given out, so this account starts at $0.00."
    user.last_login_at = utcnow()
    log_event(db, "signup", request, user=user)
    db.commit()
    set_session_cookie(response, user)
    return {**_me(user), "welcome_note": welcome_note}


# ---------- sign in / out ----------
@router.post("/login")
def login(body: LoginBody, request: Request, response: Response, db: Session = Depends(get_db)):
    require_secrets()
    csrf_guard(request)
    ip = client_ip(request)
    if ip_throttled(db, ip):
        raise HTTPException(429, "Too many attempts. Try again later.")
    email = body.email.strip().lower()
    if "\x00" in email:                              # PostgreSQL rejects NUL in text: a 500 instead of the normal wrong-login answer
        raise HTTPException(401, GENERIC_LOGIN_ERROR)
    user = db.scalar(select(User).where(User.email == email))
    now = utcnow()
    # Failures are counted per ADDRESS (real or made up), so the lock-out answer is identical for both and does not reveal which accounts exist.
    # Before this, a customer who mistyped five times got "Invalid email or password" for 15 minutes with no hint that they were locked out.
    shown = loggable_email(email)
    # Only failures after the last successful sign-in (or owner unlock) for this address count; the audit rows themselves are never edited.
    fresh_since = func.coalesce(select(func.max(AuthEvent.created_at)).where(AuthEvent.event.in_(("login_ok", "lock_cleared")), AuthEvent.email == shown).scalar_subquery(),
                                now - timedelta(minutes=LOCKOUT_MINUTES))
    if shown and (db.scalar(select(func.count(AuthEvent.id)).where(
            AuthEvent.event.in_(("login_fail", "totp_fail")), AuthEvent.email == shown,
            AuthEvent.created_at >= now - timedelta(minutes=LOCKOUT_MINUTES), AuthEvent.created_at > fresh_since)) or 0) >= LOCKOUT_AFTER:
        db.add(AuthEvent(event="login_locked", email=shown, ip=client_ip(request), user_agent=request.headers.get("user-agent", "")[:200]))
        db.commit()
        raise HTTPException(429, LOCKED_MESSAGE)
    # Count this attempt BEFORE the slow password check and commit it, so a burst of simultaneous guesses cannot
    # all be checked against the same stale counter (per-IP row below, per-account counter via an atomic UPDATE).
    attempt = AuthEvent(event="login_fail", email=loggable_email(email), ip=client_ip(request),
                        user_agent=request.headers.get("user-agent", "")[:200])
    db.add(attempt)
    claimed = _claim_attempt(db, user.id) if user else 0
    db.commit()
    if user and ((user.locked_until and aware(user.locked_until) > now) or claimed > LOCKOUT_AFTER):
        verify_password(user.password_hash, body.password)           # same work as any other attempt, so timing does not give the lock away
        attempt.event, attempt.user_id = "login_locked", user.id
        db.commit()
        raise HTTPException(429, LOCKED_MESSAGE)                     # unknown addresses lock the same way (see above), so this reveals nothing

    password_ok = verify_password(user.password_hash if user else None, body.password)     # same work for unknown emails
    if not (password_ok and user and user.is_active):
        if user:
            _register_failure(db, user, now, claimed)
        db.commit()
        raise HTTPException(401, GENERIC_LOGIN_ERROR)

    if user.totp_enabled and (settings.admin_require_2fa or not user.is_admin):
        if not body.totp_code:
            _release_attempt(db, user, attempt)
            raise HTTPException(401, "totp_required")
        if not check_totp(user, body.totp_code):
            _register_failure(db, user, now, claimed)
            attempt.event, attempt.user_id, attempt.email = "totp_fail", user.id, user.email
            db.commit()
            raise HTTPException(401, GENERIC_LOGIN_ERROR)
    elif user.is_admin:
        if user.email not in admin_emails():
            _release_attempt(db, user, attempt)
            raise HTTPException(403, "Admin access required")
    if user.is_admin and not user.totp_enabled and settings.admin_require_2fa:
        # Password is right but 2FA is not set up yet: hand back a short-lived enrolment token (NOT a session).
        attempt.event, attempt.user_id, attempt.email = "totp_setup_required", user.id, user.email
        _release_attempt(db, user, None)
        db.commit()
        return JSONResponse(status_code=401, content={"detail": "totp_setup_required", "setup_token": issue_enrol_token(user)})

    user.failed_logins, user.locked_until, user.last_login_at = 0, None, now
    if _ph.check_needs_rehash(user.password_hash):
        user.password_hash = hash_password(body.password)
    attempt.event, attempt.user_id, attempt.email = "login_ok", user.id, user.email
    db.commit()
    set_session_cookie(response, user)
    return _me(user)


def _claim_attempt(db: Session, user_id: int) -> int:
    """Atomically count one sign-in attempt against the account and return the new count (concurrent requests get distinct numbers)."""
    return db.execute(update(User).where(User.id == user_id).values(failed_logins=func.coalesce(User.failed_logins, 0) + 1)
                      .returning(User.failed_logins)).scalar_one()


def _release_attempt(db: Session, user: User, attempt) -> None:
    """The password was right but the attempt is not finished (2FA code still to come): give the claimed attempt back."""
    if attempt is not None:
        db.delete(attempt)
    db.execute(update(User).where(User.id == user.id, User.failed_logins > 0).values(failed_logins=User.failed_logins - 1))


def _register_failure(db: Session, user: User, now, claimed: int | None = None) -> None:
    """A failed attempt was already counted by _claim_attempt (login); other callers (2FA enrolment) count it here."""
    if claimed is None:
        claimed = _claim_attempt(db, user.id)
    if claimed >= LOCKOUT_AFTER:
        db.execute(update(User).where(User.id == user.id).values(locked_until=now + timedelta(minutes=LOCKOUT_MINUTES), failed_logins=0))


@router.get("/me")
def me(user: User = Depends(current_user)) -> dict:
    return _me(user)


@router.post("/logout")
def logout(request: Request, response: Response, user: User | None = Depends(session_user), db: Session = Depends(get_db)) -> dict:
    csrf_guard(request)
    if user is not None:                           # end the session on the server too, so a copied cookie stops working
        user.session_version += 1
        log_event(db, "logout", request, user=user)
        db.commit()
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
