"""Google-layer tests: query plan, due/budget logic, SerpAPI params, fetch fallback chain, pipeline pre-check.
SerpAPI/Claude/Playwright calls are stubbed; nothing here proves the live services behave as assumed."""
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone

import httpx
import pytest
from sqlalchemy import create_engine, func, select
from sqlalchemy.orm import sessionmaker

from app.config import settings
from app.db import Base
from app.dedup import dedupe_hash
from app.models import Job, ScrapeRun, SearchQuery
from app.schemas import ExtractedJob, SearchResult
from app.scrapers import google_search, llm_extractor, page_fetcher, pipeline, query_plan
from app.scrapers.query_plan import (
    JOB_SCOPES, OPEN_WEB, POST_SCOPES, POST_TERMS, TERMS, QuerySpec, build_plan, due_queries, searches_used_this_month,
)

NOW = datetime(2026, 10, 8, 12, 0, tzinfo=timezone.utc)


@pytest.fixture
def db(monkeypatch):
    engine = create_engine("sqlite://")
    Base.metadata.create_all(engine)
    Session = sessionmaker(bind=engine, autoflush=False)

    @contextmanager
    def scope():
        s = Session()
        try:
            yield s
            s.commit()
        except Exception:
            s.rollback()
            raise
        finally:
            s.close()

    monkeypatch.setattr(pipeline, "session_scope", scope)
    monkeypatch.setattr(query_plan, "session_scope", scope)
    monkeypatch.setattr(settings, "dev_fixtures", False)
    monkeypatch.setattr(settings, "anthropic_api_key", "k")
    monkeypatch.setattr(settings, "serpapi_api_key", "k")
    monkeypatch.setattr(settings, "google_search_provider", "serpapi")
    return scope


# ---------- plan ----------
def test_plan_has_open_web_and_site_scoped_queries_without_duplicates():
    texts = [q.text for q in build_plan()]
    assert len(texts) == len(set(texts)) == len(OPEN_WEB) + len(POST_TERMS) * len(POST_SCOPES) + len(TERMS) * len(JOB_SCOPES)
    assert '"hiring copywriter"' in texts
    assert 'site:linkedin.com/posts "hiring" copywriter' in texts
    assert "site:linkedin.com/jobs/view copywriter jobs" in texts
    assert 'site:x.com "hiring" "creative strategist"' in texts
    assert 'site:facebook.com "hiring" "email copywriter"' in texts
    assert 'site:reddit.com/r/forhire/comments "hiring" "creative strategist"' in texts
    assert 'site:reddit.com/r/hiring/comments "hiring" copywriter' in texts
    assert "site:dynamitejobs.com/company copywriter jobs" in texts
    assert "site:himalayas.app/companies copywriter jobs" in texts
    assert "site:remotive.com/remote/jobs/ creative strategist jobs" in texts
    assert not any("tryremotely.com" in t or "creatorjobboard.com" in t for t in texts)
    assert all(len(t) <= 256 for t in texts)
    assert all(not t.startswith('"') or " site:" not in t for t in texts)


# ---------- due / budget ----------
def test_never_run_queries_are_due_first_and_backfill_a_week(db):
    with db() as s:
        plan = build_plan()
        s.add(SearchQuery(query_key=plan[0].key, provider="serpapi", ran_at=NOW - timedelta(hours=1)))
        s.flush()
        due = due_queries(s, plan, now=NOW)
        assert plan[0].key not in [d.key for d in due]            # ran 1h ago, cycle is 12h
        assert len(due) == len(plan) - 1 and {d.freshness for d in due} == {"w"}


def test_ok_queries_return_after_cycle_errors_after_an_hour(db):
    with db() as s:
        a, b = QuerySpec("a", "d"), QuerySpec("b", "d")
        s.add_all([
            SearchQuery(query_key="a", provider="serpapi", ran_at=NOW - timedelta(hours=13)),
            SearchQuery(query_key="b", provider="serpapi", ran_at=NOW - timedelta(minutes=70), error="boom"),
        ])
        s.flush()
        due = due_queries(s, [a, b], now=NOW)
        assert [d.key for d in due] == ["a", "b"] and {d.freshness for d in due} == {"d"}


def test_oldest_first_and_limit(db):
    with db() as s:
        plan = [QuerySpec(c, "d") for c in "abc"]
        for c, h in zip("abc", (30, 50, 40)):
            s.add(SearchQuery(query_key=c, provider="serpapi", ran_at=NOW - timedelta(hours=h)))
        s.flush()
        assert [d.key for d in due_queries(s, plan, now=NOW, limit=2)] == ["b", "c"]


def test_budget_counts_only_successful_searches_this_month(db):
    with db() as s:
        s.add_all([
            SearchQuery(query_key="a", provider="serpapi", ran_at=NOW),
            SearchQuery(query_key="b", provider="serpapi", ran_at=NOW, error="x"),                       # failed: not billed
            SearchQuery(query_key="c", provider="serpapi", ran_at=NOW - timedelta(days=40)),             # last month
            SearchQuery(query_key="d", provider="serper", ran_at=NOW),                                   # other provider
        ])
        s.flush()
        assert searches_used_this_month(s, NOW) == 1


def test_tick_stops_when_budget_exhausted(db, monkeypatch):
    monkeypatch.setattr(settings, "serpapi_monthly_budget", 2)
    with db() as s:
        s.add_all([SearchQuery(query_key=f"q{i}", provider="serpapi") for i in range(2)])
    called = []
    monkeypatch.setattr(pipeline, "run_pipeline_for_query", lambda *a, **k: called.append(a) or {"searched": True})
    assert query_plan.run_due_queries() == [] and not called


def test_tick_caps_to_remaining_budget_and_logs_each_search(db, monkeypatch):
    monkeypatch.setattr(settings, "serpapi_monthly_budget", 3)
    monkeypatch.setattr(settings, "google_max_queries_per_tick", 10)
    with db() as s:
        s.add(SearchQuery(query_key="old", provider="serpapi"))
    monkeypatch.setattr(pipeline, "run_pipeline_for_query",
                        lambda q, freshness=None, **k: {"query": q, "searched": True, "search_hits": 4, "added": 1})
    out = query_plan.run_due_queries()
    assert len(out) == 2  # budget 3, 1 used -> 2 left
    with db() as s:
        assert s.scalar(select(func.count(SearchQuery.id))) == 3
        assert s.scalar(select(func.sum(SearchQuery.new_jobs))) == 2


def test_tick_does_not_log_when_search_not_attempted(db, monkeypatch):
    monkeypatch.setattr(pipeline, "run_pipeline_for_query", lambda *a, **k: {"searched": False, "error": "no key"})
    query_plan.run_due_queries(max_queries=3)
    with db() as s:
        assert s.scalar(select(func.count(SearchQuery.id))) == 0


# ---------- serpapi request ----------
def test_serpapi_sends_freshness_filter(monkeypatch):
    seen = {}

    class R:
        def raise_for_status(self): pass
        def json(self): return {"organic_results": [{"link": "https://x.com/a/status/1", "title": "t", "snippet": "s"}]}

    monkeypatch.setattr(google_search.httpx, "get", lambda url, params=None, timeout=None: seen.update(p=params) or R())
    monkeypatch.setattr(settings, "dev_fixtures", False)
    monkeypatch.setattr(settings, "serpapi_api_key", "k")
    monkeypatch.setattr(settings, "google_search_provider", "serpapi")
    res = google_search.search('"hiring copywriter"', freshness="d")
    assert seen["p"]["tbs"] == "qdr:d" and seen["p"]["q"] == '"hiring copywriter"' and res[0].platform == "twitter"
    google_search.search("q")
    assert "tbs" not in seen["p"]


# ---------- fetch fallback ----------
LONG = "<html><body>" + "real job text " * 60 + "</body></html>"
SHELL = "<html><body><div id=root></div></body></html>"
R = SearchResult(url="https://example.org/job/1", title="Copywriter <b>", snippet="Pay $50/hr", source_query="q", platform="web")


@pytest.fixture
def modes(monkeypatch):
    monkeypatch.setattr(settings, "dev_fixtures", False)
    monkeypatch.setattr(settings, "playwright_fallback", True)
    monkeypatch.setattr(settings, "firecrawl_api_key", "")
    calls = []

    def install(**behaviour):
        def fake(url, mode="simple"):
            calls.append(mode)
            b = behaviour.get(mode)
            if isinstance(b, Exception):
                raise b
            return b
        monkeypatch.setattr(page_fetcher, "fetch", fake)
        return calls
    return install


def test_simple_fetch_wins_when_it_works(modes):
    calls = modes(simple=LONG)
    assert page_fetcher.fetch_page_for_result(R) == (LONG, True) and calls == ["simple"]


def test_js_shell_falls_through_to_playwright(modes):
    calls = modes(simple=SHELL, playwright=LONG)
    assert page_fetcher.fetch_page_for_result(R)[1] is True and calls == ["simple", "playwright"]


def test_blocked_then_firecrawl_when_key_set(modes, monkeypatch):
    monkeypatch.setattr(settings, "firecrawl_api_key", "fc")
    calls = modes(simple=RuntimeError("403"), playwright=RuntimeError("no browser"), firecrawl=LONG)
    assert page_fetcher.fetch_page_for_result(R)[1] is True and calls == ["simple", "playwright", "firecrawl"]


def test_everything_fails_falls_back_to_escaped_snippet(modes):
    modes(simple=RuntimeError("403"), playwright=RuntimeError("x"))
    page, fetched = page_fetcher.fetch_page_for_result(R)
    assert fetched is False and "Pay $50/hr" in page and "<b>" not in page and "&lt;b&gt;" in page


def test_social_platforms_never_fetch(modes):
    calls = modes(simple=LONG)
    for platform in ("twitter", "linkedin", "facebook"):
        calls.clear()
        row = R.model_copy(update={"platform": platform})
        assert page_fetcher.fetch_page_for_result(row)[1] is False and calls == []


def test_site_query_drops_results_from_other_hosts():
    assert google_search.on_site("https://www.linkedin.com/posts/a", "linkedin.com/posts")
    assert google_search.on_site("https://twitter.com/a/status/1", "x.com")
    assert google_search.on_site("https://x.com/someone/status/123", "x.com")
    assert not google_search.on_site("https://x.com/copywriting_job", "x.com")
    assert not google_search.on_site("https://x.com/someone/article/123", "x.com")
    assert google_search.on_site("https://www.reddit.com/r/forhire/comments/abc/hiring_copywriter", "reddit.com/r/forhire/comments")
    assert not google_search.on_site("https://www.reddit.com/r/forhire/", "reddit.com/r/forhire/comments")
    assert not google_search.on_site("https://www.reddit.com/r/advertising/comments/abc/how_to", "reddit.com/r/forhire/comments")
    assert not google_search.on_site("https://www.ziprecruiter.com/Jobs/Copywriter", "linkedin.com")
    assert not google_search.on_site("https://www.linkedin.com/jobs/copywriter-jobs", "linkedin.com/jobs/view")
    assert google_search.on_site("https://dynamitejobs.com/company/kindred/remote-job/lead-copywriter", "dynamitejobs.com/company")
    assert not google_search.on_site("https://dynamitejobs.com/skill/remote-copywriting-jobs", "dynamitejobs.com/company")
    assert google_search.on_site("https://himalayas.app/companies/codekeeper/jobs/marketing-copywriter", "himalayas.app/companies")
    assert not google_search.on_site("https://himalayas.app/jobs/copywriting", "himalayas.app/companies")
    assert google_search.on_site("https://weworkremotely.com/remote-jobs/acme-copywriter", "weworkremotely.com/remote-jobs/")
    assert not google_search.on_site("https://weworkremotely.com/remote-jobs-copywriting", "weworkremotely.com/remote-jobs/")
    assert google_search.on_site("https://remote.co/job-details/copywriter-256ca411", "remote.co/job-details/")
    assert not google_search.on_site("https://remote.co/remote-jobs/copywriting", "remote.co/job-details/")
    assert google_search.on_site("https://remotive.com/remote/jobs/writing/copywriter-1", "remotive.com/remote/jobs/")
    assert not google_search.on_site("https://remotive.com/remote-jobs/o-marketing-copywriter", "remotive.com/remote/jobs/")
    assert google_search._platform_from_url("https://www.himalayas.app/companies/x/jobs/y") == "himalayas"
    assert google_search._platform_from_url("https://jobs.workable.com/view/abc/creative-strategist") == "workable"
    assert google_search._platform_from_url("https://euremotejobs.com/job/copywriter/") == "euremotejobs"
    assert google_search._platform_from_url("https://remote.co/job-details/copywriter-1") == "remoteco"
    assert google_search._platform_from_url("https://4dayweek.io/job/copywriter-at-anthropic") == "fourdayweek"


def test_quota_falls_back_to_serper_and_keeps_only_the_site(monkeypatch):
    monkeypatch.setattr(settings, "dev_fixtures", False)
    monkeypatch.setattr(settings, "serpapi_api_key", "k")
    monkeypatch.setattr(settings, "serper_api_key", "s")
    monkeypatch.setattr(settings, "google_search_provider", "serpapi")

    def fake_get(url, **kw):
        return httpx.Response(429, json={"error": "Your account has run out of searches."}, request=httpx.Request("GET", "https://serpapi.com/search.json"))

    def fake_post(url, **kw):
        return httpx.Response(200, json={"organic": [
            {"link": "https://www.linkedin.com/posts/someone_hiring", "title": "Hiring a copywriter", "snippet": "remote"},
            {"link": "https://www.ziprecruiter.com/Jobs/Copywriter", "title": "Copywriter jobs", "snippet": "browse"},
        ]}, request=httpx.Request("POST", "https://google.serper.dev/search"))

    monkeypatch.setattr(google_search.httpx, "get", fake_get)
    monkeypatch.setattr(google_search.httpx, "post", fake_post)
    res = google_search.search("site:linkedin.com/posts hiring copywriter")
    assert [r.url for r in res] == ["https://www.linkedin.com/posts/someone_hiring"]
    assert res[0].platform == "linkedin"


# ---------- pipeline ----------
def _extracted(**kw):
    return ExtractedJob(is_real_job=True, title="Copywriter", usage={"input_tokens": 10, "output_tokens": 5}, **kw)


def test_known_url_skips_fetch_and_claude_and_new_url_is_stored(db, monkeypatch):
    known = SearchResult(url="https://jobs.lever.co/acme/1?utm_source=g", title="t", snippet="s", source_query="q", platform="lever")
    fresh = SearchResult(url="https://example.org/job/2", title="Copywriter", snippet="s", source_query="q", platform="web")
    with db() as s:  # as if the Lever feed already stored it (same URL, tracking params stripped by the hash)
        s.add(Job(dedupe_hash=dedupe_hash(url="https://jobs.lever.co/acme/1", title="x"), source_url="u", platform="lever",
                  title="x", source_key="lever"))
    monkeypatch.setattr(google_search, "search", lambda *a, **k: [known, fresh])
    fetched, extracted = [], []
    monkeypatch.setattr(page_fetcher, "fetch_page_for_result", lambda r: fetched.append(r.url) or (LONG, True))
    monkeypatch.setattr(llm_extractor, "extract", lambda r, h: extracted.append(r.url) or _extracted())

    stats = pipeline.run_pipeline_for_query('"hiring copywriter"', freshness="d")
    assert fetched == extracted == ["https://example.org/job/2"]
    assert (stats["added"], stats["updated"], stats["tokens_in"], stats["tokens_out"], stats["searched"]) == (1, 1, 10, 5, True)
    with db() as s:
        job = s.scalar(select(Job).where(Job.source_url == "https://example.org/job/2"))
        assert job.extra == {"usage": {"input_tokens": 10, "output_tokens": 5}, "page_fetched": True}


def test_second_run_costs_nothing(db, monkeypatch):
    r = SearchResult(url="https://example.org/job/3", title="Copywriter", snippet="s", source_query="q", platform="web")
    monkeypatch.setattr(google_search, "search", lambda *a, **k: [r])
    monkeypatch.setattr(page_fetcher, "fetch_page_for_result", lambda r: (LONG, True))
    n = []
    monkeypatch.setattr(llm_extractor, "extract", lambda r, h: n.append(1) or _extracted())
    pipeline.run_pipeline_for_query("q")
    again = pipeline.run_pipeline_for_query("q")
    assert len(n) == 1 and (again["added"], again["updated"]) == (0, 1)


def test_search_error_is_reported_not_raised(db, monkeypatch):
    def boom(*a, **k):
        raise RuntimeError("429 out of searches")
    monkeypatch.setattr(google_search, "search", boom)
    stats = pipeline.run_pipeline_for_query("q")
    assert stats["searched"] and "429" in stats["search_error"]


def test_long_query_source_key_fits_column():
    key = pipeline._source_key('"hiring email marketer" site:linkedin.com/posts ' + "x" * 80)
    assert len(key) <= 64 and key == pipeline._source_key('"hiring email marketer" site:linkedin.com/posts ' + "x" * 80)


# ---------- cost control ----------
def _runs(s, key, new_jobs_newest_first, start=NOW - timedelta(days=2), step=timedelta(hours=1)):
    """Insert runs so that new_jobs_newest_first[0] is the most recent."""
    n = len(new_jobs_newest_first)
    for i, nj in enumerate(reversed(new_jobs_newest_first)):
        s.add(SearchQuery(query_key=key, provider="serpapi", new_jobs=nj, ran_at=start + step * i))
    s.flush()
    return start + step * (n - 1)  # time of newest run


def test_quiet_query_gets_grace_before_any_backoff(db):
    with db() as s:
        newest = _runs(s, "q", [0, 0])                    # 2 empty runs: still on the normal 12h cycle
        got = due_queries(s, [QuerySpec("q", "d")], now=newest + timedelta(hours=12, minutes=1))
        assert [g.freshness for g in got] == ["d"]


def test_dead_query_backs_off_and_widens_window(db):
    with db() as s:
        newest = _runs(s, "dead", [0, 0, 0, 0, 0])       # 5 empty runs -> wait 12h x 2^3 = 4 days
        spec = QuerySpec("dead", "d")
        assert due_queries(s, [spec], now=newest + timedelta(days=3)) == []
        got = due_queries(s, [spec], now=newest + timedelta(days=4, minutes=1))
        assert [g.freshness for g in got] == ["w"]        # >24h gap needs a week window


def test_backoff_is_capped_at_a_week(db):
    with db() as s:
        newest = _runs(s, "dead", [0] * 12)
        spec = QuerySpec("dead", "d")
        assert due_queries(s, [spec], now=newest + timedelta(days=6, hours=23)) == []
        got = due_queries(s, [spec], now=newest + timedelta(days=7))
        assert [g.freshness for g in got] == ["w"]        # a 7-day gap is covered by the week window


def test_window_always_covers_the_gap():
    from app.scrapers.query_plan import freshness_for
    assert freshness_for(timedelta(hours=12)) == "d" and freshness_for(timedelta(hours=24)) == "d"
    assert freshness_for(timedelta(hours=25)) == "w" and freshness_for(timedelta(days=7)) == "w"
    assert freshness_for(timedelta(days=7, minutes=1)) == "m"


def test_a_productive_run_resets_the_backoff(db):
    with db() as s:
        newest = _runs(s, "q", [3, 0, 0, 0])              # newest run found jobs -> streak 0
        got = due_queries(s, [QuerySpec("q", "d")], now=newest + timedelta(hours=12, minutes=1))
        assert [g.freshness for g in got] == ["d"]


def test_failed_runs_do_not_count_toward_backoff(db):
    with db() as s:
        s.add_all([SearchQuery(query_key="q", provider="serpapi", new_jobs=0, error="429", ran_at=NOW - timedelta(hours=h)) for h in (5, 4, 3)])
        s.add(SearchQuery(query_key="q", provider="serpapi", new_jobs=2, ran_at=NOW - timedelta(hours=20)))
        s.flush()
        assert [g.freshness for g in due_queries(s, [QuerySpec("q", "d")], now=NOW)] == ["d"]  # error retry after 1h


def test_page_text_sent_to_claude_is_capped():
    html = "<html><body>" + "word " * 5000 + "</body></html>"
    assert len(llm_extractor._html_to_text(html)) <= 6000


def test_pricing_known_and_unknown_models():
    from app.pricing import cost_usd
    assert cost_usd("claude-haiku-4-5-20251001", 1_000_000, 0) == 1.00
    assert cost_usd("claude-haiku-4-5-20251001", 2000, 400) == pytest.approx(0.004)
    assert cost_usd("some-new-model", 100, 100) is None


def test_charge_is_cost_times_markup_rounded_up():
    from app.pricing import CREDIT_MARKUP, charge_usd, cost_usd
    assert CREDIT_MARKUP == 1.5
    cost = cost_usd("claude-haiku-4-5-20251001", 2000, 400)          # 0.004
    assert charge_usd("claude-haiku-4-5-20251001", 2000, 400) == pytest.approx(cost * 1.5)   # 0.006
    assert charge_usd("claude-haiku-4-5-20251001", 1, 1) >= cost_usd("claude-haiku-4-5-20251001", 1, 1) * 1.5  # never rounds down
    assert charge_usd("unpriced-model", 10, 10) is None
