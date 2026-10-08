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
from app.models import AuthEvent, CreditEntry, SetupCode, User

EMAIL = "christian@emailsandsms.com"
STATIC = Path(__file__).resolve().parent.parent / "app" / "static" / "admin"


@pytest.fixture
def env(monkeypatch):
    e = authkit.make_env(monkeypatch)
    yield e
    app.dependency_overrides.clear()


def issue_code(env, email=EMAIL, minutes=30):
    code = sec.new_setup_code()
    with env.scope() as db:
        db.add(SetupCode(email=email, code_hash=sec.hash_setup_code(code), expires_at=sec.utcnow() + timedelta(minutes=minutes)))
    return code


def start(env, code, email=EMAIL, password=PW):
    return env.client.post("/auth/setup/start", json={"email": email, "setup_code": code, "password": password}, headers=CSRF)


def confirm(env, code, totp, email=EMAIL):
    return env.client.post("/auth/setup/confirm", json={"email": email, "setup_code": code, "totp_code": totp}, headers=CSRF)


# =============== first-run account setup ("Create your admin account") ===============
def test_status_before_and_after_setup(env):
    s = env.client.get("/auth/setup/status").json()
    assert s == {"auth_configured": True, "problem": None, "needs_setup": True}
    code = issue_code(env)
    secret = start(env, code).json()["totp_secret"]
    assert confirm(env, code, totp_now(secret)).status_code == 200
    assert env.client.get("/auth/setup/status").json()["needs_setup"] is False


def test_full_setup_flow_creates_a_working_admin(env):
    code = issue_code(env)
    r = start(env, code)
    assert r.status_code == 200
    body = r.json()
    assert body["otpauth_uri"].startswith("otpauth://totp/") and "Clientfinder.ai" in body["otpauth_uri"]
    assert body["qr"].startswith("data:image/svg+xml") and len(body["totp_secret"]) >= 16
    with env.scope() as db:                                    # not usable until 2FA is confirmed
        u = db.scalar(select(User).where(User.email == EMAIL))
        assert (u.is_active, u.totp_enabled, u.is_admin) == (False, False, True)
        assert u.totp_secret_enc and body["totp_secret"] not in u.totp_secret_enc          # encrypted at rest
        assert PW not in u.password_hash and u.password_hash.startswith("$argon2id$")
    assert login(env, env.client, EMAIL).status_code == 401                               # cannot sign in yet
    c = confirm(env, code, totp_now(body["totp_secret"]))
    assert c.status_code == 200 and c.json()["is_admin"] is True
    assert "cf_session" in c.cookies
    me = env.client.get("/auth/me")
    assert me.status_code == 200 and me.json()["email"] == EMAIL
    assert env.client.get("/admin/overview").status_code == 200


def test_setup_code_works_once(env):
    code = issue_code(env)
    secret = start(env, code).json()["totp_secret"]
    assert confirm(env, code, totp_now(secret)).status_code == 200
    assert start(env, code).status_code == 403                  # consumed


def test_setup_rejects_wrong_code_and_burns_it_after_five_tries(env):
    code = issue_code(env)
    for _ in range(5):
        assert start(env, "AAAAA-BBBBB-CCCCC-DDDDD-EEEEE").status_code == 403
    assert start(env, code).status_code == 403                  # the real code is now dead too: a new one is needed
    with env.scope() as db:
        assert db.scalar(select(SetupCode)).used_at is not None
        assert db.query(AuthEvent).filter(AuthEvent.event == "setup_fail").count() >= 5


def test_setup_rejects_expired_code_and_unlisted_emails(env):
    assert start(env, issue_code(env, minutes=-1)).status_code == 403
    code = issue_code(env, email="attacker@example.com")
    r = start(env, code, email="attacker@example.com")
    assert r.status_code == 403 and r.json()["detail"] == "Invalid setup code"       # same message: no hints
    with env.scope() as db:
        assert db.scalar(select(User).where(User.email == "attacker@example.com")) is None


def test_setup_without_any_issued_code_is_refused(env):
    assert start(env, "AAAAA-BBBBB-CCCCC-DDDDD-EEEEE").status_code == 403


@pytest.mark.parametrize("pw", ["short", "a" * 20, "password" * 3, "christianpassword1"])
def test_setup_enforces_password_policy(env, pw):
    r = start(env, issue_code(env), password=pw)
    assert r.status_code == 422, pw


def test_confirm_with_wrong_2fa_code_is_refused_and_counts_against_the_code(env):
    code = issue_code(env)
    secret = start(env, code).json()["totp_secret"]
    wrong = "000000" if totp_now(secret) != "000000" else "111111"
    assert confirm(env, code, wrong).status_code == 403
    assert env.client.get("/auth/me").status_code == 401
    assert confirm(env, code, totp_now(secret)).status_code == 200      # still recoverable with the right code


def test_confirm_requires_start_first(env):
    code = issue_code(env)
    assert confirm(env, code, "123456").status_code == 409


def test_setup_not_allowed_when_account_already_active(env):
    code = issue_code(env)
    secret = start(env, code).json()["totp_secret"]
    confirm(env, code, totp_now(secret))
    again = issue_code(env)
    assert start(env, again).status_code == 409                          # must use reset-admin from the server shell


def test_setup_disabled_when_server_secret_is_weak(env, monkeypatch):
    monkeypatch.setattr(settings, "jwt_secret", "change-me-dev-only")
    s = env.client.get("/auth/setup/status").json()
    assert s["auth_configured"] is False and s["needs_setup"] is False and "JWT_SECRET" in s["problem"]
    assert start(env, "x").status_code == 503
    assert env.client.post("/auth/login", json={"email": EMAIL, "password": PW}, headers=CSRF).status_code == 503


# =============== login ===============
@pytest.fixture
def admin(env):
    with env.scope() as db:
        uid, secret = make_user(db, EMAIL, admin=True)
    return SimpleNs(id=uid, secret=secret)


class SimpleNs:
    def __init__(self, **kw):
        self.__dict__.update(kw)


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


def test_admin_without_2fa_is_refused(env):
    with env.scope() as db:
        make_user(db, EMAIL, admin=True, totp=False)
    r = login(env, env.client, EMAIL)
    assert r.status_code == 403 and "2FA" in r.json()["detail"]


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
def test_dashboard_page_and_assets_are_served_with_strict_csp(env):
    r = env.client.get("/admin")
    assert r.status_code == 200 and "Create your admin account" in r.text
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
    assert all(u.startswith(("/auth/", "/admin/")) for u in urls if "/" in u[1:] or u in ("/admin",)), urls
    assert "http://" not in code and "https://" not in code


# =============== public job feed: rate limit + optional login ===============
def test_public_feed_is_rate_limited_per_ip(env, monkeypatch):
    from app import security
    monkeypatch.setattr(security, "_public_limiter", security.RateLimiter())
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
