"""Account setup, login hardening, sessions, CSRF, authorization, credits API and dashboard files."""
import re
import time
from datetime import timedelta
from pathlib import Path

import jwt
import pyotp
import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select

import authkit
from authkit import CSRF, PW, login, make_user, totp_now
from app import security as sec
from app.config import settings
from app.main import app
from app.models import AuthEvent, CreditEntry, User

EMAIL = "christian@emailsandsms.com"
STATIC = Path(__file__).resolve().parent.parent / "app" / "static" / "admin"


class SimpleNs:
    def __init__(self, **kw):
        self.__dict__.update(kw)


@pytest.fixture
def env(monkeypatch):
    e = authkit.make_env(monkeypatch)
    yield e
    app.dependency_overrides.clear()


def signup(client, email="new.person@example.com", password=PW):
    return client.post("/auth/signup", json={"email": email, "password": password}, headers=CSRF)


# =============== customer sign-up ===============
def test_signup_creates_an_account_and_signs_the_person_in(env):
    r = signup(env.client)
    assert r.status_code == 201
    assert r.json() == {"id": r.json()["id"], "email": "new.person@example.com", "is_admin": False, "totp_enabled": False, "balance_usd": 0.0}
    assert "cf_session" in r.cookies
    assert env.client.get("/auth/me").json()["email"] == "new.person@example.com"
    with env.scope() as db:
        u = db.scalar(select(User))
        assert u.password_hash.startswith("$argon2id$") and PW not in u.password_hash
        assert (u.is_admin, u.is_active, u.balance_micro) == (False, True, 0)
        assert [e.event for e in db.scalars(select(AuthEvent))] == ["signup"]


def test_signup_email_is_normalised_and_unique(env):
    assert signup(env.client, "  Mixed.Case@Example.COM ").status_code == 201
    r = signup(TestClient(app), "mixed.case@example.com")
    assert r.status_code == 409 and "already exists" in r.json()["detail"]
    with env.scope() as db:
        assert db.scalar(select(User.email)) == "mixed.case@example.com"


def test_signup_can_then_sign_in_later(env):
    signup(env.client, "later@example.com")
    other = TestClient(app)
    assert login(env, other, "later@example.com").status_code == 200
    assert other.get("/auth/me").status_code == 200


@pytest.mark.parametrize("email", ["", "not-an-email", "a@b", "two@@example.com", "spaces in@example.com", "x" * 400 + "@example.com"])
def test_signup_rejects_bad_emails(env, email):
    assert signup(env.client, email).status_code in (422,)


@pytest.mark.parametrize("pw", ["short", "a" * 20, "passwordpassword", "newperson-secret-1"])
def test_signup_enforces_password_policy(env, pw):
    assert signup(env.client, "new.person@example.com", pw).status_code == 422


def test_owner_address_cannot_be_registered_in_the_browser(env):
    r = signup(env.client, EMAIL)
    assert r.status_code == 403 and "reserved" in r.json()["detail"]
    r = signup(env.client, EMAIL.upper())
    assert r.status_code == 403
    with env.scope() as db:
        assert db.scalar(select(User)) is None


def test_signup_needs_the_csrf_header_and_a_configured_server(env, monkeypatch):
    assert env.client.post("/auth/signup", json={"email": "a@example.com", "password": PW}).status_code == 403
    monkeypatch.setattr(settings, "jwt_secret", "change-me-dev-only")
    assert signup(env.client).status_code == 503


def test_signup_is_throttled_per_ip_and_globally(env, monkeypatch):
    for i in range(sec.SIGNUPS_PER_IP_PER_HOUR):
        assert signup(TestClient(app), f"user{i}@example.com").status_code == 201
    r = signup(TestClient(app), "one.too.many@example.com")
    assert r.status_code == 429 and "hour" in r.json()["detail"]
    with env.scope() as db:                                     # a different IP is fine until the global ceiling
        db.query(AuthEvent).update({AuthEvent.ip: "203.0.113.9"})
    assert signup(TestClient(app), "other.ip@example.com").status_code == 201
    monkeypatch.setattr(sec, "SIGNUPS_GLOBAL_PER_HOUR", 3)
    assert signup(TestClient(app), "busy@example.com").status_code == 429


def test_signup_bonus_is_off_by_default_and_works_when_enabled(env, monkeypatch):
    assert signup(env.client, "nobonus@example.com").json()["balance_usd"] == 0.0
    monkeypatch.setattr(settings, "signup_bonus_usd", 2.5)
    r = signup(TestClient(app), "bonus@example.com")
    assert r.json()["balance_usd"] == 2.5
    with env.scope() as db:
        e = db.scalar(select(CreditEntry))
        assert (e.kind, e.delta_micro, e.reason) == ("grant", 2_500_000, "Welcome credit")


def test_customers_never_get_admin_access(env):
    signup(env.client)
    for path in ("/admin/overview", "/admin/users", "/admin/sources"):
        assert env.client.get(path).status_code == 403
    assert env.client.post("/admin/retag", headers=CSRF).status_code == 403


def test_auth_status_reports_configuration(env, monkeypatch):
    assert env.client.get("/auth/status").json() == {"auth_configured": True, "problem": None}
    monkeypatch.setattr(settings, "jwt_secret", "short")
    s = env.client.get("/auth/status").json()
    assert s["auth_configured"] is False and "JWT_SECRET" in s["problem"]
    assert env.client.post("/auth/login", json={"email": EMAIL, "password": PW}, headers=CSRF).status_code == 503


# =============== admin: first sign-in turns on 2FA ===============
@pytest.fixture
def fresh_admin(env):
    """The owner account as created by `admin_cli.py create-admin`: admin flag, password, no 2FA yet."""
    with env.scope() as db:
        uid, _ = make_user(db, EMAIL, admin=True, totp=False)
    return SimpleNs(id=uid)


def first_login(env):
    r = env.client.post("/auth/login", json={"email": EMAIL, "password": PW}, headers=CSRF)
    return r, (r.json().get("setup_token") if r.status_code == 401 else None)


def test_first_admin_login_returns_an_enrolment_token_not_a_session(env, fresh_admin):
    r, token = first_login(env)
    assert r.status_code == 401 and r.json()["detail"] == "totp_setup_required" and token
    assert "cf_session" not in r.cookies
    assert env.client.get("/auth/me").status_code == 401 and env.client.get("/admin/overview").status_code == 401
    assert env.client.cookies.get("cf_session") is None


def test_full_admin_enrolment_flow(env, fresh_admin):
    _, token = first_login(env)
    s = env.client.post("/auth/2fa/start", json={"setup_token": token}, headers=CSRF)
    assert s.status_code == 200
    body = s.json()
    assert body["otpauth_uri"].startswith("otpauth://totp/") and body["qr"].startswith("data:image/svg+xml") and len(body["totp_secret"]) >= 16
    with env.scope() as db:
        u = db.scalar(select(User))
        assert u.totp_enabled is False and body["totp_secret"] not in u.totp_secret_enc          # pending, encrypted
    wrong = "000000" if totp_now(body["totp_secret"]) != "000000" else "111111"
    assert env.client.post("/auth/2fa/confirm", json={"setup_token": token, "totp_code": wrong}, headers=CSRF).status_code == 403
    assert env.client.get("/auth/me").status_code == 401
    c = env.client.post("/auth/2fa/confirm", json={"setup_token": token, "totp_code": totp_now(body["totp_secret"])}, headers=CSRF)
    assert c.status_code == 200 and c.json()["is_admin"] is True and c.json()["totp_enabled"] is True
    assert env.client.get("/admin/overview").status_code == 200                                   # now a real admin session
    again = TestClient(app)                                                                       # later sign-ins need password + code
    r = again.post("/auth/login", json={"email": EMAIL, "password": PW}, headers=CSRF)
    assert r.status_code == 401 and r.json()["detail"] == "totp_required"


def test_enrolment_token_cannot_be_reused_forged_or_expired(env, fresh_admin, monkeypatch):
    _, token = first_login(env)
    secret = env.client.post("/auth/2fa/start", json={"setup_token": token}, headers=CSRF).json()["totp_secret"]
    env.client.post("/auth/2fa/confirm", json={"setup_token": token, "totp_code": totp_now(secret)}, headers=CSRF)
    other = TestClient(app)
    for tok in (token, token[:-3] + "abc", "garbage", ""):                                       # already enrolled / tampered
        assert other.post("/auth/2fa/start", json={"setup_token": tok}, headers=CSRF).status_code == 401
    # a normal session cookie is not an enrolment token, and an enrolment token is not a session
    session = env.client.cookies["cf_session"]
    assert other.post("/auth/2fa/start", json={"setup_token": session}, headers=CSRF).status_code == 401
    other.cookies.set("cf_session", token)
    assert other.get("/auth/me").status_code == 401
    # expired
    with env.scope() as db:
        db.query(User).update({User.totp_enabled: False, User.totp_secret_enc: None})
    monkeypatch.setattr(sec, "ENROL_MINUTES", -1)
    _, expired = first_login(env)
    assert other.post("/auth/2fa/start", json={"setup_token": expired}, headers=CSRF).status_code == 401


def test_enrolment_token_dies_when_sessions_are_revoked(env, fresh_admin):
    _, token = first_login(env)
    with env.scope() as db:
        db.query(User).update({User.session_version: User.session_version + 1})
    assert env.client.post("/auth/2fa/start", json={"setup_token": token}, headers=CSRF).status_code == 401


def test_enrolment_needs_csrf_and_is_throttled(env, fresh_admin):
    _, token = first_login(env)
    assert env.client.post("/auth/2fa/start", json={"setup_token": token}).status_code == 403
    env.client.post("/auth/2fa/start", json={"setup_token": token}, headers=CSRF)
    for _ in range(sec.LOCKOUT_AFTER):
        env.client.post("/auth/2fa/confirm", json={"setup_token": token, "totp_code": "000000"}, headers=CSRF)
    with env.scope() as db:
        assert db.scalar(select(User)).locked_until is not None                                    # wrong codes lock the account too


def test_admin_flag_alone_is_not_enough(env):
    with env.scope() as db:
        make_user(db, "impostor@example.com", admin=True, totp=False)                            # not in ADMIN_EMAILS
    r = env.client.post("/auth/login", json={"email": "impostor@example.com", "password": PW}, headers=CSRF)
    assert r.status_code == 403
    assert "setup_token" not in r.text


# =============== login ===============
@pytest.fixture
def admin(env):
    with env.scope() as db:
        uid, secret = make_user(db, EMAIL, admin=True)
    return SimpleNs(id=uid, secret=secret)



def test_login_success_sets_a_locked_down_cookie(env, admin, monkeypatch):
    monkeypatch.setattr(settings, "cookie_secure", True)
    r = login(env, env.client, EMAIL, secret=admin.secret)
    assert r.status_code == 200
    cookie = r.headers["set-cookie"].lower()
    assert "httponly" in cookie and "samesite=strict" in cookie and "secure" in cookie and "path=/" in cookie


def test_wrong_password_unknown_email_and_wrong_2fa_look_identical(env, admin):
    msgs = set()
    msgs.add(login(env, env.client, EMAIL, "wrong password here", secret=admin.secret).json()["detail"])
    msgs.add(login(env, env.client, "nobody@example.com", PW).json()["detail"])
    r = env.client.post("/auth/login", json={"email": EMAIL, "password": PW, "totp_code": "000000"}, headers=CSRF)
    msgs.add(r.json()["detail"])
    assert msgs == {"Invalid email, password or code"}


def test_login_asks_for_2fa_only_after_the_password_is_right(env, admin):
    r = env.client.post("/auth/login", json={"email": EMAIL, "password": PW}, headers=CSRF)
    assert r.status_code == 401 and r.json()["detail"] == "totp_required"
    assert "cf_session" not in r.cookies


def test_2fa_code_cannot_be_replayed(env, admin):
    code = totp_now(admin.secret)
    body = {"email": EMAIL, "password": PW, "totp_code": code}
    with env.scope() as db:
        db.query(User).update({User.totp_last_step: None})
    assert env.client.post("/auth/login", json=body, headers=CSRF).status_code == 200
    other = TestClient(app)
    assert other.post("/auth/login", json=body, headers=CSRF).status_code == 401       # same code, same 30s window


def test_account_locks_after_five_failures_even_for_the_right_password(env, admin):
    for _ in range(5):
        assert login(env, env.client, EMAIL, "wrong password here").status_code == 401
    r = login(env, env.client, EMAIL, secret=admin.secret)
    assert r.status_code == 429
    with env.scope() as db:                                    # lock expires
        db.query(User).update({User.locked_until: sec.utcnow() - timedelta(seconds=1)})
    assert login(env, env.client, EMAIL, secret=admin.secret).status_code == 200
    with env.scope() as db:
        assert db.scalar(select(User)).failed_logins == 0


def test_ip_is_throttled_after_many_failures_across_accounts(env, admin):
    for i in range(sec.IP_FAIL_LIMIT):
        env.client.post("/auth/login", json={"email": f"user{i}@example.com", "password": "whatever-password"}, headers=CSRF)
    r = login(env, env.client, EMAIL, secret=admin.secret)
    assert r.status_code == 429                                # even valid credentials are refused from this IP


def test_suspended_user_cannot_login(env):
    with env.scope() as db:
        make_user(db, "gone@example.com", admin=False, active=False)
    assert login(env, env.client, "gone@example.com").status_code == 401


def test_oversized_inputs_are_rejected_before_hashing(env):
    r = env.client.post("/auth/login", json={"email": EMAIL, "password": "p" * 5000}, headers=CSRF)
    assert r.status_code == 422


def test_login_events_are_audited(env, admin):
    login(env, env.client, EMAIL, "wrong password here")
    login(env, env.client, EMAIL, secret=admin.secret)
    with env.scope() as db:
        events = [e.event for e in db.scalars(select(AuthEvent).order_by(AuthEvent.id))]
    assert events == ["login_fail", "login_ok"]


# =============== sessions ===============
def signed_in(env, admin):
    c = TestClient(app)
    assert login(env, c, EMAIL, secret=admin.secret).status_code == 200
    return c


def test_tampered_forged_and_expired_tokens_are_rejected(env, admin):
    c = signed_in(env, admin)
    good = c.cookies["cf_session"]
    assert c.get("/auth/me").status_code == 200
    for bad in (good[:-3] + "abc", "garbage", jwt.encode({"sub": "1", "sv": 1, "iat": 1, "exp": 9999999999, "iss": "Clientfinder.ai"}, "wrong-key" * 5, algorithm="HS256")):
        c.cookies.set("cf_session", bad)
        assert c.get("/auth/me").status_code == 401
    # alg=none must never be accepted
    none_tok = jwt.encode({"sub": "1", "sv": 1, "iat": 1, "exp": 9999999999, "iss": "Clientfinder.ai"}, None, algorithm="none")
    c.cookies.set("cf_session", none_tok)
    assert c.get("/auth/me").status_code == 401
    expired = jwt.encode({"sub": str(admin.id), "sv": 1, "iat": int(time.time()) - 100, "exp": int(time.time()) - 10, "iss": "Clientfinder.ai"},
                         sec._subkey(b"session-jwt"), algorithm="HS256")
    c.cookies.set("cf_session", expired)
    assert c.get("/auth/me").status_code == 401


def test_logout_everywhere_revokes_other_sessions(env, admin):
    a, b = signed_in(env, admin), TestClient(app)
    with env.scope() as db:
        db.query(User).update({User.totp_last_step: None})
    assert login(env, b, EMAIL, secret=admin.secret).status_code == 200
    assert a.post("/auth/logout-all", headers=CSRF).status_code == 200
    assert b.get("/auth/me").status_code == 401 and a.get("/auth/me").status_code == 401


def test_logout_clears_cookie(env, admin):
    c = signed_in(env, admin)
    assert c.post("/auth/logout", headers=CSRF).status_code == 200
    assert c.get("/auth/me").status_code == 401


# =============== CSRF ===============
def test_state_changing_requests_need_the_custom_header_and_same_origin(env, admin):
    c = signed_in(env, admin)
    assert c.post("/admin/retag").status_code == 403                                        # no header
    assert c.post("/admin/retag", headers={**CSRF, "Origin": "https://evil.example"}).status_code == 403
    assert c.post("/admin/retag", headers={**CSRF, "Origin": "http://testserver"}).status_code == 200
    assert c.post("/admin/retag", headers=CSRF).status_code == 200
    assert c.get("/admin/overview").status_code == 200                                      # reads need no header
    assert env.client.post("/auth/login", json={"email": EMAIL, "password": PW}).status_code == 403


# =============== authorization ===============
def test_customers_and_half_configured_admins_cannot_use_admin_endpoints(env):
    with env.scope() as db:
        make_user(db, "customer@example.com")
        _, s2 = make_user(db, "other-admin@example.com", admin=True)              # admin flag but not in ADMIN_EMAILS
    c = TestClient(app)
    assert login(env, c, "customer@example.com").status_code == 200
    assert c.get("/admin/overview").status_code == 403 and c.get("/admin/users").status_code == 403
    d = TestClient(app)
    assert login(env, d, "other-admin@example.com", secret=s2).status_code == 200
    assert d.get("/admin/overview").status_code == 403


def test_admin_is_refused_if_2fa_gets_turned_off(env, admin):
    c = signed_in(env, admin)
    with env.scope() as db:
        db.query(User).update({User.totp_enabled: False})
    assert c.get("/admin/overview").status_code == 403


# =============== credits through the admin API ===============
def customer(env, balance=0):
    with env.scope() as db:
        uid, _ = make_user(db, "buyer@example.com", balance_micro=balance)
    return uid


def test_admin_grants_and_revokes_credit_with_a_full_audit_trail(env, admin):
    c, uid = signed_in(env, admin), customer(env)
    r = c.post(f"/admin/users/{uid}/credits", json={"amount_usd": "25.50", "kind": "grant", "reason": "launch bonus"}, headers=CSRF)
    assert r.status_code == 200 and r.json()["user"]["balance_usd"] == 25.5
    r = c.post(f"/admin/users/{uid}/credits", json={"amount_usd": 5, "kind": "revoke", "reason": "chargeback"}, headers=CSRF)
    assert r.json()["user"]["balance_usd"] == 20.5
    led = c.get(f"/admin/users/{uid}/ledger").json()
    assert [(e["kind"], e["amount_usd"], e["balance_after_usd"], e["reason"]) for e in led["entries"]] == [
        ("revoke", -5.0, 20.5, "chargeback"), ("grant", 25.5, 25.5, "launch bonus")]
    assert all(e["by"] == admin.id for e in led["entries"])
    events = [e["event"] for e in c.get("/admin/security/events").json()]
    assert "credit_grant" in events and "credit_revoke" in events


@pytest.mark.parametrize("amount", ["0", "-5", "nan", "inf", "abc", "1e400", "0.0000001", "5000", None, "", "1,5"])
def test_bad_credit_amounts_are_refused_and_change_nothing(env, admin, amount):
    c, uid = signed_in(env, admin), customer(env, balance=1_000_000)
    r = c.post(f"/admin/users/{uid}/credits", json={"amount_usd": amount, "kind": "grant", "reason": "test"}, headers=CSRF)
    assert r.status_code == 422, amount
    assert c.get(f"/admin/users/{uid}/ledger").json()["user"]["balance_usd"] == 1.0


def test_cannot_revoke_more_than_the_balance(env, admin):
    c, uid = signed_in(env, admin), customer(env, balance=2_000_000)
    r = c.post(f"/admin/users/{uid}/credits", json={"amount_usd": "2.01", "kind": "revoke", "reason": "oops"}, headers=CSRF)
    assert r.status_code == 409
    assert c.get(f"/admin/users/{uid}/ledger").json()["user"]["balance_usd"] == 2.0


def test_credit_adjustments_need_a_reason_and_valid_kind(env, admin):
    c, uid = signed_in(env, admin), customer(env)
    for body in ({"amount_usd": "1", "kind": "grant", "reason": ""}, {"amount_usd": "1", "kind": "steal", "reason": "abc"},
                 {"amount_usd": "1", "kind": "topup", "reason": "abc"}, {"kind": "grant", "reason": "abc"}):
        assert c.post(f"/admin/users/{uid}/credits", json=body, headers=CSRF).status_code == 422
    assert c.post("/admin/users/9999/credits", json={"amount_usd": "1", "kind": "grant", "reason": "abc"}, headers=CSRF).status_code == 404


def test_suspending_a_user_ends_their_session_and_admin_cannot_suspend_self(env, admin):
    c, uid = signed_in(env, admin), customer(env)
    cust = TestClient(app)
    assert login(env, cust, "buyer@example.com").status_code == 200 and cust.get("/auth/me").status_code == 200
    assert c.post(f"/admin/users/{uid}/active", json={"active": False}, headers=CSRF).status_code == 200
    assert cust.get("/auth/me").status_code == 401
    assert login(env, cust, "buyer@example.com").status_code == 401
    assert c.post(f"/admin/users/{admin.id}/active", json={"active": False}, headers=CSRF).status_code == 409


def test_unlock_endpoint_clears_a_lockout(env, admin):
    c, uid = signed_in(env, admin), customer(env)
    for _ in range(5):
        login(env, TestClient(app), "buyer@example.com", "wrong password here")
    assert any(u["locked"] for u in c.get("/admin/users?q=buyer").json())
    assert c.post(f"/admin/users/{uid}/unlock", headers=CSRF).status_code == 200
    assert login(env, TestClient(app), "buyer@example.com").status_code == 200


def test_customer_sees_only_their_own_credits(env, admin):
    uid = customer(env)
    with env.scope() as db:
        make_user(db, "other@example.com", balance_micro=99_000_000)
    c = signed_in(env, admin)
    c.post(f"/admin/users/{uid}/credits", json={"amount_usd": "3", "kind": "grant", "reason": "welcome"}, headers=CSRF)
    cust = TestClient(app)
    login(env, cust, "buyer@example.com")
    body = cust.get("/account/credits").json()
    assert body["balance_usd"] == 3.0 and [e["reason"] for e in body["entries"]] == ["welcome"]
    assert TestClient(app).get("/account/credits").status_code == 401


def test_overview_reports_customer_liability_excluding_admins(env, admin):
    customer(env, balance=7_500_000)
    c = signed_in(env, admin)
    o = c.get("/admin/overview").json()
    assert o["customers"] == {"count": 1, "credit_liability_usd": 7.5}


# =============== dashboard files ===============
def test_dashboard_page_redirects_to_sign_in_and_serves_admins_with_strict_csp(env, admin):
    anon = env.client.get("/admin", follow_redirects=False)
    assert anon.status_code == 302 and anon.headers["location"] == "/login?next=/admin"
    c = signed_in(env, admin)
    r = c.get("/admin")
    assert r.status_code == 200 and "Clientfinder admin" in r.text and "Create your admin account" not in r.text
    csp = r.headers["content-security-policy"]
    assert "script-src 'self'" in csp and "unsafe-inline" not in csp and "unsafe-eval" not in csp and "frame-ancestors 'none'" in csp
    assert r.headers["cache-control"] == "no-store"
    assert env.client.get("/admin/assets/dashboard.js").headers["content-type"].startswith("text/javascript")
    assert env.client.get("/admin/assets/dashboard.css").status_code == 200


@pytest.mark.parametrize("name", ["../main.py", "..%2fmain.py", "index.html", "dashboard.js.map", "%2e%2e/config.py", ".env"])
def test_assets_endpoint_only_serves_the_allow_list(env, name):
    assert env.client.get(f"/admin/assets/{name}").status_code in (404, 405)


def test_dashboard_has_no_inline_script_or_style_and_never_uses_innerhtml():
    html = (STATIC / "index.html").read_text()
    assert not re.search(r"<script(?![^>]*\bsrc=)", html), "inline <script>"
    assert not re.search(r"\sstyle=|\son[a-z]+=", html), "inline style/handler"
    assert "http://" not in html and "https://cdn" not in html
    code = "\n".join(l for l in (STATIC / "dashboard.js").read_text().splitlines() if not l.lstrip().startswith("//"))
    for banned in ("innerHTML", "outerHTML", "insertAdjacentHTML", "document.write", "eval(", "new Function", "setAttribute(\"style\""):
        assert banned not in code, banned


def test_dashboard_script_only_calls_our_own_endpoints():
    code = (STATIC / "dashboard.js").read_text()
    urls = set(re.findall(r"""["'`](/[a-z][^"'`]*)["'`]""", code))
    assert all(u in ("/admin", "/login") or u.startswith(("/auth/", "/admin/", "/login")) for u in urls if "/" in u[1:] or u in ("/admin", "/login")), urls
    assert "http://" not in code and "https://" not in code


# =============== public job feed: rate limit + optional login ===============
def test_public_feed_is_rate_limited_per_ip(env, monkeypatch):
    from app import security
    monkeypatch.setattr(security, "_public_limiter", security.RateLimiter())
    monkeypatch.setattr(settings, "jobs_require_login", False)       # isolate the rate limiter
    monkeypatch.setattr(settings, "public_rate_limit_per_min", 5)
    codes = [env.client.get("/jobs").status_code for _ in range(8)]
    assert codes == [200] * 5 + [429] * 3
    assert env.client.get("/jobs/tags").status_code == 429                      # the limit covers every /jobs route
    assert env.client.get("/health").status_code == 200                          # health is not rate limited


def test_rate_limiter_window_slides_and_keys_are_independent():
    from app.security import RateLimiter
    rl = RateLimiter()
    assert [rl.allow("a", 2, 60, now=t) for t in (0, 1, 2)] == [True, True, False]
    assert rl.allow("b", 2, 60, now=2) is True
    assert rl.allow("a", 2, 60, now=61) is True                                  # old hits aged out


def test_jobs_can_require_login(env, monkeypatch):
    with env.scope() as db:
        make_user(db, "member@example.com")
    monkeypatch.setattr(settings, "jobs_require_login", True)
    assert env.client.get("/jobs").status_code == 401 and env.client.get("/jobs/stats").status_code == 401
    assert login(env, env.client, "member@example.com").status_code == 200
    assert env.client.get("/jobs").status_code == 200
    monkeypatch.setattr(settings, "jobs_require_login", False)
    assert TestClient(app).get("/jobs").status_code == 200


def test_rate_limit_off_when_zero(env, monkeypatch):
    from app import security
    monkeypatch.setattr(security, "_public_limiter", security.RateLimiter())
    monkeypatch.setattr(settings, "jobs_require_login", False)
    monkeypatch.setattr(settings, "public_rate_limit_per_min", 0)
    assert all(env.client.get("/jobs").status_code == 200 for _ in range(30))


# =============== 2FA secret encryption keys ===============
def test_2fa_survives_jwt_secret_rotation_when_a_data_key_is_set(env, monkeypatch):
    monkeypatch.setattr(settings, "data_encryption_key", "d" * 40)
    with env.scope() as db:
        _, secret = make_user(db, EMAIL, admin=True)
    monkeypatch.setattr(settings, "jwt_secret", "z" * 48)               # rotate the session secret
    assert login(env, env.client, EMAIL, secret=secret).status_code == 200


def test_2fa_breaks_on_jwt_rotation_without_a_data_key_which_is_why_it_must_be_set(env, monkeypatch):
    monkeypatch.setattr(settings, "data_encryption_key", "")
    with env.scope() as db:
        _, secret = make_user(db, EMAIL, admin=True)
    monkeypatch.setattr(settings, "jwt_secret", "z" * 48)
    assert login(env, env.client, EMAIL, secret=secret).status_code == 401


def test_data_key_rotation_keeps_old_secrets_readable_and_encrypts_with_the_new_key(env, monkeypatch):
    monkeypatch.setattr(settings, "data_encryption_key", "o" * 40)
    old_blob = sec.encrypt_secret("JBSWY3DPEHPK3PXP")
    monkeypatch.setattr(settings, "data_encryption_key", "n" * 40)
    monkeypatch.setattr(settings, "data_encryption_key_previous", "o" * 40)
    assert sec.decrypt_secret(old_blob) == "JBSWY3DPEHPK3PXP"
    new_blob = sec.encrypt_secret("NEWSECRET")
    monkeypatch.setattr(settings, "data_encryption_key_previous", "")
    assert sec.decrypt_secret(new_blob) == "NEWSECRET" and sec.decrypt_secret(old_blob) is None


def test_short_data_keys_are_ignored(env, monkeypatch):
    monkeypatch.setattr(settings, "data_encryption_key", "short")
    assert sec.decrypt_secret(sec.encrypt_secret("X")) == "X"           # falls back to the JWT-derived key, no crash


def test_job_feed_requires_login_by_default(env):
    assert type(settings).model_fields["jobs_require_login"].default is True
    anon = TestClient(app)
    for path in ("/jobs", "/jobs/stats", "/jobs/tags"):
        assert anon.get(path).status_code == 401
    signup(env.client)
    assert env.client.get("/jobs").status_code == 200
