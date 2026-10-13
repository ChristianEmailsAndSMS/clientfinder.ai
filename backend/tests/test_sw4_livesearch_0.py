"""A model outage during a live search must not be billed, reported as a complete empty answer, or cached for everyone."""
from sqlalchemy import select

from app.config import settings
from app.models import User
from app.scrapers import llm_extractor
from tests.test_customer_api import env, live, member, run, balance, CSRF  # noqa: F401  (fixtures)


def test_model_outage_fails_the_search_without_charge_or_cache(env, live, monkeypatch):
    def boom(prompt):
        raise RuntimeError("529 overloaded")
    monkeypatch.setattr(settings, "anthropic_api_key", "k")
    monkeypatch.setattr(llm_extractor, "_call_model", boom)
    monkeypatch.setattr(llm_extractor, "extract", llm_extractor._claude_extract)
    a = member(env, balance_usd=5)
    start = balance(env, a.uid)
    sid = run(a).json()["id"]
    s = a.get(f"/searches/{sid}").json()
    assert s["status"] == "failed" and "not charged" in s["error"]
    assert balance(env, a.uid) == start
    b = member(env, "b@example.com", balance_usd=5)
    sid2 = run(b).json()["id"]
    s2 = b.get(f"/searches/{sid2}").json()
    assert s2["cached"] is False
