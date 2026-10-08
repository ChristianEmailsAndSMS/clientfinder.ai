"""Authentication primitives: passwords, sessions, 2FA, request guards, audit log.

Design rules:
  * Passwords: argon2id. A dummy hash is verified for unknown emails so login timing does not reveal who has an account.
  * Sessions: signed JWT in an HttpOnly + Secure + SameSite=Strict cookie. Each token carries the user's
    `session_version`; bumping it ("log out everywhere", password change) revokes every token at once.
  * 2FA: TOTP, secret encrypted at rest, a code can be used once (replay protection). Mandatory for admins.
  * CSRF: SameSite=Strict + a required custom header on every state-changing request + Origin check.
  * Throttling: per-account lockout and per-IP limit, both backed by the database (survives restarts).
"""
from __future__ import annotations

import base64
import hashlib
import hmac
import re
import secrets
import threading
import time
from datetime import datetime, timedelta, timezone

import jwt
import pyotp
from argon2 import PasswordHasher
from argon2.exceptions import InvalidHashError, VerificationError
from cryptography.fernet import Fernet, InvalidToken, MultiFernet
from fastapi import Depends, HTTPException, Request, Response
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from .config import settings
from .db import get_db
from .models import AuthEvent, User

COOKIE_NAME = "cf_session"
CSRF_HEADER = "x-requested-with"
CSRF_VALUE = "clientfinder"
LOCKOUT_AFTER = 5
LOCKOUT_MINUTES = 15
IP_FAIL_LIMIT = 20          # failed attempts per IP per window
IP_FAIL_WINDOW = timedelta(minutes=10)
ISSUER = "Clientfinder.ai"

_ph = PasswordHasher()
_DUMMY_HASH = _ph.hash(secrets.token_urlsafe(16))
_EMAIL_RE = re.compile(r"^[^@\s]{1,64}@[^@\s]{1,255}\.[^@\s]{2,}$")
_COMMON = {"password", "passw0rd", "123456789", "1234567890", "qwertyuiop", "letmein", "iloveyou", "administrator",
           "welcome1", "clientfinder", "emailsandsms", "changeme", "password1", "qwerty123"}


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


def aware(dt: datetime | None) -> datetime | None:
    return dt if dt is None or dt.tzinfo else dt.replace(tzinfo=timezone.utc)


# ---------- configuration ----------
def secrets_ready() -> tuple[bool, str]:
    s = settings.jwt_secret
    if s == "change-me-dev-only" or len(s) < 32:
        return False, "JWT_SECRET is unset, default, or shorter than 32 characters"
    return True, ""


def require_secrets() -> None:
    ok, why = secrets_ready()
    if not ok:
        raise HTTPException(503, f"Server not configured: {why}")


def admin_emails() -> set[str]:
    return {e.strip().lower() for e in settings.admin_emails.split(",") if e.strip()}


def _subkey(label: bytes) -> bytes:
    return hmac.new(settings.jwt_secret.encode(), label, hashlib.sha256).digest()


# ---------- email / password ----------
def normalize_email(email: str) -> str:
    e = (email or "").strip().lower()
    if len(e) > 320 or not _EMAIL_RE.match(e):
        raise HTTPException(422, "Enter a valid email address")
    return e


def password_problem(password: str, email: str, admin: bool = False) -> str | None:
    """Human-readable reason a password is unacceptable, or None."""
    need = 14 if admin else 12
    if len(password) < need:
        return f"Use at least {need} characters"
    if len(password) > 256:
        return "Password is too long (max 256)"
    low = password.lower()
    local = email.split("@")[0].lower()
    letters = re.sub(r"[^a-z]", "", low)
    if (len(set(low)) < 5
            or re.fullmatch(r"(.{1,8}?)\1+", low)                                   # abcabcabc..., passwordpassword
            or any(c in low and len(low) - len(c) <= 6 for c in _COMMON)             # password1234!, welcome1xyz
            or (letters and any(c.isalpha() and c in letters and len(letters) - len(c) <= 2 for c in _COMMON))):
        return "That password is too easy to guess"
    squashed, local_squashed = re.sub(r"[^a-z0-9]", "", low), re.sub(r"[^a-z0-9]", "", local)   # new.person == newperson
    if len(local_squashed) >= 4 and local_squashed in squashed:
        return "Do not include your email name in the password"
    return None


def hash_password(password: str) -> str:
    return _ph.hash(password)


def verify_password(stored_hash: str | None, password: str) -> bool:
    """Constant-ish work whether or not the account exists (pass stored_hash=None for unknown users)."""
    try:
        return _ph.verify(stored_hash or _DUMMY_HASH, password) and stored_hash is not None
    except (VerificationError, InvalidHashError):
        return False


# ---------- 2FA ----------
def _fernet() -> MultiFernet:
    """Encrypts with the newest key, decrypts with any: DATA_ENCRYPTION_KEY, then the previous one, then the
    legacy key derived from JWT_SECRET (so secrets written before DATA_ENCRYPTION_KEY was set stay readable)."""
    def key(material: bytes) -> Fernet:
        return Fernet(base64.urlsafe_b64encode(hmac.new(material, b"totp-at-rest", hashlib.sha256).digest()))
    keys = [key(m.encode()) for m in (settings.data_encryption_key, settings.data_encryption_key_previous) if len(m) >= 32]
    keys.append(key(settings.jwt_secret.encode()))
    return MultiFernet(keys)


def new_totp_secret() -> str:
    return pyotp.random_base32()


def encrypt_secret(secret: str) -> str:
    return _fernet().encrypt(secret.encode()).decode()


def decrypt_secret(token: str) -> str | None:
    try:
        return _fernet().decrypt(token.encode()).decode()
    except InvalidToken:
        return None


def totp_uri(secret: str, email: str) -> str:
    return pyotp.TOTP(secret).provisioning_uri(name=email, issuer_name=ISSUER)


def qr_data_uri(uri: str) -> str:
    import segno
    return segno.make(uri, error="m").svg_data_uri(scale=5, border=2, dark="#000", light="#fff")


def check_totp(user: User, code: str, *, now: float | None = None) -> bool:
    """True if `code` is valid for the user and has not been used before. Marks the step as used."""
    code = re.sub(r"\s+", "", code or "")
    if not re.fullmatch(r"\d{6}", code) or not user.totp_secret_enc:
        return False
    secret = decrypt_secret(user.totp_secret_enc)
    if not secret:
        return False
    totp = pyotp.TOTP(secret)
    now = time.time() if now is None else now
    base_step = int(now // 30)
    for step in (base_step - 1, base_step, base_step + 1):          # +-30s clock drift
        if hmac.compare_digest(totp.at(step * 30), code):
            if user.totp_last_step is not None and step <= user.totp_last_step:
                return False                                        # replay of an already-used code
            user.totp_last_step = step
            return True
    return False


# ---------- sessions ----------
def issue_session(user: User) -> tuple[str, int]:
    ttl = settings.session_hours * 3600
    now = int(time.time())
    token = jwt.encode({"sub": str(user.id), "sv": user.session_version, "iat": now, "exp": now + ttl, "iss": ISSUER},
                       _subkey(b"session-jwt"), algorithm="HS256")
    return token, ttl


def read_session(token: str | None) -> dict | None:
    if not token:
        return None
    try:
        return jwt.decode(token, _subkey(b"session-jwt"), algorithms=["HS256"], issuer=ISSUER,
                          options={"require": ["exp", "iat", "sub", "sv"]})
    except jwt.PyJWTError:
        return None


ENROL_MINUTES = 10


def issue_enrol_token(user: User) -> str:
    """Short-lived proof that the password was just verified, good only for enrolling 2FA (it is NOT a session)."""
    now = int(time.time())
    return jwt.encode({"sub": str(user.id), "sv": user.session_version, "iat": now, "exp": now + ENROL_MINUTES * 60,
                       "iss": ISSUER, "purpose": "2fa-enrol"}, _subkey(b"2fa-enrol-jwt"), algorithm="HS256")


def read_enrol_token(token: str | None) -> dict | None:
    if not token:
        return None
    try:
        claims = jwt.decode(token, _subkey(b"2fa-enrol-jwt"), algorithms=["HS256"], issuer=ISSUER,
                            options={"require": ["exp", "iat", "sub", "sv", "purpose"]})
    except jwt.PyJWTError:
        return None
    return claims if claims.get("purpose") == "2fa-enrol" else None


def set_session_cookie(response: Response, user: User) -> None:
    token, ttl = issue_session(user)
    response.set_cookie(COOKIE_NAME, token, max_age=ttl, httponly=True, secure=settings.cookie_secure,
                        samesite="strict", path="/")


def clear_session_cookie(response: Response) -> None:
    response.delete_cookie(COOKIE_NAME, path="/", httponly=True, secure=settings.cookie_secure, samesite="strict")


# ---------- audit log + throttling ----------
def client_ip(request: Request) -> str:
    return (request.client.host if request.client else "unknown")[:64]


def log_event(db: Session, event: str, request: Request | None = None, *, user: User | None = None,
              email: str | None = None, detail: str | None = None) -> None:
    db.add(AuthEvent(
        event=event, email=(email or (user.email if user else None)), user_id=user.id if user else None,
        ip=client_ip(request) if request else None,
        user_agent=(request.headers.get("user-agent", "")[:200] if request else None), detail=(detail or None) and detail[:300],
    ))


def ip_throttled(db: Session, ip: str) -> bool:
    n = db.scalar(select(func.count(AuthEvent.id)).where(
        AuthEvent.ip == ip, AuthEvent.event.in_(("login_fail", "totp_fail", "setup_fail")),
        AuthEvent.created_at >= utcnow() - IP_FAIL_WINDOW)) or 0
    return n >= IP_FAIL_LIMIT


# ---------- request guards ----------
def csrf_guard(request: Request) -> None:
    """State-changing requests must carry the custom header (which a cross-site form cannot set) and,
    when the browser sends an Origin, it must be this site."""
    if request.method in ("GET", "HEAD", "OPTIONS"):
        return
    if request.headers.get(CSRF_HEADER) != CSRF_VALUE:
        raise HTTPException(403, "Missing CSRF header")
    origin = request.headers.get("origin")
    if origin and origin.split("://", 1)[-1] != request.headers.get("host"):
        raise HTTPException(403, "Cross-site request blocked")


def session_user(request: Request, db: Session = Depends(get_db)) -> User | None:
    claims = read_session(request.cookies.get(COOKIE_NAME))
    if not claims:
        return None
    try:
        user = db.get(User, int(claims["sub"]))
    except (ValueError, TypeError):
        return None
    if not user or not user.is_active or user.session_version != claims["sv"]:
        return None
    return user


def current_user(user: User | None = Depends(session_user)) -> User:
    if user is None:
        raise HTTPException(401, "Not signed in")
    return user


def current_admin(user: User = Depends(current_user)) -> User:
    """An admin must be flagged, allow-listed by email, AND have 2FA on (unless the owner turned ADMIN_REQUIRE_2FA off)."""
    if not (user.is_admin and (user.totp_enabled or not settings.admin_require_2fa) and user.email in admin_emails()):
        raise HTTPException(403, "Admin access required")
    return user


SIGNUPS_PER_IP_PER_HOUR = 5
SIGNUPS_GLOBAL_PER_HOUR = 200


def signup_throttle_reason(db: Session, ip: str) -> str | None:
    since = utcnow() - timedelta(hours=1)
    mine = db.scalar(select(func.count(AuthEvent.id)).where(AuthEvent.event == "signup", AuthEvent.ip == ip, AuthEvent.created_at >= since)) or 0
    if mine >= SIGNUPS_PER_IP_PER_HOUR:
        return "Too many accounts created from this address. Try again in an hour."
    total = db.scalar(select(func.count(AuthEvent.id)).where(AuthEvent.event == "signup", AuthEvent.created_at >= since)) or 0
    if total >= SIGNUPS_GLOBAL_PER_HOUR:
        return "Sign-ups are very busy right now. Try again in a little while."
    return None


# ---------- public rate limit ----------
class RateLimiter:
    """Sliding-window counter per key, in memory (one API process). Enough to stop casual scraping; real abuse
    protection for many workers would move this to Redis."""

    def __init__(self) -> None:
        self._hits: dict[str, list[float]] = {}
        self._lock = threading.Lock()

    def allow(self, key: str, limit: int, window: float = 60.0, now: float | None = None) -> bool:
        now = time.monotonic() if now is None else now
        with self._lock:
            hits = [t for t in self._hits.get(key, ()) if t > now - window]
            if len(hits) >= limit:
                self._hits[key] = hits
                return False
            hits.append(now)
            self._hits[key] = hits
            if len(self._hits) > 20_000:                                  # bound memory: drop idle keys
                for k in [k for k, v in self._hits.items() if not v or v[-1] <= now - window]:
                    self._hits.pop(k, None)
            return True


_public_limiter = RateLimiter()


def rate_limit_guard(request: Request) -> None:
    """Per-IP limit for endpoints anyone can call."""
    limit = settings.public_rate_limit_per_min
    if limit and not _public_limiter.allow(client_ip(request), limit):
        raise HTTPException(429, "Too many requests. Slow down.", headers={"Retry-After": "60"})


def public_guard(request: Request, user: User | None = Depends(session_user)) -> None:
    """Dependency for the job endpoints: login requirement (default on) + per-IP rate limit."""
    if settings.jobs_require_login and user is None:
        raise HTTPException(401, "Sign in to view jobs")
    rate_limit_guard(request)
