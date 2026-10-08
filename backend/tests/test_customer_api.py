"""What a signed-in customer uses: job filters, the public preview, and live searches (money rules included)."""
from datetime import timedelta

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import func, select

import authkit
from authkit import CSRF, PW
from app import credits, live_search
from app.config import settings
from app.main import app
from app.models import CreditEntry, Job, JobTag, ScrapeRun, Source, User, UserSearch, UserSearchJob
from app.schemas import ExtractedJob, SearchResult
from app.scrapers import google_search, llm_extractor, page_fetcher, query_plan
from app.scrapers.google_search import SearchError
from app.security import utcnow
from app.tagging import set_tags

MODEL = "claude-haiku-5-5"            # the default extraction model: $0.10 in / $0.50 out per million tokens


@pytest.fixture
def env(monkeypatch):
    e = authkit.make_env(monkeypatch)
    yield e
    app.dependency_overrides.clear()


def member(env, email="member@example.com", balance_usd=0.0):
    """A signed-in customer (own client) with the given credit."""
    with env.scope() as db:
        uid, _ = authkit.make_user(db, email, balance_micro=credits.usd_to_micro(balance_usd))
    c = TestClient(app)
    assert authkit.login(env, c, email).status_code == 200
    c.uid = uid
    return c


def add_job(db, **kw):
    kw.setdefault("source_url", f"https://x.test/{db.query(Job).count()}")
    kw.setdefault("platform", "web"); kw.setdefault("source_key", "s"); kw.setdefault("title", "Job")
    kw.setdefault("dedupe_hash", f"h{db.query(Job).count()}")
    j = Job(**kw); db.add(j); db.flush(); set_tags(db, j); return j


# =============== job filters ===============
@pytest.fixture
def seeded(env):
    now = utcnow()
    with env.scope() as db:
        add_job(db, title="Senior Email Marketing Manager", platform="remoteok", type="full_time", remote=True, pay_min=90000, pay_max=120000,
                pay_period="year", pay_text="$90-120k", first_seen_at=now - timedelta(hours=2))
        add_job(db, title="Direct Response Copywriter", platform="reddit", type="contract", remote=True, pay_min=40, pay_max=60,
                pay_period="hour", pay_text="$40-60/hr", first_seen_at=now - timedelta(days=3))
        add_job(db, title="Landing Page Builder (100% remote)", platform="web", type="contract", remote=None, first_seen_at=now - timedelta(days=20))
        add_job(db, title="Funnel Designer", platform="linkedin", type="full_time", remote=False, first_seen_at=now - timedelta(days=60))
        add_job(db, title="Spam post", platform="web", is_real_job=False)
    return member(env)


def titles(r):
    return [j["title"] for j in r.json()]


def test_filters_each_narrow_the_list_and_total_count_ignores_paging(seeded):
    c = seeded
    assert len(titles(c.get("/jobs"))) == 4 and c.get("/jobs").headers["x-total-count"] == "4"           # the spam post is hidden
    assert titles(c.get("/jobs?platform=reddit&platform=remoteok")) == ["Senior Email Marketing Manager", "Direct Response Copywriter"]
    assert titles(c.get("/jobs?type=contract")) == ["Direct Response Copywriter", "Landing Page Builder (100% remote)"]
    assert titles(c.get("/jobs?remote=true")) == ["Senior Email Marketing Manager", "Direct Response Copywriter"]
    assert titles(c.get("/jobs?remote=false")) == ["Landing Page Builder (100% remote)", "Funnel Designer"]
    assert titles(c.get("/jobs?has_pay=true")) == ["Senior Email Marketing Manager", "Direct Response Copywriter"]
    assert titles(c.get("/jobs?pay_period=hour&min_pay=50")) == ["Direct Response Copywriter"]
    assert titles(c.get("/jobs?pay_period=year&min_pay=100000")) == ["Senior Email Marketing Manager"]
    assert titles(c.get("/jobs?posted_within_days=7")) == ["Senior Email Marketing Manager", "Direct Response Copywriter"]
    assert titles(c.get("/jobs?tag=email-marketing&tag=remote")) == ["Senior Email Marketing Manager"]
    page = c.get("/jobs?limit=1&offset=1")
    assert titles(page) == ["Direct Response Copywriter"] and page.headers["x-total-count"] == "4"


def test_search_text_is_literal_so_percent_and_underscore_do_not_match_everything(seeded):
    c = seeded
    assert titles(c.get("/jobs?q=%25")) == ["Landing Page Builder (100% remote)"]          # '%' matches only a real percent sign
    assert titles(c.get("/jobs?q=_")) == []
    assert titles(c.get("/jobs?q=copywriter")) == ["Direct Response Copywriter"]
    assert titles(c.get("/jobs?q=COPYWRITER")) == ["Direct Response Copywriter"]
    assert titles(c.get("/jobs?q=%20%20")) != []                                              # blank search = no filter


def test_sorting_puts_missing_pay_last(seeded):
    c = seeded
    assert titles(c.get("/jobs?sort=pay_high"))[:2] == ["Senior Email Marketing Manager", "Direct Response Copywriter"]
    assert titles(c.get("/jobs?sort=oldest"))[0] == "Funnel Designer"
    assert titles(c.get("/jobs?sort=newest"))[0] == "Senior Email Marketing Manager"


@pytest.mark.parametrize("qs", ["limit=101", "limit=0", "offset=10001", "offset=-1", "sort=drop_table", "pay_period=century", "min_pay=-1", "posted_within_days=0"])
def test_bad_parameters_are_rejected(seeded, qs):
    assert seeded.get(f"/jobs?{qs}").status_code == 422


def test_facets_give_counts_for_the_sidebar(seeded):
    f = seeded.get("/jobs/facets").json()
    assert f["total"] == 4
    assert {p["value"]: p["count"] for p in f["platforms"]} == {"remoteok": 1, "reddit": 1, "web": 1, "linkedin": 1}
    assert {t["value"]: t["count"] for t in f["types"]} == {"full_time": 2, "contract": 2}
    assert any(t["value"] == "email-marketing" for t in f["tags"]) and f["newest_at"]


def test_jobs_need_a_signed_in_account(seeded):
    anon = TestClient(app)
    assert anon.get("/jobs").status_code == 401 and anon.get("/jobs/facets").status_code == 401


# =============== public preview ===============
def test_public_preview_is_redacted_and_limited(env):
    with env.scope() as db:
        for i in range(9):
            add_job(db, title=f"Email Marketer {i}", company_or_poster="Secret Co", source_url=f"https://secret.test/{i}",
                    description="private detail", pay_text="$50/hr", first_seen_at=utcnow() - timedelta(hours=i))
    r = TestClient(app).get("/public/preview")                                           # no login needed
    assert r.status_code == 200 and len(r.json()) == 6
    assert set(r.json()[0]) == {"title", "platform", "type", "pay", "remote", "seen_at"}
    blob = r.text
    assert "Secret Co" not in blob and "secret.test" not in blob and "private detail" not in blob


def test_public_stats_count_real_jobs_and_report_liveness(env):
    with env.scope() as db:
        add_job(db, platform="remoteok"); add_job(db, platform="web"); add_job(db, is_real_job=False)
        add_job(db, first_seen_at=utcnow() - timedelta(days=5))
    a = TestClient(app).get("/public/stats").json()
    assert (a["jobs_total"], a["jobs_new_24h"], a["sources"], a["live"]) == (4 - 1, 2, 2, False)
    public_api_clear()
    with env.scope() as db:
        src = Source(key="remoteok", kind="direct_api", display_name="r"); db.add(src); db.flush()
        db.add(ScrapeRun(source_id=src.id, status="ok", finished_at=utcnow()))
    assert TestClient(app).get("/public/stats").json()["live"] is True


def public_api_clear():
    from app.api import public
    public._cache.clear()


def test_public_endpoints_are_cached_and_rate_limited(env, monkeypatch):
    with env.scope() as db:
        add_job(db)
    c = TestClient(app)
    first = c.get("/public/stats").json()["jobs_total"]
    with env.scope() as db:
        add_job(db)
    assert c.get("/public/stats").json()["jobs_total"] == first                           # served from the 60s cache
    monkeypatch.setattr(settings, "public_rate_limit_per_min", 3)
    codes = [TestClient(app).get("/public/config").status_code for _ in range(5)]
    assert codes[-1] == 429


def test_public_config_hides_nothing_sensitive(env):
    cfg = TestClient(app).get("/public/config").json()
    assert set(cfg) == {"live_search", "search_price_usd", "signup_bonus_usd"} and cfg["live_search"] is False


# =============== live search ===============
@pytest.fixture
def live(monkeypatch):
    """Switch live search on and stub everything outside our code. Returns a recorder."""
    monkeypatch.setattr(settings, "serpapi_api_key", "test-key")
    monkeypatch.setattr(settings, "google_search_provider", "serpapi")
    rec = type("Rec", (), {})()
    rec.searches, rec.n, rec.usage, rec.fail = [], 3, (2000, 400), None

    def search(query, num=10, freshness=None):
        rec.searches.append((query, num, freshness))
        if rec.fail:
            raise rec.fail
        return [SearchResult(url=f"https://jobs.example/{abs(hash(query)) % 10**6}/{i}", title=f"Hiring copywriter {i}", snippet="s",
                             source_query=query, platform="web") for i in range(rec.n)]

    monkeypatch.setattr(google_search, "search", search)
    monkeypatch.setattr(page_fetcher, "fetch_page_for_result", lambda r: ("<html>" + "job " * 120 + "</html>", True))
    monkeypatch.setattr(llm_extractor, "extract", lambda r, h: ExtractedJob(
        is_real_job=True, title=r.title, usage={"model": MODEL, "input_tokens": rec.usage[0], "output_tokens": rec.usage[1]}))
    return rec


def run(c, query="hiring copywriter", freshness="w", site=None):
    return c.post("/searches", json={"query": query, "freshness": freshness, "site": site}, headers=CSRF)


def balance(env, uid):
    with env.scope() as db:
        return db.scalar(select(User.balance_micro).where(User.id == uid))


def test_live_search_is_off_until_keys_exist(env):
    c = member(env, balance_usd=5)
    assert c.get("/searches/config").json()["ready"] is False
    r = run(c)
    assert r.status_code == 503 and "being switched on" in r.json()["detail"]
    assert c.post("/searches/estimate", json={"query": "hiring copywriter", "freshness": "w"}, headers=CSRF).json()["ready"] is False


def test_a_search_runs_stores_jobs_and_charges_exactly_what_it_cost(env, live):
    c = member(env, balance_usd=5)
    start = balance(env, c.uid)
    r = run(c)
    assert r.status_code == 202
    sid = r.json()["id"]
    s = c.get(f"/searches/{sid}").json()                              # background task already ran (TestClient runs it inline)
    assert s["status"] == "done" and s["results"] == 3 and s["new_jobs"] == 3 and s["cached"] is False
    expected = live_search.search_fee_micro() + credits.usage_cost_micro(MODEL, 3 * 2000, 3 * 400)
    assert credits.usd_to_micro(s["cost_usd"]) == expected
    assert balance(env, c.uid) == start - expected
    with env.scope() as db:
        e = db.scalar(select(CreditEntry).where(CreditEntry.user_id == c.uid))
        assert (e.kind, e.ref, e.delta_micro) == ("usage", f"usersearch:{sid}", -expected)
        assert e.meta["tokens_in"] == 6000 and e.meta["search_id"] == sid
    assert len(c.get(f"/searches/{sid}/jobs").json()) == 3
    assert c.get("/jobs").headers["x-total-count"] == "3"            # the jobs are in the shared database for everyone
    assert live.searches == [("hiring copywriter", settings.user_search_results, "w")]


def test_the_price_includes_the_1_5x_markup(env, live):
    assert settings.extraction_model == MODEL
    # 2,000 in + 400 out on Haiku 5.5 = $0.0002 + $0.0002 = $0.0004 for us; customers pay 1.5x = $0.0006
    assert credits.usage_cost_micro(MODEL, 2000, 400) == 600
    assert live_search.search_fee_micro() == 22_500                   # $0.015 * 1.5


def test_not_enough_credit_means_no_search_and_no_charge(env, live):
    c = member(env, balance_usd=0.01)
    r = run(c)
    assert r.status_code == 402 and r.json()["needed_usd"] > 0.01 and r.json()["balance_usd"] == 0.01
    assert live.searches == [] and balance(env, c.uid) == 10_000
    with env.scope() as db:
        assert db.scalar(select(func.count(UserSearch.id))) == 0 and db.scalar(select(func.count(CreditEntry.id))) == 0


def test_repeating_a_search_is_free_and_served_from_the_database(env, live):
    a = member(env, "a@example.com", balance_usd=5)
    first = run(a, "Hiring  Copywriter").json()
    bal = balance(env, a.uid)
    b = member(env, "b@example.com", balance_usd=0)                     # a different customer with NO credit
    r = run(b, "hiring copywriter")                                     # same search, different case/spacing
    assert r.status_code == 202 and r.json()["cached"] is True and r.json()["status"] == "done" and r.json()["cost_usd"] == 0
    assert len(live.searches) == 1 and balance(env, a.uid) == bal and balance(env, b.uid) == 0
    ids_a = {j["id"] for j in a.get(f"/searches/{first['id']}/jobs").json()}
    assert {j["id"] for j in b.get(f"/searches/{r.json()['id']}/jobs").json()} == ids_a and len(ids_a) == 3
    q = b.post("/searches/estimate", json={"query": "HIRING COPYWRITER", "freshness": "w"}, headers=CSRF).json()
    assert q["cached"] is True and q["price_usd"] == 0 and q["cached_results"] == 3


def test_different_options_are_different_searches_and_the_cache_expires(env, live, monkeypatch):
    c = member(env, balance_usd=5)
    run(c); run(c, freshness="d"); run(c, site="reddit.com")
    assert [q for q, _, _ in live.searches] == ["hiring copywriter", "hiring copywriter", "hiring copywriter site:reddit.com"]
    assert [f for _, _, f in live.searches] == ["w", "d", "w"]
    with env.scope() as db:
        db.query(UserSearch).update({UserSearch.finished_at: utcnow() - timedelta(hours=settings.search_cache_hours + 1)})
    run(c)
    assert len(live.searches) == 4


def test_daily_limit_counts_new_searches_only(env, live, monkeypatch):
    monkeypatch.setattr(settings, "user_searches_per_day", 2)
    c = member(env, balance_usd=5)
    assert run(c, "one one").status_code == 202 and run(c, "two two").status_code == 202
    assert run(c, "three three").status_code == 429
    assert run(c, "one one").json()["cached"] is True                   # cached repeats are still allowed
    assert c.get("/searches/config").json()["left_today"] == 0


def test_only_one_search_at_a_time_per_user_and_stale_ones_expire(env, live):
    c = member(env, balance_usd=5)
    with env.scope() as db:
        db.add(UserSearch(user_id=c.uid, query="stuck", query_key="stuck|w|", status="running"))
    assert run(c, "another one").status_code == 409
    with env.scope() as db:
        db.query(UserSearch).update({UserSearch.created_at: utcnow() - timedelta(minutes=30)})
    assert run(c, "another one").status_code == 202
    with env.scope() as db:
        stuck = db.scalar(select(UserSearch).where(UserSearch.query == "stuck"))
        assert stuck.status == "failed" and "not charged" in stuck.error


def test_site_wide_concurrency_and_monthly_budget_are_enforced(env, live, monkeypatch):
    c = member(env, balance_usd=5)
    monkeypatch.setattr(settings, "user_search_max_concurrent", 1)
    other = member(env, "busy@example.com")
    with env.scope() as db:
        db.add(UserSearch(user_id=other.uid, query="busy", query_key="busy|w|", status="running"))
    r = run(c)
    assert r.status_code == 503 and "busy" in r.json()["detail"].lower()
    with env.scope() as db:
        db.query(UserSearch).delete()
    monkeypatch.setattr(settings, "serpapi_monthly_budget", 0)
    r = run(c)
    assert r.status_code == 503 and "paused" in r.json()["detail"]
    assert live.searches == []


def test_a_failed_search_is_free_and_never_leaks_the_key(env, live):
    c = member(env, balance_usd=5)
    start = balance(env, c.uid)
    live.fail = SearchError("serpapi HTTP 401")
    sid = run(c).json()["id"]
    s = c.get(f"/searches/{sid}").json()
    assert s["status"] == "failed" and "not charged" in s["error"] and s["cost_usd"] == 0
    assert balance(env, c.uid) == start
    with env.scope() as db:
        assert db.scalar(select(func.count(CreditEntry.id))) == 0
    live.fail = None
    assert run(c).status_code == 202                                     # a failure does not use up the daily allowance
    assert c.get("/searches/config").json()["left_today"] == settings.user_searches_per_day - 1


def test_an_unexpected_crash_still_ends_in_failed_not_running(env, live, monkeypatch):
    c = member(env, balance_usd=5)
    def boom(*a, **k):
        raise RuntimeError("database exploded")
    monkeypatch.setattr(live_search.pipeline, "run_pipeline_for_query", boom)
    sid = run(c).json()["id"]
    s = c.get(f"/searches/{sid}").json()
    assert s["status"] == "failed" and "not charged" in s["error"] and balance(env, c.uid) == 5_000_000


def test_running_the_same_search_twice_charges_once(env, live):
    c = member(env, balance_usd=5)
    sid = run(c).json()["id"]
    bal = balance(env, c.uid)
    live_search.run_search(sid)                                          # a retried task: the search is no longer 'queued'
    assert balance(env, c.uid) == bal and len(live.searches) == 1
    with env.scope() as db:
        s = db.get(UserSearch, sid); s.status = "queued"                  # even if forced to re-run, the ledger ref stops a double charge
    live_search.run_search(sid)
    assert balance(env, c.uid) == bal


def test_customers_only_see_their_own_searches(env, live):
    a, b = member(env, "a@example.com", 5), member(env, "b@example.com", 5)
    sid = run(a).json()["id"]
    assert b.get(f"/searches/{sid}").status_code == 404 and b.get(f"/searches/{sid}/jobs").status_code == 404
    assert b.get("/searches/999999").status_code == 404                  # same answer as 'someone else's'
    assert [x["id"] for x in a.get("/searches").json()] == [sid] and b.get("/searches").json() == []
    anon = TestClient(app)
    assert anon.get(f"/searches/{sid}").status_code == 401 and anon.post("/searches", json={"query": "x y z"}, headers=CSRF).status_code == 401


def test_search_writes_need_the_csrf_header(env, live):
    c = member(env, balance_usd=5)
    assert c.post("/searches", json={"query": "hiring copywriter", "freshness": "w"}).status_code == 403
    assert c.post("/searches/estimate", json={"query": "hiring copywriter", "freshness": "w"}).status_code == 403


@pytest.mark.parametrize("body", [
    {"query": "ab"}, {"query": "x" * 121}, {"query": "bad\x00query here"}, {"query": "line\nbreak search"},
    {"query": "hiring copywriter", "freshness": "forever"}, {"query": "hiring copywriter", "site": "evil.example"},
    {"query": "hiring copywriter", "site": "reddit.com; drop table"}, {"query": "y" * 400},
])
def test_search_input_is_validated(env, live, body):
    c = member(env, balance_usd=5)
    assert c.post("/searches", json={"freshness": "w", **body}, headers=CSRF).status_code == 422
    assert live.searches == []


def test_estimate_reports_affordability_and_never_runs_anything(env, live):
    c = member(env, balance_usd=0.001)
    q = c.post("/searches/estimate", json={"query": "hiring copywriter", "freshness": "w"}, headers=CSRF).json()
    assert q["ready"] is True and q["cached"] is False and q["affordable"] is False
    assert q["price_usd"] == live_search.estimated_user_price_usd() and 0.02 < q["price_usd"] < 0.06
    assert live.searches == []


def test_cached_results_never_leak_other_peoples_balance_or_identity(env, live):
    a = member(env, "a@example.com", 5)
    run(a)
    b = member(env, "b@example.com", 0)
    body = b.get(f"/searches/{run(b).json()['id']}").text
    assert "a@example.com" not in body and "user_id" not in body


# =============== the 1.5x guarantee ===============
@pytest.mark.parametrize("usage,results", [((2000, 400), 3), ((2600, 900), 10), ((1, 1), 1), ((120_000, 3000), 2), ((5000, 2048), 7)])
def test_every_search_is_charged_at_least_1_5x_what_it_really_cost_us_and_not_a_cent_more(env, live, usage, results):
    from app import pricing
    live.usage, live.n = usage, results
    c = member(env, balance_usd=5)
    sid = run(c, f"margin check {usage[0]}").json()["id"]
    with env.scope() as db:
        e = db.scalar(select(CreditEntry).where(CreditEntry.ref == f"usersearch:{sid}"))
        meta = e.meta
        charged, our_cost = -e.delta_micro, meta["our_cost_micro"]
    # independent recomputation of our real cost from the raw rates (not through the code under test)
    r_in, r_out = (0.50, 2.50) if usage[0] > 100_000 else (0.10, 0.50)
    real_extraction = results * (usage[0] * r_in + usage[1] * r_out)
    real_cost = settings.serpapi_cost_per_search_usd * 1_000_000 + real_extraction
    assert our_cost >= real_cost - 1e-6 and our_cost - real_cost < 3          # we record what we really spent (rounded up)
    assert charged >= 1.5 * real_cost - 1e-6                                  # the customer pays at least 1.5x
    assert charged <= 1.5 * real_cost + 5                                     # ...and only rounding on top: never an accidental surcharge
    assert meta["charged_micro"] == charged


def test_tokens_from_pages_the_model_read_but_we_could_not_use_are_charged_too(env, live, monkeypatch):
    live.n = 3
    calls = {"n": 0}
    def extract(r, h):
        calls["n"] += 1
        if calls["n"] == 2:                                              # one page: model refused, but we paid for the tokens
            raise llm_extractor.ExtractionError("model declined this page", {"model": MODEL, "input_tokens": 3000, "output_tokens": 50})
        return ExtractedJob(is_real_job=True, title=r.title, usage={"model": MODEL, "input_tokens": 2000, "output_tokens": 400})
    monkeypatch.setattr(llm_extractor, "extract", extract)
    c = member(env, balance_usd=5)
    sid = run(c, "partial failure").json()["id"]
    with env.scope() as db:
        meta = db.scalar(select(CreditEntry).where(CreditEntry.ref == f"usersearch:{sid}")).meta
    assert meta["tokens_in"] == 2000 + 3000 + 2000 and meta["tokens_out"] == 400 + 50 + 400


def test_search_refuses_to_run_when_the_model_has_no_price(env, live, monkeypatch):
    monkeypatch.setattr(settings, "extraction_model", "claude-some-future-model")
    c = member(env, balance_usd=5)
    r = run(c)
    assert r.status_code == 503 and "no price" in r.json()["detail"]
    assert live.searches == []


def test_the_scheduled_google_plan_is_not_started_by_adding_keys(env, live):
    from app import scheduler
    assert settings.google_schedule_enabled is False and scheduler.google_layer_ready()[0] is False


# =============== buy-credits link + margin display ===============
def test_buy_url_is_shown_only_when_configured_and_https(env, monkeypatch):
    c = member(env)
    assert c.get("/account/credits").json()["buy_url"] is None
    monkeypatch.setattr(settings, "whop_checkout_url", "https://whop.com/checkout/abc")
    assert c.get("/account/credits").json()["buy_url"] == "https://whop.com/checkout/abc"
    for bad in ("http://whop.com/x", "javascript:alert(1)", "//evil.example", "whop.com/x"):
        monkeypatch.setattr(settings, "whop_checkout_url", bad)
        assert c.get("/account/credits").json()["buy_url"] is None, bad


def test_admin_overview_reports_the_real_margin_on_customer_searches(env, live):
    from authkit import login, make_user, CSRF as _CSRF
    c = member(env, balance_usd=5)
    run(c, "margin one"); run(c, "margin two")
    with env.scope() as db:
        _, secret = make_user(db, "christian@emailsandsms.com", admin=True)
    admin_client = TestClient(app)
    assert login(env, admin_client, "christian@emailsandsms.com", secret=secret).status_code == 200
    o = admin_client.get("/admin/overview").json()["customer_searches_30d"]
    assert o["count"] == 2 and o["charged_usd"] > o["our_cost_usd"] > 0
    assert 1.5 <= o["margin_x"] <= 1.51                                        # exactly the markup, plus rounding only
