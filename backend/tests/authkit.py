"""Shared helpers for account/admin tests: a sqlite-backed app environment, user factory and login."""
import time
from contextlib import contextmanager
from types import SimpleNamespace

import pyotp
from argon2 import PasswordHasher
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from app import security as sec
from app.config import settings
from app.db import Base, get_db
from app.main import app
from app.models import User
from app import live_search
from app.api import public as public_api
from app.scrapers import common, pipeline, query_plan

PW = "correct horse battery staple"
CSRF = {"X-Requested-With": "clientfinder"}


def make_env(monkeypatch):
    engine = create_engine("sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool)
    Base.metadata.create_all(engine)
    Session = sessionmaker(bind=engine, autoflush=False)

    @contextmanager
    def scope():
        s = Session()
        try:
            yield s
            s.commit()
        except Exception:
            s.rollback()
            raise
        finally:
            s.close()

    def override():
        s = Session()
        try:
            yield s
        finally:
            s.close()

    for mod in (pipeline, common, query_plan, live_search):
        monkeypatch.setattr(mod, "session_scope", scope)
    app.dependency_overrides[get_db] = override
    fast = PasswordHasher(time_cost=1, memory_cost=8, parallelism=1)         # tests do hundreds of hashes
    monkeypatch.setattr(sec, "_ph", fast)
    monkeypatch.setattr(sec, "_DUMMY_HASH", fast.hash("dummy-for-timing"))
    monkeypatch.setattr(sec, "_public_limiter", sec.RateLimiter())            # no cross-test rate-limit bleed
    public_api._cache.clear()
    monkeypatch.setattr(settings, "jwt_secret", "k" * 48)
    monkeypatch.setattr(settings, "cookie_secure", False)                    # TestClient speaks http
    monkeypatch.setattr(settings, "admin_emails", "christian@emailsandsms.com")
    monkeypatch.setattr(settings, "dev_fixtures", False)
    monkeypatch.setattr(settings, "anthropic_api_key", "k")
    return SimpleNamespace(scope=scope, client=TestClient(app))


def make_user(db, email, password=PW, *, admin=False, totp=None, active=True, balance_micro=0):
    """Create a user directly in the DB. totp=None -> admins get 2FA, customers don't. Returns (user_id, totp_secret|None)."""
    if totp is None:
        totp = admin
    secret = sec.new_totp_secret() if totp else None
    u = User(email=email, password_hash=sec.hash_password(password), is_admin=admin, is_active=active,
             totp_secret_enc=sec.encrypt_secret(secret) if secret else None, totp_enabled=bool(secret), balance_micro=balance_micro)
    db.add(u)
    db.flush()
    return u.id, secret


def totp_now(secret):
    return pyotp.TOTP(secret).at(time.time())


def login(env, client, email, password=PW, secret=None):
    """Sign `client` in (resetting the 2FA replay marker first so tests can log in repeatedly)."""
    if secret:
        with env.scope() as db:
            u = db.query(User).filter(User.email == email).one()
            u.totp_last_step = None
    body = {"email": email, "password": password}
    if secret:
        body["totp_code"] = totp_now(secret)
    return client.post("/auth/login", json=body, headers=CSRF)
