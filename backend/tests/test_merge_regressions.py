"""After merging the social-search fan-out (about 20 Google searches per customer search) onto the swarm-1 fixes:
every swarm-1 money/safety fix must still hold on the fan-out path, and the words and numbers around it must match the code."""
import re
from datetime import timedelta
from pathlib import Path

import httpx
import pytest
from sqlalchemy import func, select

from app import live_search
from app.config import settings
from app.models import CreditEntry, Job, SearchQuery, UserSearch
from app.scrapers import google_search, pipeline, query_plan
from app.scrapers.google_search import SearchError
from app.security import utcnow
from test_customer_api import add_job, balance, env, live, member, run  # noqa: F401  (fixtures)

ROOT = Path(__file__).resolve().parent.parent.parent
needs_root = pytest.mark.skipif(not (ROOT / "docs").exists(), reason="repo root not present")


# ---------- swarm-1 S1: a search with no key behind it is an error, never "no results", never billed ----------
def test_with_no_search_key_nothing_is_searched_and_nothing_is_charged(env, monkeypatch):
    monkeypatch.setattr(live_search, "live_search_ready", lambda: (True, ""))          # keys vanished between the check and the run
    for k in ("serpapi_api_key", "serper_api_key"):
        monkeypatch.setattr(settings, k, "")
    with pytest.raises(SearchError):
        google_search.search("hiring copywriter")
    c = member(env, balance_usd=5)
    s = c.get(f"/searches/{run(c).json()['id']}").json()
    assert s["status"] == "failed" and "not charged" in s["error"].lower() and balance(env, c.uid) == 5_000_000
    with env.scope() as db:
        assert db.scalar(select(func.count(CreditEntry.id))) == 0 and db.scalar(select(func.count(SearchQuery.id))) == 0


# ---------- swarm-1 M1: each model call is priced on its own, also across the whole fan-out ----------
def test_long_prompt_rate_is_per_call_across_the_whole_fan_out(env, live):
    live.usage, live.n = (30_000, 500), 5                                  # 20 Google searches x 5 pages x 30K tokens = 3M tokens in total
    c = member(env, balance_usd=50)
    sid = run(c, "fan out per call").json()["id"]
    n = len(live_search.search_targets("fan out per call", None))
    with env.scope() as db:
        e = db.scalar(select(CreditEntry).where(CreditEntry.ref == f"usersearch:{sid}"))
        our, charged = e.meta["our_cost_micro"], -e.delta_micro
    real = n * settings.serpapi_cost_per_search_usd * 1e6 + n * 5 * (30_000 * 0.10 + 500 * 0.50)      # every call under the 100K line: cheap rate card
    assert abs(our - real) < 5 and 1.5 * real <= charged <= 1.5 * real + 5


# ---------- swarm-1 S2: a search that was told "timed out, not charged" must also STOP spending ----------
def test_an_expired_search_stops_running_more_google_searches(env, live, monkeypatch):
    c = member(env, balance_usd=5)
    calls = []
    real = live_search.pipeline.run_pipeline_for_query

    def slow(*a, **k):
        calls.append(a[0])
        with env.scope() as db:
            db.query(UserSearch).update({UserSearch.created_at: utcnow() - timedelta(minutes=11)})
        c.get("/searches/1")                                               # the customer's poll expires it
        return real(*a, **k)

    monkeypatch.setattr(live_search.pipeline, "run_pipeline_for_query", slow)
    run(c)
    assert len(calls) == 1, f"{len(calls)} Google searches ran after the customer was told it timed out"
    assert c.get("/searches/1").json()["status"] == "failed" and balance(env, c.uid) == 5_000_000


# ---------- fan-out: partial failure must be visible and cheaper ----------
def test_a_search_where_some_google_searches_failed_says_so_and_charges_only_the_ones_that_ran(env, live, monkeypatch):
    c = member(env, balance_usd=5)
    stub = google_search.search                                           # the recorder from the `live` fixture
    n = len(live_search.search_targets("hiring copywriter", None))
    state = {"i": 0}

    def flaky(query, num=10, freshness=None, **kw):
        state["i"] += 1
        if state["i"] % 2 == 0:
            raise SearchError("serpapi HTTP 500")
        return stub(query, num, freshness)

    monkeypatch.setattr(google_search, "search", flaky)
    start = balance(env, c.uid)
    s = c.get(f"/searches/{run(c).json()['id']}").json()
    ok = n - n // 2
    assert s["status"] == "done"
    assert s["error"] and f"{ok} of {n}" in s["error"] and "incomplete" in s["error"]
    extraction, _ = live_search.extraction_totals([(2000, 400)] * (3 * ok))
    assert start - balance(env, c.uid) == ok * live_search.search_fee_micro() + extraction


# ---------- a "quota" word inside Google's results is not an out-of-searches answer ----------
def test_a_good_response_that_mentions_a_quota_is_not_an_exhausted_provider(monkeypatch):
    body = {"organic_results": [{"link": "https://www.reddit.com/r/forhire/comments/1/x/", "title": "[HIRING] Copywriter",
                                 "snippet": "Our Google Ads account hit Quota exceeded, so we need a copywriter"}]}
    monkeypatch.setattr(settings, "dev_fixtures", False)
    monkeypatch.setattr(settings, "serpapi_api_key", "a"); monkeypatch.setattr(settings, "serper_api_key", "")
    monkeypatch.setattr(httpx, "get", lambda url, **kw: httpx.Response(200, json=body, request=httpx.Request("GET", url)))
    exhausted: set[str] = set()
    assert len(google_search.search("hiring copywriter", exhausted=exhausted)) == 1 and exhausted == set()


@pytest.mark.parametrize("status,text", [(429, ""), (400, '{"message":"Not enough credits"}')])
def test_real_quota_answers_are_still_recognised(monkeypatch, status, text):
    r = httpx.Response(status, text=text, request=httpx.Request("GET", "https://x.test"))
    with pytest.raises(SearchError) as e:
        google_search._checked("serper", r)
    assert e.value.quota is True


# ---------- the chips in the search box must find the posts they are for ----------
@pytest.mark.parametrize("service,title", [
    ("copywriting", "[HIRING] Copywriter for a SaaS brand"),
    ("email marketing", "[HIRING] Email Marketer for DTC"),
    ("creative strategy", "[HIRING] Creative Strategist for paid social"),
    ("email marketer", "[HIRING] Email Marketing Specialist"),
    ("copywriter", "[HIRING] Copywriting help for a launch"),
    ("funnel builder", "[HIRING] Funnel builder (GoHighLevel)"),
])
def test_reddit_titles_are_matched_on_the_role_not_on_the_ending_of_the_word(service, title):
    query = "site:reddit.com/r/forhire/comments " + live_search.clean_query("hiring " + service)
    assert pipeline.not_a_hiring_post(title, "", query, "https://www.reddit.com/r/forhire/comments/1/x/") is False


def test_reddit_titles_for_another_role_are_still_skipped():
    q = "site:reddit.com/r/forhire/comments hiring email marketer"
    assert pipeline.not_a_hiring_post("[HIRING] Video editor", "", q, "https://www.reddit.com/r/forhire/comments/1/x/") is True
    q = "site:reddit.com/r/forhire/comments hiring copywriter"
    assert pipeline.not_a_hiring_post("[HIRING] Copy chief wanted", "", q, "https://www.reddit.com/r/forhire/comments/1/x/") is True


def test_a_row_stored_before_the_skip_rules_is_not_handed_to_the_customer(env, live, monkeypatch):
    c = member(env, balance_usd=5)
    junk = "Top 38 Email copywriter Jobs"
    with env.scope() as db:
        r = google_search.SearchResult(url="https://jobs.example/old", title=junk, snippet="s", source_query="hiring copywriter", platform="web")
        add_job(db, title=junk, source_url=r.url, dedupe_hash=pipeline.result_hash(r))
    monkeypatch.setattr(google_search, "search", lambda q, num=10, freshness=None, **k: [
        google_search.SearchResult(url="https://jobs.example/old", title=junk, snippet="s", source_query=q, platform="web")])
    s = c.get(f"/searches/{run(c, site=live_search.CUSTOMER_SITES[0]).json()['id']}").json()
    assert s["results"] == 0


# ---------- names: every platform the scrapers can emit has a label and a kind ----------
def test_every_platform_the_scrapers_emit_has_a_label_and_a_kind():
    from app import platforms
    js = (Path(__file__).resolve().parent.parent / "app" / "static" / "site" / "common.js").read_text()
    labels = set(re.findall(r"(\w+):\s*\"", re.search(r"CF\.PLATFORM = \{(.*?)\};", js, re.S).group(1)))
    emitted = {p for _, p in google_search._PLATFORM_HOSTS} | {"linkedin_jobs", "remoteok", "weworkremotely", "remotive", "greenhouse",
                                                               "lever", "ashby", "problogger"}
    assert emitted - labels == set(), "a platform shows its raw key in the filter and on the card"
    assert {p for p in emitted if platforms.kind_of(p) == "web"} == set()


# ---------- numbers in the docs are the numbers in the code ----------
@needs_root
def test_docs_state_the_real_size_and_price_of_a_customer_search_and_of_the_scheduled_plan():
    n = len(live_search.search_targets("hiring copywriter", None))
    boards = len(query_plan.REMOTE_SCOPES)
    costs = (ROOT / "docs" / "COSTS.md").read_text()
    assert f"{n} Google searches" in costs and f"{boards} remote" in costs
    assert f"about ${live_search.estimated_user_price_usd():.2f}" in costs
    assert f"about ${n * settings.serpapi_cost_per_search_usd:.2f}" in costs
    plan = len(query_plan.build_plan())
    per_day = settings.google_max_queries_per_tick * 24
    assert f"{plan} queries" in costs and f"{per_day} a day" in costs
    assert f"{per_day * 30:,}/mo" in costs
    env_example = (ROOT / "backend" / ".env.example").read_text()
    assert f"default plan is {plan} queries" in env_example
    assert "one search used" not in (ROOT / "CHECKLIST.md").read_text()


@needs_root
def test_no_leftover_patch_backups_are_committed():
    left = [p for p in (ROOT / "backend").rglob("*") if p.suffix in {".orig", ".rej"} and ".venv" not in p.parts]
    assert left == []


def test_settings_has_no_duplicate_fields():
    src = (Path(__file__).resolve().parent.parent / "app" / "config.py").read_text()
    names = re.findall(r"^    ([a-z_0-9]+): (?:str|int|float|bool)\b", src, re.M)
    assert len(names) == len(set(names)), sorted({n for n in names if names.count(n) > 1})


def test_app_imports_with_warnings_as_errors():
    import subprocess, sys
    r = subprocess.run([sys.executable, "-W", "error", "-c", "import app.main"], cwd=Path(__file__).resolve().parent.parent,
                       capture_output=True, text=True)
    assert r.returncode == 0, r.stderr[-400:]


def test_recompute_platforms_does_not_turn_an_old_social_post_into_web_because_its_apply_link_is_elsewhere(env, tmp_path, monkeypatch):
    """Job.source_url is the model's apply link when it gave one. For rows without the stored found_url that must not decide 'web'."""
    import subprocess, sys
    from sqlalchemy import create_engine
    from app.db import Base
    db_file = tmp_path / "rp.db"
    e = create_engine(f"sqlite:///{db_file}")
    Base.metadata.create_all(e)
    from sqlalchemy.orm import Session
    with Session(e) as s:
        s.add(Job(dedupe_hash="a", source_url="https://forms.gle/abc", platform="reddit", title="t", source_key="usersearch"))
        s.add(Job(dedupe_hash="b", source_url="https://acme.com/apply", platform="reddit", title="t", source_key="usersearch",
                  extra={"found_url": "https://www.reddit.com/r/forhire/comments/1/x/"}))
        s.add(Job(dedupe_hash="c", source_url="https://www.linkedin.com/jobs/view/1", platform="linkedin", title="t", source_key="usersearch"))
        s.commit()
    root = Path(__file__).resolve().parent.parent
    out = subprocess.run([sys.executable, "scripts/recompute_platforms.py", "--apply"], cwd=root, capture_output=True, text=True,
                         env={"PATH": "/usr/bin", "DATABASE_URL": f"sqlite:///{db_file}", "PYTHONPATH": str(root)})
    assert out.returncode == 0, out.stderr[-500:]
    with Session(e) as s:
        got = {j.dedupe_hash: j.platform for j in s.scalars(select(Job))}
    assert got == {"a": "reddit", "b": "reddit", "c": "linkedin_jobs"}, out.stdout
