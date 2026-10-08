"""Whop webhook: signature, idempotency, matching, and every way a payment can be held instead of guessed at."""
import base64
import hashlib
import hmac
import json
import time

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import func, select

import authkit
from app import whop
from app.config import settings
from app.main import app
from app.models import CreditEntry, PaymentEvent, User

SECRET = "ws_" + base64.b64encode(b"super-secret-key-bytes-1234567890").decode()


@pytest.fixture
def env(monkeypatch):
    e = authkit.make_env(monkeypatch)
    monkeypatch.setattr(settings, "whop_webhook_secret", SECRET)
    monkeypatch.setattr(settings, "whop_amount_unit", "dollars")
    yield e
    app.dependency_overrides.clear()


def sign(body: bytes, msg_id="evt_1", ts=None, key=None):
    ts = str(ts or int(time.time()))
    key = key if key is not None else base64.b64decode(SECRET.split("_", 1)[1])
    sig = base64.b64encode(hmac.new(key, f"{msg_id}.{ts}.".encode() + body, hashlib.sha256).digest()).decode()
    return {"webhook-id": msg_id, "webhook-timestamp": ts, "webhook-signature": f"v1,{sig}", "content-type": "application/json"}


def post(c, payload, msg_id="evt_1", **kw):
    body = json.dumps(payload).encode()
    return c.post("/webhooks/whop", content=body, headers=sign(body, msg_id, **kw))


def pay(email="buyer@example.com", total=20, **extra):
    return {"type": "payment.succeeded", "data": {"user": {"email": email}, "currency": "usd", "total": total, **extra}}


def user(env, email="buyer@example.com"):
    with env.scope() as db:
        authkit.make_user(db, email)


def bal(env, email="buyer@example.com"):
    with env.scope() as db:
        return db.scalar(select(User.balance_micro).where(User.email == email))


def test_paid_event_credits_the_account_once_even_if_delivered_twice(env):
    user(env); c = TestClient(app)
    assert post(c, pay()).json() == {"ok": True, "status": "credited"}
    assert post(c, pay()).status_code == 200
    assert bal(env) == 20_000_000
    with env.scope() as db:
        assert db.scalar(select(func.count()).select_from(CreditEntry).where(CreditEntry.ref == "whop:evt_1")) == 1


def test_raw_string_secret_also_verifies():
    body = b"{}"; key = b"ws_plainsecret"
    assert whop.verify("ws_plainsecret", "i", "100", sign(body, "i", 100, key)["webhook-signature"], body, now=100)


@pytest.mark.parametrize("break_it", ["body", "key", "old", "future", "missing"])
def test_bad_signatures_are_rejected_and_credit_nothing(env, break_it):
    user(env); c = TestClient(app); body = json.dumps(pay()).encode()
    h = sign(body, ts={"old": int(time.time()) - 3600, "future": int(time.time()) + 3600}.get(break_it),
             key=b"wrong-key" if break_it == "key" else None)
    if break_it == "body":
        body += b" "
    if break_it == "missing":
        h.pop("webhook-signature")
    assert c.post("/webhooks/whop", content=body, headers=h).status_code == 401
    assert bal(env) == 0


def test_webhook_refuses_everything_when_no_secret_is_set(env, monkeypatch):
    monkeypatch.setattr(settings, "whop_webhook_secret", "")
    assert post(TestClient(app), pay()).status_code == 503


@pytest.mark.parametrize("payload,status", [
    (pay(email="nobody@example.com"), "unmatched"),
    ({"type": "payment.succeeded", "data": {"currency": "usd", "total": 5}}, "unmatched"),
    (pay(currency="eur"), "review"),
    (pay(total=0), "review"),
    (pay(total=-5), "review"),
    (pay(total=100000), "review"),
    ({"type": "payment.succeeded", "data": {"user": {"email": "buyer@example.com"}, "currency": "usd"}}, "review"),
    ({"type": "membership.went_valid", "data": {"user": {"email": "buyer@example.com"}}}, "ignored"),
    ({"type": "payment.failed", "data": {"user": {"email": "buyer@example.com"}, "total": 9, "currency": "usd"}}, "ignored"),
    ({"nothing": 1}, "ignored"),
])
def test_unclear_payments_are_held_not_guessed_and_never_credit(env, payload, status):
    user(env); c = TestClient(app)
    assert post(c, payload).json()["status"] == status
    assert bal(env) == 0


def test_underscore_event_names_and_cents_unit(env, monkeypatch):
    user(env); c = TestClient(app)
    monkeypatch.setattr(settings, "whop_amount_unit", "cents")
    assert post(c, {"type": "payment_succeeded", "data": {"user": {"email": "Buyer@Example.com"}, "currency": "usd", "total": 2500}}).json()["status"] == "credited"
    assert bal(env) == 25_000_000


def test_unmatched_payment_can_be_resolved_by_the_owner_once(env):
    c = TestClient(app); post(c, pay(email="later@example.com", total=12), msg_id="evt_9")
    user(env, "later@example.com")
    with env.scope() as db:
        _, secret = authkit.make_user(db, "christian@emailsandsms.com", admin=True)
        uid = db.scalar(select(User.id).where(User.email == "later@example.com"))
        eid = db.scalar(select(PaymentEvent.id))
    a = TestClient(app)
    assert authkit.login(env, a, "christian@emailsandsms.com", secret=secret).status_code == 200
    rows = a.get("/admin/payments?status=unmatched").json()
    assert len(rows) == 1 and rows[0]["amount_usd"] == 12
    assert a.post(f"/admin/payments/{eid}/credit", json={"user_id": uid}, headers=authkit.CSRF).status_code == 200
    assert bal(env, "later@example.com") == 12_000_000
    assert a.post(f"/admin/payments/{eid}/credit", json={"user_id": uid}, headers=authkit.CSRF).status_code == 409
    assert bal(env, "later@example.com") == 12_000_000


def test_webhook_needs_no_csrf_header_or_cookie_and_is_not_blocked_by_origin(env):
    user(env)
    body = json.dumps(pay()).encode()
    r = TestClient(app).post("/webhooks/whop", content=body, headers=sign(body))
    assert r.status_code == 200


# =============== owner can opt out of admin 2FA ===============
def test_admin_signs_in_with_password_only_when_2fa_is_switched_off(env, monkeypatch):
    monkeypatch.setattr(settings, "admin_require_2fa", False)
    with env.scope() as db:
        authkit.make_user(db, "christian@emailsandsms.com", admin=True, totp=False)       # never enrolled
    a = TestClient(app)
    r = a.post("/auth/login", json={"email": "christian@emailsandsms.com", "password": authkit.PW}, headers=authkit.CSRF)
    assert r.status_code == 200 and r.json()["is_admin"]
    assert a.get("/admin/overview").status_code == 200


def test_2fa_stays_required_by_default_and_for_non_allowlisted_admins(env, monkeypatch):
    with env.scope() as db:
        authkit.make_user(db, "christian@emailsandsms.com", admin=True, totp=False)
    r = TestClient(app).post("/auth/login", json={"email": "christian@emailsandsms.com", "password": authkit.PW}, headers=authkit.CSRF)
    assert r.status_code == 401 and r.json()["detail"] == "totp_setup_required"
    monkeypatch.setattr(settings, "admin_require_2fa", False)
    with env.scope() as db:
        authkit.make_user(db, "other-admin@example.com", admin=True, totp=False)
    r = TestClient(app).post("/auth/login", json={"email": "other-admin@example.com", "password": authkit.PW}, headers=authkit.CSRF)
    assert r.status_code == 403                                  # not in ADMIN_EMAILS: still no admin access


# =============== checkout links created through Whop's API ===============
from app import whop_api  # noqa: E402


class FakeResp:
    def __init__(self, status=200, body=None, text=""):
        self.status_code, self._body, self.text = status, body, text or json.dumps(body or {})
    def json(self):
        if self._body is None:
            raise ValueError("no json")
        return self._body


@pytest.fixture
def whop_on(env, monkeypatch):
    monkeypatch.setattr(settings, "whop_api_key", "whop_SECRET_KEY_123456789")
    monkeypatch.setattr(settings, "whop_company_id", "biz_test")
    sent = []
    def fake_post(payload):
        sent.append(payload)
        return FakeResp(200, {"id": "ch_1", "purchase_url": "https://whop.com/checkout/ch_1"})
    monkeypatch.setattr(whop_api, "_post", fake_post)
    whop_api._last = sent
    return sent


def buyer(env, email="buyer@example.com"):
    with env.scope() as db:
        uid, _ = authkit.make_user(db, email)
    c = TestClient(app)
    assert authkit.login(env, c, email).status_code == 200
    c.uid = uid
    return c


def test_checkout_creates_a_link_for_exactly_the_chosen_bundle_and_tags_the_buyer(env, whop_on):
    c = buyer(env)
    r = c.post("/account/checkout", json={"usd": 47}, headers=authkit.CSRF)
    assert r.status_code == 200 and r.json() == {"url": "https://whop.com/checkout/ch_1"}
    p = whop_on[0]
    assert p["plan"]["initial_price"] == 47 and p["plan"]["plan_type"] == "one_time" and p["plan"]["currency"] == "usd" and p["plan"]["company_id"] == "biz_test"
    assert p["metadata"]["cf_user_id"] == str(c.uid) and p["redirect_url"].endswith("/app")
    assert "SECRET_KEY" not in json.dumps(p)


def test_checkout_only_sells_the_listed_bundles_and_needs_login_and_csrf(env, whop_on):
    c = buyer(env)
    for bad in (1, 28, 0, -27, 10000):
        assert c.post("/account/checkout", json={"usd": bad}, headers=authkit.CSRF).status_code == 422
    assert c.post("/account/checkout", json={"usd": 27}).status_code == 403
    assert TestClient(app).post("/account/checkout", json={"usd": 27}, headers=authkit.CSRF).status_code == 401
    assert not whop_on


def test_checkout_failures_are_explained_and_never_leak_the_key(env, whop_on, monkeypatch):
    c = buyer(env)
    monkeypatch.setattr(whop_api, "_post", lambda p: FakeResp(401, text='{"error":"bad key whop_SECRET_KEY_123456789 lacks plan:create"}'))
    r = c.post("/account/checkout", json={"usd": 27}, headers=authkit.CSRF)
    assert r.status_code == 502 and "401" in r.json()["detail"] and "SECRET_KEY_123456789" not in r.json()["detail"]
    monkeypatch.setattr(whop_api, "_post", lambda p: FakeResp(200, {"id": "x"}))
    assert c.post("/account/checkout", json={"usd": 27}, headers=authkit.CSRF).status_code == 502
    monkeypatch.setattr(whop_api, "_post", lambda p: FakeResp(200, {"purchase_url": "http://not-https.example"}))
    assert c.post("/account/checkout", json={"usd": 27}, headers=authkit.CSRF).status_code == 502


def test_checkout_is_off_without_keys_and_rate_limited_per_account(env, monkeypatch):
    c = buyer(env)
    assert c.post("/account/checkout", json={"usd": 27}, headers=authkit.CSRF).status_code == 503
    monkeypatch.setattr(settings, "whop_api_key", "k"); monkeypatch.setattr(settings, "whop_company_id", "biz_x")
    monkeypatch.setattr(whop_api, "_post", lambda p: FakeResp(200, {"purchase_url": "https://whop.com/c/1"}))
    from app.api import account as acct
    acct._checkout_hits.clear()
    codes = [c.post("/account/checkout", json={"usd": 27}, headers=authkit.CSRF).status_code for _ in range(14)]
    assert codes[:12] == [200] * 12 and codes[12:] == [429, 429]


def test_credits_page_lists_bundles_without_urls_when_the_api_is_on(env, whop_on):
    b = buyer(env).get("/account/credits").json()["bundles"]
    assert [x["usd"] for x in b] == [27, 47, 97] and all(x["url"] is None for x in b)


def test_payment_is_matched_by_the_account_id_we_put_in_the_checkout_even_if_the_email_differs(env):
    user(env, "signup@example.com"); c = TestClient(app)
    with env.scope() as db:
        uid = db.scalar(select(User.id).where(User.email == "signup@example.com"))
    payload = {"type": "payment.succeeded", "data": {"user": {"email": "different-paypal-email@example.com"}, "currency": "usd", "total": 47,
                                                   "metadata": {"cf_user_id": str(uid), "cf_bundle_usd": "47"}}}
    assert post(c, payload, msg_id="evt_meta").json()["status"] == "credited"
    assert bal(env, "signup@example.com") == 47_000_000


def test_a_forged_or_unknown_account_id_never_credits_anyone(env):
    user(env); c = TestClient(app)
    payload = {"type": "payment.succeeded", "data": {"currency": "usd", "total": 20, "metadata": {"cf_user_id": "99999"}}}
    assert post(c, payload).json()["status"] == "unmatched" and bal(env) == 0
