"""Verified swarm-3 findings that had no patch: NUL bytes, failed feeds on the dashboard, and small screen fixes."""
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select

import authkit
from authkit import CSRF, PW
from app.main import app
from app.models import ScrapeRun, Source
from app.scrapers import problogger, remoteok

SITE = Path(__file__).resolve().parent.parent / "app" / "static" / "site"


@pytest.fixture
def env(monkeypatch):
    e = authkit.make_env(monkeypatch)
    from app.scrapers import common
    monkeypatch.setattr(common, "session_scope", e.scope)
    monkeypatch.setattr(remoteok, "session_scope", e.scope)
    monkeypatch.setattr(problogger, "session_scope", e.scope)
    yield e
    app.dependency_overrides.clear()


def test_a_nul_byte_is_a_422_not_a_500(env):
    c = TestClient(app, raise_server_exceptions=False)
    assert c.post("/auth/signup", json={"email": "nul@example.com", "password": PW}, headers=CSRF).status_code == 201
    for qs in ("q=a%00b", "location=a%00", "tag=a%00", "any_tag=%00", "platform=%00", "type=%00", "region=%00", "kind=%00"):
        assert c.get("/jobs?" + qs).status_code == 422, qs
    assert c.get("/jobs?q=copy").status_code == 200
    assert c.post("/auth/signup", json={"email": "a\u0000@example.com", "password": PW}, headers=CSRF).status_code == 422
    assert TestClient(app, raise_server_exceptions=False).post("/auth/login", json={"email": "a\u0000@example.com", "password": PW}, headers=CSRF).status_code == 401


@pytest.mark.parametrize("mod,key", [(remoteok, "remoteok"), (problogger, "problogger")])
def test_a_feed_that_cannot_be_fetched_leaves_a_failed_run_for_the_dashboard(env, monkeypatch, mod, key):
    def boom(*a, **k):
        raise RuntimeError("blocked")
    monkeypatch.setattr(mod, "fetch_jobs" if key == "remoteok" else "fetch_listings", boom)
    stats = mod.run()
    assert "error" in stats
    with env.scope() as db:
        src = db.scalar(select(Source).where(Source.key == key))
        assert src is not None
        run = db.scalar(select(ScrapeRun).where(ScrapeRun.source_id == src.id))
        assert run is not None and run.status == "failed"


def test_small_screen_fixes():
    app_js, css = (SITE / "app.js").read_text(), (SITE / "site.css").read_text()
    assert 'type: "search", maxlength: "100"' in app_js and '$("count").textContent = ""' in app_js
    assert "money(quote.balance_usd, 3)" in app_js and "money(c.balance_usd, 3)" in app_js          # "$0.66 ... needs $0.66" cannot happen
    assert ".prow[role=link]:focus-visible{outline:2px solid var(--accent)" in css
