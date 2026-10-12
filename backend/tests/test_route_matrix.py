"""Table-driven sweep of every route: anonymous / customer / admin, with and without CSRF, malformed bodies."""
import json
import pytest
from fastapi.testclient import TestClient

import authkit
from authkit import CSRF
from app.main import app
from app.scrapers import pipeline, query_plan
from app.scrapers.registry import DURABLE
from test_customer_api import env, member, add_job  # noqa

ADMIN = "christian@emailsandsms.com"

ROUTES = [  # method, path, body
    ("GET", "/jobs", None), ("GET", "/jobs/facets", None), ("GET", "/jobs/tags", None), ("GET", "/jobs/stats", None),
    ("GET", "/public/stats", None), ("GET", "/public/preview", None), ("GET", "/public/config", None),
    ("GET", "/searches/config", None), ("POST", "/searches/estimate", {"query": "x"}), ("GET", "/searches", None),
    ("POST", "/searches", {"query": "x"}), ("GET", "/searches/1", None), ("GET", "/searches/1/jobs", None),
    ("GET", "/auth/status", None), ("GET", "/auth/me", None), 
    ("POST", "/auth/2fa/start", {"setup_token": "x"}), ("POST", "/auth/2fa/confirm", {"setup_token": "x", "totp_code": "1"}),
    ("GET", "/account/credits", None), ("PUT", "/account/prefs", {}), ("GET", "/account/profile", None), ("PUT", "/account/profile", {}),
    ("POST", "/account/checkout", {"usd": 27}),
    ("GET", "/admin/sources", None), ("POST", "/admin/sources/nope/run", {}), ("POST", "/admin/regions/rebuild", {}),
    ("GET", "/admin/scrape-runs", None), ("GET", "/admin/failed-urls", None), ("POST", "/admin/failed-urls/requeue", {}),
    ("POST", "/admin/retag", {}), ("GET", "/admin/backups", None), ("GET", "/admin/overview", None), ("GET", "/admin/users", None),
    ("GET", "/admin/users/1/ledger", None), ("POST", "/admin/users/1/credits", {"amount_usd": 1, "kind": "grant", "reason": "test"}),
    ("POST", "/admin/users/1/limits", {}), ("POST", "/admin/users/1/active", {"active": True}), ("POST", "/admin/users/1/unlock", {}),
    ("POST", "/admin/users/1/logout-everywhere", {}), ("GET", "/admin/security/events", None), ("GET", "/admin/payments", None),
    ("POST", "/admin/payments/1/credit", {"user_id": 1}), ("POST", "/assist", {"mode": "x"}), ("GET", "/assist/config", None),
]


def call(c, method, path, body, csrf=True, raw=None):
    kw = {"headers": dict(CSRF) if csrf else {}}
    if raw is not None:
        kw["content"] = raw; kw["headers"]["content-type"] = "application/json"
    elif body is not None:
        kw["json"] = body
    return c.request(method, path, **kw)


@pytest.fixture
def people(env, monkeypatch):
    monkeypatch.setattr(pipeline, "requeue_failed", lambda **k: None)
    monkeypatch.setattr(query_plan, "run_query_now", lambda *a, **k: None)
    with env.scope() as db:
        authkit.make_user(db, "other@example.com")
    cust = member(env)
    with env.scope() as db:
        uid, secret = authkit.make_user(db, ADMIN, admin=True)
    adm = TestClient(app)
    assert authkit.login(env, adm, ADMIN, secret=secret).status_code == 200
    return env, TestClient(app), cust, adm


@pytest.mark.parametrize("method,path,body", ROUTES)
def test_matrix(people, method, path, body):
    env, anon, cust, adm = people
    is_admin_route = path.startswith("/admin")
    is_write = method != "GET"
    for who, c in (("anon", anon), ("cust", cust), ("admin", adm)):
        for csrf in (True, False):
            r = call(c, method, path, body, csrf)
            assert r.status_code < 500 or (path == "/account/checkout" and r.status_code == 503), (who, csrf, r.status_code, r.text[:200])
            if r.headers.get("content-type", "").startswith("application/json") and r.status_code >= 400:
                assert "detail" in r.json(), (who, csrf, r.status_code, r.text[:200])
            if who == "anon" and path not in ("/auth/status", "/public/stats", "/public/preview", "/public/config", "/auth/2fa/start", "/auth/2fa/confirm"):
                assert r.status_code in (401, 403), (path, csrf, r.status_code)
            if who == "cust" and is_admin_route:
                assert r.status_code == 403, (path, csrf, r.status_code)
            if who == "admin" and is_write and not csrf and path.startswith("/admin"):
                assert r.status_code == 403
        # malformed body
        if is_write:
            for raw in ("{", "[]", '"x"', "null", "x" * 10):
                r = call(c, method, path, None, True, raw=raw)
                assert r.status_code < 500, (who, raw, r.status_code, r.text[:200])
