"""Behavioural tests for guards that mutation testing showed could be removed with the suite staying green."""
import pyotp
import pytest

import authkit
from authkit import CSRF, PW, login, make_user
from app import security as sec
from app.config import settings
from app.main import app
from app.models import CreditEntry, User


@pytest.fixture
def env(monkeypatch):
    e = authkit.make_env(monkeypatch)
    yield e
    app.dependency_overrides.clear()


def _client():
    from fastapi.testclient import TestClient
    return TestClient(app)


def test_credits_ledger_shows_only_own_rows(env):
    with env.scope() as db:
        a, _ = make_user(db, "a.customer@example.com")
        b, _ = make_user(db, "b.customer@example.com")
        for uid, why in ((a, "Search: alpha-secret"), (b, "Search: bravo-secret")):
            db.add(CreditEntry(user_id=uid, kind="spend", delta_micro=-1000, balance_after_micro=0, reason=why))
    assert login(env, env.client, "a.customer@example.com").status_code == 200
    r = env.client.get("/account/credits")
    assert r.status_code == 200
    reasons = [e["reason"] for e in r.json()["entries"]]
    assert "Search: alpha-secret" in reasons
    assert "Search: bravo-secret" not in reasons


def test_profile_save_needs_csrf_header(env):
    with env.scope() as db:
        make_user(db, "p.customer@example.com")
    assert login(env, env.client, "p.customer@example.com").status_code == 200
    assert env.client.put("/account/profile", json={"about": "x", "links": ""}).status_code == 403
    assert env.client.put("/account/profile", json={"about": "x", "links": ""}, headers=CSRF).status_code == 200


def test_password_length_limits():
    assert sec.password_problem("Xk7!mQ2zRp9", "z@example.com")              # 11 chars
    assert sec.password_problem("Xk7!mQ2zRp9w", "z@example.com") is None      # 12 chars
    assert sec.password_problem("Xk7!mQ2zRp9wT", "z@example.com", admin=True)           # 13 chars
    assert sec.password_problem("Xk7!mQ2zRp9wTu", "z@example.com", admin=True) is None  # 14 chars
    assert sec.password_problem("Xk7!mQ2zRp9w" * 22, "z@example.com")        # 264 chars, too long
    assert sec.password_problem("Xk7!mQ2zRp9w" + "Ab3$" * 61, "z@example.com") is None   # 256 chars


def test_non_flagged_user_with_admin_email_is_not_admin(env):
    with env.scope() as db:
        _, secret = make_user(db, "christian@emailsandsms.com", admin=False, totp=True)
    assert login(env, env.client, "christian@emailsandsms.com", secret=secret).status_code == 200
    assert env.client.get("/account/credits").status_code == 200        # signed in...
    assert env.client.get("/admin/users").status_code == 403            # ...but not an admin


def test_jwt_secret_must_be_32_chars(monkeypatch):
    monkeypatch.setattr(settings, "jwt_secret", "k" * 20)
    assert sec.secrets_ready()[0] is False
    monkeypatch.setattr(settings, "jwt_secret", "k" * 32)
    assert sec.secrets_ready()[0] is True


def test_totp_window_is_one_step_each_way(env):
    with env.scope() as db:
        uid, secret = make_user(db, "t.admin@example.com", admin=True)
    t = pyotp.TOTP(secret)
    base = 1_800_000_000.0
    step = int(base // 30)
    for offset, ok in ((-3, False), (-2, False), (-1, True), (1, True), (2, False), (3, False)):
        with env.scope() as db:
            u = db.get(User, uid)
            u.totp_last_step = None
            assert sec.check_totp(u, t.at((step + offset) * 30), now=base) is ok, offset


def test_admin_logout_everywhere_and_unlock(env):
    with env.scope() as db:
        _, asecret = make_user(db, "christian@emailsandsms.com", admin=True)
        vid, _ = make_user(db, "victim@example.com")
    victim = _client()
    assert login(env, victim, "victim@example.com").status_code == 200
    assert victim.get("/account/credits").status_code == 200
    assert login(env, env.client, "christian@emailsandsms.com", secret=asecret).status_code == 200
    r = env.client.post(f"/admin/users/{vid}/logout-everywhere", headers=CSRF)
    assert r.status_code == 200
    assert victim.get("/account/credits").status_code == 401
    with env.scope() as db:
        u = db.get(User, vid)
        u.failed_logins = 3
    assert env.client.post(f"/admin/users/{vid}/unlock", headers=CSRF).status_code == 200
    with env.scope() as db:
        assert db.get(User, vid).failed_logins == 0
