"""Regressions from the fan-out audit: partial searches, the monthly budget under concurrency, stale filtered rows, and the numbers in the docs."""
import hashlib
import re
from pathlib import Path

import httpx
from sqlalchemy import func, select

from app import live_search
from app.config import settings
from app.models import CreditEntry, Job, SearchQuery, UserSearch, UserSearchJob
from app.schemas import ExtractedJob, SearchResult
from app.scrapers import google_search, llm_extractor, page_fetcher, pipeline, query_plan
from test_customer_api import CSRF, MODEL, add_job, balance, env, live, member, run  # noqa: F401  (fixtures)


class Resp:
    def __init__(self, status=200, js=None):
        self.status_code, self._j, self.text = status, js, ""

    def json(self):
        return self._j


def _hits(q, n=2):
    host, _, path = (q.split()[0][5:] if q.startswith("site:") else "jobs.example").partition("/")
    path = "u/status/" if host == "x.com" else (path + "/" if path and not path.endswith("/") else path)
    h = hashlib.md5(q.encode()).hexdigest()[:6]
    return [{"link": f"https://{host}/{path}{h}/{i}", "title": f"Hiring copywriter {h}{i}", "snippet": "s"} for i in range(n)]


def _stub_model(monkeypatch):
    monkeypatch.setattr(page_fetcher, "fetch_page_for_result", lambda r: ("<html>" + "job " * 120 + "</html>", True))
    monkeypatch.setattr(llm_extractor, "extract", lambda r, h: ExtractedJob(
        is_real_job=True, title=r.title, usage={"model": MODEL, "input_tokens": 2000, "output_tokens": 400}))


def _both_providers(monkeypatch, serpapi_ok, serper_ok):
    _stub_model(monkeypatch)
    monkeypatch.setattr(settings, "serpapi_api_key", "a")
    monkeypatch.setattr(settings, "serper_api_key", "b")
    monkeypatch.setattr(settings, "google_search_provider", "serpapi")
    monkeypatch.setattr(settings, "dev_fixtures", False)
    n = {"serpapi": 0, "serper": 0}

    def get(url, **kw):
        n["serpapi"] += 1
        return Resp(200, {"organic_results": _hits(kw["params"]["q"])}) if n["serpapi"] <= serpapi_ok else Resp(429, {})

    def post(url, **kw):
        n["serper"] += 1
        return Resp(200, {"organic": _hits(kw["json"]["q"])}) if n["serper"] <= serper_ok else Resp(429, {})

    monkeypatch.setattr(httpx, "get", get)
    monkeypatch.setattr(httpx, "post", post)
    return n


def test_a_search_where_both_providers_run_out_midway_says_so_is_charged_only_for_what_ran_and_is_not_cached(env, monkeypatch):
    n = _both_providers(monkeypatch, serpapi_ok=5, serper_ok=4)
    c = member(env, balance_usd=5)
    s = c.get(f"/searches/{run(c).json()['id']}").json()
    assert s["status"] == "done" and "Only 9 of 20 Google searches could run" in s["error"] and "charged only for the 9" in s["error"]
    with env.scope() as db:
        meta = db.scalars(select(CreditEntry).where(CreditEntry.kind == "usage")).one().meta
    assert meta["fee_micro"] == 9 * live_search.search_fee_micro()                      # nothing billed for the 11 that never ran
    again = run(c).json()                                                                # the same words again are a NEW paid search, not a free "complete" copy
    assert again["cached"] is False
    q = c.post("/searches/estimate", json={"query": "hiring copywriter", "freshness": "w"}, headers=CSRF).json()
    assert q["cached"] is False and q["price_usd"] > 0


def test_a_complete_search_has_no_note_and_is_cached(env, monkeypatch):
    _both_providers(monkeypatch, serpapi_ok=999, serper_ok=0)
    c = member(env, balance_usd=5)
    s = c.get(f"/searches/{run(c).json()['id']}").json()
    assert s["error"] is None and run(c).json()["cached"] is True


def test_searches_that_started_together_cannot_overshoot_the_monthly_budget(env, monkeypatch):
    n = _both_providers(monkeypatch, serpapi_ok=999, serper_ok=0)
    monkeypatch.setattr(settings, "serpapi_monthly_budget", 25)
    a, b = member(env, "a@example.com", 5), member(env, "b@example.com", 5)
    with env.scope() as db:                                                              # both pass the start check before either has logged a search
        sa = live_search.start_search(db, db.get(live_search.User, a.uid), "hiring copywriter", "w", None)
        sb = live_search.start_search(db, db.get(live_search.User, b.uid), "hiring email marketer", "w", None)
        ids = [sa.id, sb.id]
    for sid in ids:
        live_search.run_search(sid)
    assert n["serpapi"] <= 25, n
    with env.scope() as db:
        assert query_plan.searches_used_this_month(db) <= 25
        done = [s for s in db.scalars(select(UserSearch)).all()]
        assert sum(1 for s in done if s.error) >= 1                                       # the one cut short says so


def test_a_stored_row_that_todays_filters_reject_is_not_attached_to_a_search(env, live):
    stale = SearchResult(url="https://www.bebee.com/job/abc", title="Hiring copywriter 0", snippet="s", source_query="hiring copywriter", platform="web")
    spam = SearchResult(url="https://jobs.example/x/1", title="Hiring copywriter 1", snippet="s", source_query="hiring copywriter", platform="web")
    ok = SearchResult(url="https://jobs.example/x/2", title="Hiring copywriter 2", snippet="s", source_query="hiring copywriter", platform="web")
    with env.scope() as db:
        add_job(db, title="x", source_url=stale.url, dedupe_hash=pipeline.result_hash(stale))
        add_job(db, title="y", source_url=spam.url, is_real_job=False, dedupe_hash=pipeline.result_hash(spam))
        add_job(db, title="Copywriter", source_url=ok.url, dedupe_hash=pipeline.result_hash(ok))
    live_search.pipeline.google_search.search = lambda query, **k: [stale, spam, ok] if query == "hiring copywriter" else []
    c = member(env, balance_usd=5)
    s = c.get(f"/searches/{run(c).json()['id']}").json()
    assert s["results"] == 1 and [j["title"] for j in c.get(f"/searches/{s['id']}/jobs").json()] == ["Copywriter"]


def test_the_costs_doc_and_env_example_state_the_real_numbers():
    root = Path(__file__).resolve().parents[2]
    costs = root / "docs" / "COSTS.md"
    if not costs.exists():
        return
    n = len(live_search.search_targets("hiring copywriter", None))
    text = costs.read_text()
    assert f"{n} Google searches" in text and f"about ${live_search.estimated_user_price_usd():.2f}" in text
    assert f"{len(query_plan.build_plan())} queries" in text
    remote = len(query_plan.REMOTE_SCOPES)
    assert f"{remote} remote" in text
    env_example = (Path(__file__).resolve().parents[1] / ".env.example").read_text()
    assert f"{len(query_plan.build_plan())} queries" in env_example
