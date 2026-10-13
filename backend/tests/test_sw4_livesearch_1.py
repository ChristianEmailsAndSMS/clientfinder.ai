"""The monthly search budget counts searches on every provider, including the fallback one."""
from sqlalchemy import select

from app import live_search
from app.config import settings
from app.models import UserSearch
from app.scrapers import query_plan
from test_customer_api import env, live, member  # noqa: F401  (fixtures)
from test_fanout_audit import _both_providers


def test_fallback_provider_searches_count_against_the_monthly_budget(env, monkeypatch):
    n = _both_providers(monkeypatch, serpapi_ok=0, serper_ok=10**6)
    monkeypatch.setattr(settings, "serpapi_monthly_budget", 25)
    a = member(env, "a@example.com", 50)
    refused = 0
    for i in range(3):
        try:
            with env.scope() as db:
                s = live_search.start_search(db, db.get(live_search.User, a.uid), f"hiring copywriter {i}", "w", None)
                sid = s.id
        except live_search.SearchRefused:
            refused += 1
            continue
        live_search.run_search(sid)
    assert refused >= 1                                                                  # the budget stops new searches
    assert n["serper"] <= 25, n
    with env.scope() as db:
        assert query_plan.searches_used_this_month(db) == n["serper"] > 0
        assert query_plan.searches_used_this_month(db) <= 25
