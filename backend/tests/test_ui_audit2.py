"""Customer UI / copy accuracy found by the second audit (after the Cursor merge). Each test failed on the merged code."""
import re
from datetime import timedelta
from pathlib import Path

from fastapi.testclient import TestClient

import authkit
from authkit import CSRF
from app import live_search, platforms
from app.api import public as public_api
from app.main import app
from app.models import ScrapeRun, Source
from app.scrapers import google_search
from app.scrapers.google_search import SearchError
from app.security import utcnow
from test_customer_api import add_job, admin_client, balance, env, live, member, run   # noqa: F401  (fixtures)

BACKEND = Path(__file__).resolve().parent.parent
SITE = BACKEND / "app" / "static" / "site"
APP = (SITE / "app.js").read_text()
COMMON = (SITE / "common.js").read_text()
HOME = (SITE / "home.html").read_text()


def test_newest_first_sorts_by_the_date_the_card_shows(env):
    now = utcnow()
    with env.scope() as db:
        add_job(db, title="posted 40 days ago, scraped today", posted_at=now - timedelta(days=40), first_seen_at=now - timedelta(hours=1))
        add_job(db, title="posted yesterday", posted_at=now - timedelta(days=1), first_seen_at=now - timedelta(hours=5))
        add_job(db, title="no posted date, seen 2 days ago", posted_at=None, first_seen_at=now - timedelta(days=2))
    c = member(env)
    assert [j["title"] for j in c.get("/jobs?sort=newest").json()] == ["posted yesterday", "no posted date, seen 2 days ago", "posted 40 days ago, scraped today"]
    assert [j["title"] for j in c.get("/jobs?sort=oldest").json()][0] == "posted 40 days ago, scraped today"


def test_public_live_flag_is_not_kept_alive_by_customer_searches(env):
    with env.scope() as db:
        s = Source(key=live_search.SOURCE_KEY, kind="google_search", display_name="Customer searches", enabled=True, config={})
        db.add(s)
        db.flush()
        db.add(ScrapeRun(source_id=s.id, status="ok", started_at=utcnow(), finished_at=utcnow()))
    public_api._cache.clear()
    d = TestClient(app).get("/public/stats").json()
    assert d["live"] is False and d["last_update"] is None          # no feed has run: the home page must not say "Live: updated just now"


def test_admin_does_not_raise_alarms_or_offer_run_now_for_customer_searches(env):
    old = utcnow() - timedelta(days=3)
    with env.scope() as db:
        s = Source(key=live_search.SOURCE_KEY, kind="google_search", display_name="Customer searches", enabled=True, config={})
        db.add(s)
        db.flush()
        db.add(ScrapeRun(source_id=s.id, status="failed", started_at=old, finished_at=old))
    a = admin_client(env)
    o = a.get("/admin/overview").json()["sources"]
    assert o["stale"] == [] and o["failing"] == []
    row = next(x for x in a.get("/admin/sources").json() if x["key"] == live_search.SOURCE_KEY)
    assert row["runnable"] is False                                  # "Run now" on it was a 404
    assert a.post(f"/admin/sources/{live_search.SOURCE_KEY}/run", headers=CSRF).status_code == 404


def test_a_search_where_some_sites_could_not_be_asked_says_so_and_is_not_cached(env, live, monkeypatch):
    calls = {"n": 0}
    real = google_search.search

    def flaky(query, num=10, freshness=None, **kw):
        calls["n"] += 1
        if calls["n"] > 3:
            raise SearchError("serpapi is out of searches", quota=True)
        return real(query, num=num, freshness=freshness, **kw)
    monkeypatch.setattr(google_search, "search", flaky)
    c = member(env, balance_usd=5)
    sid = run(c).json()["id"]
    s = c.get(f"/searches/{sid}").json()
    assert s["status"] == "done" and "Only 3 of 20" in (s["error"] or "")
    q = c.post("/searches/estimate", json={"query": "hiring copywriter", "freshness": "w"}, headers=CSRF).json()
    assert q["cached"] is False                                      # not served to everyone for 6 hours as a finished search


def test_dom_append_is_never_given_null_or_an_array():
    # Element.append(null) writes the text "null" and Element.append([nodes]) writes "[object HTMLDivElement]". Use CF.put (or h) instead.
    assert "CF.put" in COMMON
    for m in re.finditer(r"[\w)\]]\.append\(", APP):
        stmt = APP[m.start():m.start() + 1200].split(";\n")[0]
        if stmt.startswith("p.append") or stmt.lstrip(".").startswith("p.append"):
            continue
        assert not re.search(r"\? [^\n]*: null[,)]", stmt.split("\n")[0]) and "r.data.map(" not in stmt, stmt[:120]


def test_every_platform_the_backend_can_emit_has_a_label():
    labelled = set(re.findall(r"(\w+):\s*\"", COMMON[COMMON.index("CF.PLATFORM"):COMMON.index("CF.TYPE")]))
    emitted = {p for _, p in google_search._PLATFORM_HOSTS} | {"linkedin_jobs", "web"} | set(platforms.BOARDS) | set(platforms.SOCIAL)
    assert emitted - labelled == set()


def test_no_merge_leftover_files_ship_in_the_app():
    assert list((BACKEND / "app").rglob("*.orig")) == [] and list((BACKEND / "app").rglob("*.rej")) == []


def test_home_copy_does_not_understate_the_markup_or_overclaim_coverage():
    assert "small margin" not in HOME and "Every hiring post" not in HOME
    assert "plus 50%" in HOME


def test_costs_doc_matches_the_real_size_and_price_of_a_search():
    doc = (BACKEND.parent / "docs" / "COSTS.md")
    if not doc.exists():
        return
    text = doc.read_text()
    n = len(live_search.search_targets("hiring copywriter", None))
    assert f"{n} Google searches" in text and f"about ${live_search.estimated_user_price_usd():.2f}" in text
    assert "22 Google searches" not in text
