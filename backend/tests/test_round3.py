"""Fixes from the $100k-customer test: duplicates, honest 24-hour numbers, listing addresses, and the screen behaviours."""
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

import authkit
from authkit import CSRF, PW
from app.dedup import find_same_job
from app.main import app
from app.models import Job

SITE = Path(__file__).resolve().parent.parent / "app" / "static" / "site"


@pytest.fixture
def env(monkeypatch):
    e = authkit.make_env(monkeypatch)
    yield e
    app.dependency_overrides.clear()


def add(env, **kw):
    with env.scope() as db:
        n = db.query(Job).count()
        now = datetime.now(timezone.utc)
        kw = {"title": "Copywriter", "platform": "web", "source_key": "s", "source_url": f"https://example.com/{n}", "dedupe_hash": f"h{n}",
              "is_real_job": True, "first_seen_at": now, "last_seen_at": now, **kw}
        db.add(Job(**kw))


def test_the_same_posting_reached_by_two_addresses_is_one_job(env):
    add(env, title="Copywriter (K18 Hair)", company_or_poster="K18 Hair", location="Remote")
    with env.scope() as db:
        assert find_same_job(db, " copywriter (K18 hair) ", "k18 HAIR", "remote") is not None             # case and spaces do not matter
        assert find_same_job(db, "Copywriter (K18 Hair)", "K18 Hair", "Los Angeles") is None              # another place is another opening
        assert find_same_job(db, "Senior Copywriter", "K18 Hair", "Remote") is None
        assert find_same_job(db, "Copywriter (K18 Hair)", "", "Remote") is None                           # no company: not known to be the same
        assert find_same_job(db, "Copywriter (K18 Hair)", "Confidential", "Remote") is None


def test_an_old_posting_does_not_swallow_a_new_one(env):
    add(env, title="Email marketer", company_or_poster="Acme", first_seen_at=datetime.now(timezone.utc) - timedelta(days=200))
    with env.scope() as db:
        assert find_same_job(db, "Email marketer", "Acme", None) is None                                   # rehired a year later is a new job


def test_the_home_page_count_uses_the_same_24_hour_rule_as_the_app_filter(env):
    now = datetime.now(timezone.utc)
    add(env, title="Posted today", posted_at=now - timedelta(hours=3))
    add(env, title="Scraped today, posted last week", posted_at=now - timedelta(days=7))
    add(env, title="No date, seen today")
    from app.api import public
    public._cache.clear() if hasattr(public, "_cache") else None
    c = TestClient(app)
    home = c.get("/public/stats").json()["jobs_new_24h"]
    assert c.post("/auth/signup", json={"email": "s@example.com", "password": PW}, headers=CSRF).status_code == 201
    assert home == len(c.get("/jobs?posted_within_days=1").json()) == 2


def test_screen_behaviours_from_the_retest():
    js = (SITE / "app.js").read_text()
    assert 'sessionStorage.setItem("cf_setup_off"' in js and "!setupOff()" in js and "setSetupOff(true); resetFilters()" in js     # Clear all sticks
    assert 'e.key === "Escape" && !pop.hidden' in js                                                   # Escape closes the account menu
    assert "disabled: !cfg.ready || broke" in js and "A written draft costs a fraction of a cent" in js   # no clickable Write it at $0.00
    assert 'j.company_or_poster ? h("span", { class: "sep"' in js                                       # no stray dot without a company
    assert "Save and show matches" in js and "no second screen" in js
    assert "posted in the last 24 hours" in (SITE / "home.js").read_text()
