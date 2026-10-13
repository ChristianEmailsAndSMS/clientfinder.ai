"""Sign-in attempts are counted before the slow password check, so simultaneous guesses cannot share a stale counter."""
from sqlalchemy import func, select

from app import security as sec
from app.api import auth as auth_api
from app.models import AuthEvent, User
from tests.authkit import CSRF, PW, login, make_env, make_user


def test_overlapping_wrong_guesses_still_lock_the_account(monkeypatch):
    env = make_env(monkeypatch)
    with env.scope() as db:
        make_user(db, "v@example.com", totp=False)
    real = sec.verify_password
    state = {"busy": False}

    def verify_while_others_run(stored, password):
        # The first request is "still hashing" while four more wrong guesses arrive and finish.
        if not state["busy"]:
            state["busy"] = True
            for n in range(4):
                assert env.client.post("/auth/login", json={"email": "v@example.com", "password": f"wrong guess {n} xx"}, headers=CSRF).status_code == 401
        return real(stored, password)
    monkeypatch.setattr(auth_api, "verify_password", verify_while_others_run)
    assert env.client.post("/auth/login", json={"email": "v@example.com", "password": "wrong guess outer"}, headers=CSRF).status_code == 401
    monkeypatch.setattr(auth_api, "verify_password", real)
    with env.scope() as db:
        u = db.scalar(select(User).where(User.email == "v@example.com"))
        assert u.locked_until is not None                      # 5 failures in total, however they overlapped
    assert login(env, env.client, "v@example.com").status_code == 429   # even the right password is refused while locked, and the answer says so


def test_ip_limit_counts_attempts_still_in_flight(monkeypatch):
    env = make_env(monkeypatch)
    with env.scope() as db:
        make_user(db, "w@example.com", totp=False)
    seen = {}
    real = sec.verify_password

    def hook(stored, password):
        if "n" not in seen:                                    # inside the first attempt: it must already count against the IP
            seen["n"] = 1
            with env.scope() as db:
                seen["rows"] = db.scalar(select(func.count(AuthEvent.id)).where(AuthEvent.event == "login_fail"))
        return real(stored, password)
    monkeypatch.setattr(auth_api, "verify_password", hook)
    env.client.post("/auth/login", json={"email": "w@example.com", "password": "nope nope nope 1"}, headers=CSRF)
    assert seen["rows"] == 1


def test_good_login_leaves_no_failure_trace(monkeypatch):
    env = make_env(monkeypatch)
    with env.scope() as db:
        make_user(db, "x@example.com", totp=False)
    assert login(env, env.client, "x@example.com").status_code == 200
    with env.scope() as db:
        assert db.scalar(select(func.count(AuthEvent.id)).where(AuthEvent.event == "login_fail")) == 0
        assert db.scalar(select(User.failed_logins).where(User.email == "x@example.com")) == 0
