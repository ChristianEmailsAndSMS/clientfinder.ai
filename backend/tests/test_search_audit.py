"""Regressions from the live-search audit: provider choice, expired-but-finished searches, and the double-start race."""
import threading
from datetime import timedelta

import httpx
import pytest
from sqlalchemy import create_engine, func, select
from sqlalchemy.orm import sessionmaker

from app import live_search
from app.config import settings
from app.db import Base
from app.models import CreditEntry, SearchQuery, User, UserSearch
from app.scrapers import google_search, query_plan
from app.security import utcnow
from test_customer_api import balance, env, live, member, run  # noqa: F401  (fixtures)


@pytest.mark.parametrize("provider", ["serpapi", "serper"])
@pytest.mark.parametrize("f", live_search.FRESHNESS)
def test_every_freshness_window_reaches_both_providers(monkeypatch, provider, f):
    seen = {}

    class R:
        def raise_for_status(self): pass
        def json(self): return {"organic_results": [], "organic": []}

    monkeypatch.setattr(httpx, "get", lambda url, **kw: seen.update(kw["params"]) or R())
    monkeypatch.setattr(httpx, "post", lambda url, **kw: seen.update(kw["json"]) or R())
    monkeypatch.setattr(settings, "serpapi_api_key", "a"); monkeypatch.setattr(settings, "serper_api_key", "b")
    monkeypatch.setattr(settings, "google_search_provider", provider)
    monkeypatch.setattr(settings, "dev_fixtures", False)             # dev mode returns canned results and never calls a provider
    google_search.search("hiring x", freshness=f)
    assert seen["tbs"] == f"qdr:{f}"


def test_only_a_serper_key_means_serper_is_called_and_budgeted(env, monkeypatch):
    monkeypatch.setattr(settings, "serpapi_api_key", "")
    monkeypatch.setattr(settings, "serper_api_key", "serper-key")
    monkeypatch.setattr(settings, "google_search_provider", "serpapi")          # the default setting
    posts = []

    class R:
        def raise_for_status(self): pass
        def json(self): return {"organic": []}

    monkeypatch.setattr(httpx, "post", lambda url, **kw: posts.append(url) or R())
    assert google_search.active_provider() == "serper" and query_plan._provider() == "serper"
    c = member(env, balance_usd=5)
    run(c)
    assert posts and set(posts) == {"https://google.serper.dev/search"}           # every Google search really went to Serper (a search fans out to several)
    with env.scope() as db:
        assert db.scalar(select(SearchQuery.provider)) == "serper"


def test_a_search_that_never_reached_a_provider_is_not_charged(env, monkeypatch):
    monkeypatch.setattr(settings, "serpapi_api_key", "k")
    monkeypatch.setattr(google_search, "active_provider", lambda: None)           # keys vanished between the check and the call
    c = member(env, balance_usd=5)
    sid = run(c).json()["id"]
    s = c.get(f"/searches/{sid}").json()
    assert s["status"] == "failed" and balance(env, c.uid) == 5_000_000


def test_a_search_expired_while_running_keeps_the_not_charged_promise(env, live, monkeypatch):
    c = member(env, balance_usd=5)
    real = live_search.pipeline.run_pipeline_for_query
    told = {}

    def slow(*a, **k):                                                            # takes >10 min; the customer's poll expires it
        with env.scope() as db:
            db.query(UserSearch).update({UserSearch.created_at: utcnow() - timedelta(minutes=11)})
        told.update(c.get("/searches/1").json())
        return real(*a, **k)

    monkeypatch.setattr(live_search.pipeline, "run_pipeline_for_query", slow)
    run(c)
    assert told["status"] == "failed" and "not charged" in told["error"]
    final = c.get("/searches/1").json()
    assert final["status"] == "failed" and final["cost_usd"] == 0
    assert balance(env, c.uid) == 5_000_000
    with env.scope() as db:
        assert db.scalar(select(func.count(CreditEntry.id))) == 0
        assert db.scalar(select(func.count(SearchQuery.id))) >= 1                # but the provider calls still count against the budget


def test_two_simultaneous_starts_by_one_user_start_only_one(monkeypatch, tmp_path):
    eng = create_engine(f"sqlite:///{tmp_path}/race.db", connect_args={"check_same_thread": False, "timeout": 20})
    Base.metadata.create_all(eng)
    S = sessionmaker(bind=eng, autoflush=False)
    monkeypatch.setattr(settings, "serpapi_api_key", "k"); monkeypatch.setattr(settings, "anthropic_api_key", "k")
    monkeypatch.setattr(settings, "dev_fixtures", False)
    with S() as db:
        u = User(email="r@x.test", password_hash="x", balance_micro=5_000_000)
        db.add(u); db.commit(); uid = u.id
    barrier = threading.Barrier(2, timeout=10)                                    # both requests pass every check before either inserts
    monkeypatch.setattr(query_plan, "searches_used_this_month", lambda db, now=None: (barrier.wait(), 0)[1])
    out = []

    def go(q):
        with S() as db:
            try:
                live_search.start_search(db, db.get(User, uid), q, "w", None); db.commit(); out.append("started")
            except live_search.SearchRefused as e:
                db.rollback(); out.append(e.status)

    ts = [threading.Thread(target=go, args=(q,)) for q in ("hiring aaa", "hiring bbb")]
    [t.start() for t in ts]; [t.join() for t in ts]
    assert sorted(map(str, out)) == ["409", "started"]
