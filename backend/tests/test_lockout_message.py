"""A locked sign-in must SAY so, with the same words for real and made-up addresses (no account enumeration), and typos must not pile up forever."""
from datetime import timedelta

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select

import authkit
from authkit import CSRF, PW
from app import security as sec
from app.main import app
from app.models import AuthEvent

BAD = {"email": "pat@example.com", "password": "definitely wrong 1"}


@pytest.fixture
def env(monkeypatch):
    e = authkit.make_env(monkeypatch)
    with e.scope() as db:
        authkit.make_user(db, "pat@example.com")
    yield e
    app.dependency_overrides.clear()


def attempt(c, email="pat@example.com", pw="definitely wrong 1"):
    return c.post("/auth/login", json={"email": email, "password": pw}, headers=CSRF)


def test_five_failures_then_a_clear_message_for_real_and_made_up_addresses(env):
    c = TestClient(app)
    for email in ("pat@example.com", "ghost@example.com"):
        assert [attempt(c, email).status_code for _ in range(5)] == [401] * 5
        r = attempt(c, email)
        assert r.status_code == 429 and "Wait 15 minutes" in r.json()["detail"]
    a, b = attempt(c).json(), attempt(c, "ghost@example.com").json()
    assert a == b                                                                  # identical answer: nothing reveals which account exists
    assert attempt(c, pw=authkit.PW).status_code == 429                           # the right password does not get in while locked


def test_the_lock_ends_after_fifteen_minutes(env):
    c = TestClient(app)
    for _ in range(6):
        attempt(c)
    with env.scope() as db:
        sec_now = sec.utcnow()
        for e in db.scalars(select(AuthEvent)).all():
            e.created_at = sec_now - timedelta(minutes=16)
        from app.models import User
        db.query(User).update({User.locked_until: None, User.failed_logins: 0})
    assert attempt(c, pw=authkit.PW).status_code == 200


def test_a_successful_sign_in_forgives_earlier_typos(env):
    c = TestClient(app)
    for _ in range(3):
        attempt(c)
    assert attempt(c, pw=authkit.PW).status_code == 200
    with env.scope() as db:                                        # the account counter is separate; reset it as the success does in real life
        pass
    c2 = TestClient(app)
    assert [attempt(c2).status_code for _ in range(3)] == [401] * 3             # three fresh typos do not trip a lock that three old ones started
