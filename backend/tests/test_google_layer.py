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
    assert 'site:linkedin.com/jobs/view "funnel builder" jobs' in texts
    assert 'site:linkedin.com/posts "hiring" "funnel builder"' in texts
    assert "site:himalayas.app/companies copywriter jobs" in texts
    assert "site:remotive.com/remote/jobs/ creative strategist jobs" in texts
    assert not any(
        host in t
        for t in texts
        for host in ("tryremotely.com", "creatorjobboard.com", "jobspresso.co", "remote.co")
    )
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
    for platform in ("twitter", "linkedin", "facebook", "himalayas", "weworkremotely", "euremotejobs"):
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


def test_a_provider_that_is_out_of_searches_is_not_asked_again(monkeypatch):
    monkeypatch.setattr(settings, "dev_fixtures", False)
    monkeypatch.setattr(settings, "serpapi_api_key", "k")
    monkeypatch.setattr(settings, "serper_api_key", "s")
    monkeypatch.setattr(settings, "google_search_provider", "serpapi")
    gets = []

    def fake_get(url, **kw):
        gets.append(1)
        return httpx.Response(429, json={"error": "Your account has run out of searches."}, request=httpx.Request("GET", "https://serpapi.com/search.json"))

    def fake_post(url, **kw):
        return httpx.Response(200, json={"organic": [
            {"link": "https://example.org/job/1", "title": "Hiring a copywriter", "snippet": "remote"},
        ]}, request=httpx.Request("POST", "https://google.serper.dev/search"))

    monkeypatch.setattr(google_search.httpx, "get", fake_get)
    monkeypatch.setattr(google_search.httpx, "post", fake_post)
    exhausted: set[str] = set()
    used: list[str] = []
    google_search.search("hiring copywriter", exhausted=exhausted, used=used)
    assert exhausted == {"serpapi"} and used == ["serper"] and len(gets) == 1
    used.clear()
    google_search.search("hiring email copywriter", exhausted=exhausted, used=used)
    assert used == ["serper"] and len(gets) == 1


def test_advice_self_promo_and_careers_indexes_are_not_hiring_posts():
    assert pipeline.not_a_hiring_post("Jim Schroën's Post", "When I'm hiring a creative strategist, I look for three things.")
    assert pipeline.not_a_hiring_post("[FOR HIRE] Senior Creative Strategist", "I can help your brand")
    assert pipeline.not_a_hiring_post("[Hire Me] Copywriter", "")
    assert pipeline.not_a_hiring_post("Careers at Liberty Mutual Insurance", "See open roles")
    assert pipeline.not_a_hiring_post("All Open Positions", "")
    assert pipeline.not_a_hiring_post(
        "Copywriter Jobs Board",
        "240 writer jobs, submission calls; 129 jobs exclusive to paid subscribers on the site!")
    assert pipeline.not_a_hiring_post(
        "Creative Strategist Job Board",
        "Discover exciting creative strategist job opportunities.",
        url="https://www.foreplay.co/creative-strategist-jobs")
    assert not pipeline.not_a_hiring_post(
        "We're hiring a creative strategist",
        "Post the role on our job board and send a portfolio.")
    assert pipeline.not_a_hiring_post(
        "Cost to build funnel for course by top builder?",
        "How much would be to build funnel for course by top funnel builder? I checked Fiverr already. Cheers,")
    assert not pipeline.not_a_hiring_post(
        "We're hiring a funnel builder",
        "Cost to build the pages is covered. Send a Fiverr profile if you have one.")
    assert pipeline.not_a_hiring_post(
        "Marketing Copywriter - BIS Safety Software",
        "This job post is closed and the position is probably filled. Please do not apply.")
    assert pipeline.not_a_hiring_post(
        "Creative Strategist | City of New York Jobs",
        "This vacancy has now expired. Minimum Qualifications.",
        url="https://cityjobs.nyc.gov/job/creative-strategist-in-manhattan-jid-32088")
    assert not pipeline.not_a_hiring_post(
        "Creative Strategist | City of New York Jobs",
        "The Creative Strategist will develop creative ideas for the agency.",
        url="https://cityjobs.nyc.gov/job/creative-strategist-in-manhattan-jid-32088")
    assert not pipeline.not_a_hiring_post(
        "We're hiring a copywriter",
        "Please do not apply unless you have a portfolio of live emails.")
    assert pipeline.not_a_hiring_post(
        "Remote Performance Marketing Specialist | E-Commerce",
        "Think project manager meets creative strategist, all while being knee-deep in data.",
        query='hiring "creative strategist"')
    assert pipeline.not_a_hiring_post(
        "Remote Account Manager at SOCi",
        "Creative strategist; develop new ways to drive sales.",
        query='hiring "creative strategist"')
    assert pipeline.not_a_hiring_post(
        "Email Graphics Designer at C9 - Philippines Remote",
        "work with the email copywriter and creative team to conceptualize graphic assets",
        query='hiring "email copywriter"')
    assert not pipeline.not_a_hiring_post(
        "Lead Creative Strategist (Performance Marketing)",
        "Superside is hiring a Creative Strategist",
        query='hiring "creative strategist"')
    assert not pipeline.not_a_hiring_post(
        "Remote Freelance Writer at ProWriterSites",
        "our ultimate goal is to bring on a full-time copywriter",
        query="hiring copywriter")
    assert not pipeline.not_a_hiring_post(
        "Email Copywriter, Account Coordinator – Dutch-speaking",
        "Full Time",
        query='hiring "email copywriter"')
    assert not pipeline.not_a_hiring_post(
        "We're hiring a copywriter",
        "Send a portfolio. We also post roles on our jobs board.")
    assert pipeline.not_a_hiring_post(
        "Remote-First Marketing Jobs",
        "Copywriter; Creative Strategist; Organic Marketing.")
    assert pipeline.not_a_hiring_post(
        "9000+ Copywriter jobs in United States",
        "Today's top 9000+ Copywriter jobs in United States. Leverage your professional network, and get hired.")
    assert pipeline.not_a_hiring_post(
        "92 Copywriter Jobs in Charleston, South Carolina",
        "Senior Copywriter. Growth Marketing & Content Manager.")
    assert pipeline.not_a_hiring_post(
        "10000+ Funnel Builder jobs in United States",
        "New Funnel Builder jobs added daily.")
    assert not pipeline.not_a_hiring_post(
        "Funnel Builder",
        "We are looking for a highly strategic Funnel Builder to join our marketing team. 9000+ brands use the tool.")
    assert pipeline.not_a_hiring_post(
        "$20-$72/hr Funnel Jobs (NOW HIRING) Oct 2026",
        "Overview We're looking for a high-level funnel builder who understands both conversion and design.",
        url="https://www.ziprecruiter.com/Jobs/Funnel")
    assert pipeline.not_a_hiring_post(
        "Email Marketing Jobs in New Jersey (NOW HIRING)",
        "Email Marketer/Copywriter. Our Marketing Department is looking to add a full-time Email Marketer.",
        url="https://www.ziprecruiter.com/Jobs/Email-Marketing/--in-New-Jersey")
    assert not pipeline.not_a_hiring_post(
        "Email Marketer",
        "Our Marketing Department is looking to add a full-time Email Marketer. Now hiring.",
        url="https://www.ziprecruiter.com/c/acme/Job/Email-Marketer/-in-Iselin,NJ")
    assert pipeline.not_a_hiring_post(
        "Remote Copywriter Jobs ($69K to $103K)",
        "Search 216 copywriter remote jobs. 26 added this week.",
        url="https://www.remoterocketship.com/jobs/copywriter/")
    assert pipeline.not_a_hiring_post(
        "$26-$54/hr No Experience Copywriting Jobs in Royal Oak, MI",
        "Browse 16 ROYAL OAK, MI NO EXPERIENCE COPYWRITING jobs hiring now.",
        url="https://www.ziprecruiter.com/Jobs/No-Experience-Copywriting/-in-Royal-Oak,MI")
    assert not pipeline.not_a_hiring_post(
        "Copywriter",
        "We are hiring a copywriter in Royal Oak. Pay is $26-$54/hr.",
        url="https://www.ziprecruiter.com/c/acme/Job/Copywriter/-in-Royal-Oak,MI")
    assert pipeline.not_a_hiring_post(
        "Copywriter Jobs in Miami (492 Open Roles)",
        "492 Copywriter Jobs in Miami. Digital Copywriter. Junior Copywriter. Lead, Copywriter.")
    assert pipeline.not_a_hiring_post(
        "Creative Strategist Jobs in Manhattan, NY (3403)",
        "There are 3,403 Creative Strategist opportunities available in Manhattan, NY.",
        url="https://www.joblist.com/j/creative-strategist-jobs/manhattan-ny")
    assert not pipeline.not_a_hiring_post(
        "Funnel Builder",
        "We are looking for a Funnel Builder. 3,403 brands use this stack.",
        url="https://www.multiplymii.com/job-description/funnel-builder")
    assert pipeline.not_a_hiring_post(
        "Creative strategist jobs",
        "Creative Strategist. Reach Digital. Creative Strategist. Ecom Warrior Academy.")
    assert pipeline.not_a_hiring_post(
        "Remote Copywriting Jobs",
        "Explore the fully remote Copywriting jobs worldwide. Apply to positions like Copywriter and Content Writer.",
        url="https://www.workingnomads.com/remote-copywriting-jobs")
    assert not pipeline.not_a_hiring_post(
        "Copywriter at a remote studio",
        "We are hiring a copywriter. This listing is also on the remote copywriting jobs page.",
        url="https://www.workingnomads.com/job/go/copywriter")
    assert pipeline.not_a_hiring_post(
        "Email Copywriter Remote Jobs",
        "Find Email Copywriter Remote Jobs that allow telecommuting, part-time, or freelance contracts.",
        url="https://www.virtualvocations.com/q-email-copywriter-remote-jobs.html")
    assert not pipeline.not_a_hiring_post(
        "Email Copywriter",
        "As the email copywriter, you help grow the clients. This role is remote.",
        url="https://jobs.smartrecruiters.com/InstantEmailCopy/743999692475726-email-copywriter")
    assert pipeline.not_a_hiring_post(
        "Freelance and remote Copywriters jobs - Twine",
        "Browse the latest freelance and remote Copywriters jobs, newest first.",
        url="https://www.twine.net/jobs/copywriters")
    assert not pipeline.not_a_hiring_post(
        "Freelance copywriter",
        "We are hiring a freelance and remote copywriter. Browse the latest work in the portfolio.",
        url="https://www.twine.net/jobs/copywriters/123")
    assert pipeline.not_a_hiring_post(
        "Creative Strategist Jobs in Atlanta Metropolitan Area ...",
        "1,000+ Creative Strategist Jobs in Atlanta Metropolitan Area. Senior Manager, Creative Strategy.",
        url="https://www.linkedin.com/jobs/creative-strategist-jobs-atlanta-metropolitan-area")
    assert pipeline.not_a_hiring_post(
        "Creative Strategist jobs in New York",
        "Discover the latest Creative Strategist jobs at the fastest growing startups in New York.",
        url="https://startup.jobs/locations/new-york/creative-strategist")
    assert pipeline.not_a_hiring_post(
        "Viral Marketing Copywriter Jobs in Greenville NC",
        "Copywriter Jobs in Greenville NC, Jobs in Film, TV, Music, VFX and more.",
        url="https://www.entertainmentcareers.net/jobs/s/viral-marketing-copywriter/545-greenville-nc/")
    assert not pipeline.not_a_hiring_post(
        "Viral Marketing Copywriter",
        "As the Copywriter, you'll bring a strong verbal and narrative lens. Full time in Greenville, NC.",
        url="https://www.entertainmentcareers.net/mrbeast/viral-marketing-copywriter/job/528861/")
    assert not pipeline.not_a_hiring_post(
        "Creative Strategist",
        "We are hiring a Creative Strategist in New York. 1,000+ brands in the category.",
        url="https://www.linkedin.com/jobs/view/creative-strategist-123")
    assert pipeline.not_a_hiring_post(
        "Copywriter Jobs in October 2026",
        "Find a Copywriter job at a fast-growing startup.",
        url="https://startup.jobs/roles/copywriter")
    assert pipeline.not_a_hiring_post(
        "Copywriting Jobs (October 2026)",
        "313 Copywriting jobs available at Startup Jobs.",
        url="https://startup.jobs/copywriting-jobs")
    assert not pipeline.not_a_hiring_post(
        "Funnel Builder",
        "We are looking for a Funnel Builder to join in October 2026.",
        url="https://www.multiplymii.com/job-description/funnel-builder")
    assert not pipeline.not_a_hiring_post(
        "Creative Strategist at Reach Digital",
        "492 people on the marketing team. Open roles are listed separately.")
    assert pipeline.not_a_hiring_post(
        "Email copywriting jobs",
        "Email Copywriter. Instant Email Copy. $45,000 - $70,000 a year.",
        url="https://www.indeed.com/q-email-copywriting-jobs.html")
    assert pipeline.not_a_hiring_post(
        "Top 38 Email copywriter Jobs in Remote",
        "38 Email copywriter jobs available in Remote. See salaries, compare reviews, easily apply.",
        url="https://www.simplyhired.com/search?q=email+copywriter&l=remote")
    assert pipeline.not_a_hiring_post(
        "Top Copywriter Jobs in NYC, NY",
        "Search for the best NYC copywriter jobs from top companies. Jobs updated daily.",
        url="https://www.builtinnyc.com/jobs/content/search/copywriter")
    assert not pipeline.not_a_hiring_post(
        "Copywriter",
        "We are hiring a copywriter. Search for the best work in your portfolio.",
        url="https://example.com/jobs/copywriter")
    assert pipeline.not_a_hiring_post(
        "Creative Strategy Jobs – Reduced Hours (2026)",
        "Hiring companies include Canva, Power Digital, and Suno.",
        url="https://4dayweek.io/creative-strategy-jobs")
    assert not pipeline.not_a_hiring_post(
        "Creative Strategist",
        "Rocket Money is hiring a Creative Strategist. Reduced hours, four-day week.",
        url="https://4dayweek.io/job/creative-strategist-rocket-money")
    assert pipeline.not_a_hiring_post(
        "Remote copywriter jobs in Davidson, NC",
        "Browse 20 Remote Copywriter job openings from Davidson, NC.",
        url="https://www.indeed.com/q-remote-copywriter-l-davidson,-nc-jobs.html")
    assert not pipeline.not_a_hiring_post(
        "Email Copywriter",
        "Work within Klaviyo. $45,000 - $70,000 a year.",
        url="https://www.indeed.com/viewjob?jk=abc123")
    assert not pipeline.not_a_hiring_post(
        "Remote Copywriter at YO IT Consulting",
        "5+ years of professional experience in copywriting. Salary $69K to $103K.",
        url="https://remoteok.com/remote-jobs/copywriter-yo")
    assert not pipeline.not_a_hiring_post(
        "Funnel Builder",
        "We are looking for a high-level funnel builder. Jobs (now hiring) are listed on our site.",
        url="https://www.multiplymii.com/job-description/funnel-builder")
    assert pipeline.not_a_hiring_post(
        "Recruitment Landing Page Templates for High Conversions",
        "Lunar is an AI landing page generator that turns your brief into a complete page.")
    assert pipeline.not_a_hiring_post(
        "Hiring Landing Page",
        "Discover 26 Hiring Landing Page designs on Dribbble.")
    assert pipeline.not_a_hiring_post(
        "Hiring landing page Images - Free Download",
        "Free Graphic Resources for Hiring landing page Vectors, Stock Photos & PSD files. Free for commercial use.")
    assert pipeline.not_a_hiring_post(
        "Landing Pages | Hiring on LinkedIn",
        "Learn how to build a Landing Page and attract talent.")
    assert pipeline.not_a_hiring_post(
        "Recruitment Landing Page Examples: What Works + Best",
        "Recruitment landing page best practices. Match your ad to your headline.")
    assert not pipeline.not_a_hiring_post(
        "We're hiring a landing page designer",
        "You'll write landing page templates for clients and learn how to build a funnel.")
    assert pipeline.not_a_hiring_post(
        "Best Freelance Email Marketers for Hire (Oct 2026)",
        "How much does hiring an email marketer cost?",
        url="https://www.upwork.com/hire/email-marketing-consultants/")
    assert pipeline.not_a_hiring_post(
        "Hire the Best funnel builders",
        "Freelancers typically offer planning and setup.",
        url="https://pro.fiverr.com/cat/marketing-and-sales/funnel-builder")
    assert pipeline.not_a_hiring_post(
        "Sales funnel builder Jobs, Employment",
        "Search for jobs related to Sales funnel builder or hire on the world's largest freelancing marketplace.",
        url="https://www.freelancer.com/job-search/sales-funnel-builder/")
    assert pipeline.not_a_hiring_post(
        "Copywriter Jobs",
        "Search and apply to our open Copywriter jobs.",
        url="https://www.roberthalf.com/us/en/jobs/all/copywriter")
    assert not pipeline.not_a_hiring_post(
        "Funnel Builder",
        "We are looking for a Funnel Builder to join our marketing team.",
        url="https://www.multiplymii.com/job-description/funnel-builder")
    assert not pipeline.not_a_hiring_post(
        "Email marketer for an ecommerce brand",
        "Fixed price, ongoing.",
        url="https://www.upwork.com/jobs/~0123456789")
    assert pipeline.not_a_hiring_post(
        "Where can I hire an email marketing manager?",
        "",
        url="https://www.quora.com/Where-can-I-hire-an-email-marketing-manager")
    assert pipeline.not_a_hiring_post(
        "What are the best email marketing jobs in the USA for 2025?",
        "",
        url="https://www.quora.com/What-are-the-best-email-marketing-jobs-in-the-USA-for-2025")
    assert not pipeline.not_a_hiring_post(
        "We're hiring an email marketer",
        "Where can I hire one? Apply on the careers page.",
        url="https://example.com/jobs/email-marketer")
    assert pipeline.not_a_hiring_post(
        "Copywriter Hiring • 500+ reels on Instagram",
        "Today, we're spotlighting one of the hottest jobs on Creatively.",
        url="https://www.instagram.com/popular/copywriter-hiring/")
    assert not pipeline.not_a_hiring_post(
        "We're hiring a copywriter",
        "500+ reels on Instagram taught us the brief. Apply if you write ads.",
        url="https://www.instagram.com/p/AbC123/")
    assert pipeline.not_a_hiring_post(
        "Page 17 - Free and customizable copywriting templates",
        "Black and Yellow Simple We",
        url="https://www.canva.com/templates/s/copywriting/?continuation=800")
    assert not pipeline.not_a_hiring_post(
        "We're hiring a copywriter",
        "You'll make customizable templates for the brand.",
        url="https://example.com/jobs/copywriter")
    assert pipeline.not_a_hiring_post(
        "Black Dark We Are Hiring Copywriter",
        "The purpose of creating a Black Dark We Are Hiring Copywriter is to attract candidates.",
        url="https://venngage.com/templates/posters/black-dark-we-are-hiring-copywriter-6cc4b9b3-0d23-46a1-b4ec-999e37afa014")
    assert not pipeline.not_a_hiring_post(
        "We're hiring a copywriter",
        "Use a Venngage template for the hiring poster.",
        url="https://example.com/jobs/copywriter-poster")
    assert pipeline.not_a_hiring_post(
        "Hiring: Copywriter Near Me (ID:1608474)",
        "Join a top company hiring Copywriter Near Me. Apply now via Vita CV!",
        url="https://www.vitacv.com/jobs/copywriter-job-near-me/1608474")
    assert not pipeline.not_a_hiring_post(
        "We're hiring a copywriter near me",
        "Apply if you are local.",
        url="https://example.com/jobs/copywriter-local")
    assert pipeline.not_a_hiring_post(
        "Email Marketer Job Description Template",
        "As an email marketer, you will develop, implement and optimise email marketing strategies for our company.",
        url="https://business.linkedin.com/hire/resources/how-to-hire-guides/email-marketer-job-description")
    assert pipeline.not_a_hiring_post(
        "Strategy to Hire a Skilled Email Marketer | Equip",
        "Includes what to look for while hiring an email marketer, skills to test, and sample questions.",
        url="https://equip.co/resources/how-to-hire-email-marketer-mugie/")
    assert not pipeline.not_a_hiring_post(
        "Email Marketer",
        "Strategy to hire a skilled email marketer is in the brief. We are hiring an email marketer.",
        url="https://jobs.example.com/email-marketer")
    assert not pipeline.not_a_hiring_post(
        "Funnel Builder",
        "We are looking for a Funnel Builder to join our marketing team. See the job description template in the doc.",
        url="https://www.multiplymii.com/job-description/funnel-builder")
    assert pipeline.not_a_hiring_post(
        "Audience & Connections Strategist at Wpromote",
        "Creative Strategist. Rocket Money. Browse Marketing jobs.")
    assert not pipeline.not_a_hiring_post(
        "Paid Social Creative Strategist at Suno",
        "Proven experience as a creative strategist in a high-volume environment.")
    assert not pipeline.not_a_hiring_post(
        "Senior Creative Strategist at Polymarket",
        "Marketing Jobs/Senior Creative Strategist. Posted 2 months ago.")
    assert not pipeline.not_a_hiring_post("We're hiring a copywriter", "1-3 years into their career")
    assert not pipeline.not_a_hiring_post("[HIRING] Creative Strategist (Fully Remote)", "full-time")
    assert pipeline.not_a_hiring_post("How to hire a creative strategist: 3 signs and 3 skills", "")
    assert pipeline.not_a_hiring_post("How should brands hire creative strategists?!", "")
    assert pipeline.not_a_hiring_post("3 signs you're not actually a creative strategist", "")
    assert pipeline.not_a_hiring_post("Ultimate checklist for hiring a copywriting genius", "Secret checklist for a pro copywriter")
    assert pipeline.not_a_hiring_post("How to become a creative strategist as a copywriter", "More brands are hiring creative strategists")
    assert pipeline.not_a_hiring_post("Here's how to get hired as a copywriter at storyarb.", "Our acceptance rate is less than 0.5%.")
    assert pipeline.not_a_hiring_post(
        "How can I get hired in Agora as a financial copywriter?",
        "Their programs are offered periodically and totally depends on what division needs copywriters.")
    assert pipeline.not_a_hiring_post(
        "Seeking Advice: Funnel Design vs. Funnel Builder – What's the Best Approach",
        "Hi everyone, I'm in the early stages of building my business.")
    assert pipeline.not_a_hiring_post(
        "What makes a great creative strategist? | Sarim Siddiqui",
        "Media Buyer & Creative Strategist. I help brands reduce new customer cost.")
    assert pipeline.not_a_hiring_post(
        "The Creative Strategist role is the fast growing job in performance marketing",
        "But it's a new role so people always ask: what's the right background?")
    assert pipeline.not_a_hiring_post(
        "The Best Funnel Builder for Recruiting in 2026",
        "Streamline your candidate acquisition with Heyflow's funnel builder.")
    assert pipeline.not_a_hiring_post(
        "Best Copywriter Jobs 2026",
        "Search for the best copywriter jobs",
        url="https://builtin.com/jobs/content/search/copywriter")
    assert not pipeline.not_a_hiring_post(
        "Copywriter",
        "We are hiring a copywriter. Search for the best copywriter jobs is how candidates find us.",
        url="https://builtin.com/job/copywriter/senior-copywriter")
    assert pipeline.not_a_hiring_post(
        "9 Best Recruiting Landing Page and Hiring Event Tools ...",
        "A recruiting landing page is a standalone, single-purpose page built for one campaign.",
        query='hiring "landing page"',
        url="https://www.joveo.com/blog/recruiting-landing-page-builders/")
    assert pipeline.not_a_hiring_post(
        "8 Mistakes DTC Brands Make When Hiring a Creative Strategist",
        "Over-indexing on beautiful portfolios is one of the most common mistakes hiring creative strategist candidates.",
        url="https://www.constanthire.com/posts/8-mistakes-dtc-brands-make-when-hiring-a-creative-strategist")
    assert not pipeline.not_a_hiring_post(
        "We're hiring a creative strategist",
        "8 mistakes DTC brands make when hiring a creative strategist are listed in the brief.",
        url="https://careers.example.com/creative-strategist")
    assert pipeline.not_a_hiring_post(
        "Hiring Email Marketer: Scope the Role Properly",
        "eCommerce Recruiting Agency | Global",
        url="https://www.linkedin.com/posts/katarina-perisic_hiring-an-email-marketer-for-your-ecom-brand-activity-7462120573469577216-d36G")
    assert not pipeline.not_a_hiring_post(
        "Email Marketer",
        "Scope the role properly with the hiring manager. We are hiring an email marketer.",
        url="https://jobs.example.com/email-marketer")
    assert not pipeline.not_a_hiring_post(
        "Landing page builder",
        "We are hiring someone to build landing pages. We compared 9 best tools first.",
        query='hiring "landing page"')
    assert pipeline.not_a_hiring_post(
        "Best Practices for Virtual Hiring Event Landing Pages",
        "The most crucial details on your landing page should include the event's time, date, and location.",
        query='hiring "landing page"',
        url="https://www.b2match.com/blog/best-practices-for-virtual-hiring-event-landing-pages")
    assert not pipeline.not_a_hiring_post(
        "Landing page builder",
        "We are hiring someone to build the hiring event landing page. Follow our best practices.",
        query='hiring "landing page"')
    assert pipeline.not_a_hiring_post(
        "Hire a GoHighLevel Funnel Builder Expert: Easy Guide",
        "A GoHighLevel funnel builder expert helps design and structure your sales process.")
    assert pipeline.not_a_hiring_post(
        "Connor Gross - PSA to anyone hiring a creative strategist.",
        "We hired 14 this year. And the best always have the same 2 things in common.")
    assert pipeline.not_a_hiring_post(
        "How to make a junior creative strategist quit within 10 days",
        "Day 1: Give them no onboarding.")
    assert pipeline.not_a_hiring_post(
        "Recruitment Landing Page Examples: What Works + Best",
        "A recruitment landing page solves this. It's a standalone page built for one goal.")
    assert pipeline.not_a_hiring_post(
        "What does a Creative Strategist do?",
        "This page details key information about the Creative Strategist role, salaries, and how to approach recruitment.")
    assert pipeline.not_a_hiring_post(
        "How to get into Creative Strategist Job?",
        "I've been applying to multiple companies via LinkedIn but never get any interview.",
        url="https://www.reddit.com/r/CreativeStrategists/comments/abc/how_to_get_into/")
    assert pipeline.not_a_hiring_post(
        "How I Got a Remote Job from LinkedIn — You Don't Need Followers",
        'Open the LinkedIn search bar and type "hiring email marketer".',
        url="https://ritamichaeladegoke.com.ng/how-to-get-remote-jobs-on-linkedin-without-followers-and-connections/")
    assert not pipeline.not_a_hiring_post(
        "We're hiring an email marketer",
        "How I got a remote job is a story we tell candidates.",
        url="https://example.com/jobs/email-marketer")
    assert pipeline.not_a_hiring_post(
        "Is being an Email Copywriter still in demand in 2025?",
        "I'm planning to learn more about email copywriting as a beginner in the freelancer world.",
        url="https://www.reddit.com/r/copywriting/comments/1l3zo0n/is_being_an_email_copywriter_still_in_demand_in/")
    assert not pipeline.not_a_hiring_post(
        "Email Copywriter",
        "Email copywriting is still in demand. We are hiring an email copywriter.",
        url="https://jobs.smartrecruiters.com/InstantEmailCopy/743999692475726-email-copywriter")
    assert pipeline.not_a_hiring_post(
        "What to look for when hiring a creative strategist?",
        "I run a DTC beauty brand and I've never hired for this before.",
        url="https://www.reddit.com/r/AskMarketing/comments/abc/what_to_look_for/")
    assert pipeline.not_a_hiring_post(
        '"Creative Strategist"?',
        "Has anyone made the transition from a typical agency role? Is this a legitimate career path?",
        url="https://www.reddit.com/r/advertising/comments/abc/creative_strategist/")
    assert not pipeline.not_a_hiring_post(
        "Where can I find a good creative strategist based in the US?",
        "I've been trying for several months already to get a creative strategist.",
        url="https://www.reddit.com/r/advertising/comments/abc/where_can_i_find/")
    assert pipeline.not_a_hiring_post(
        "Hire a Creative Strategist for Your DTC Brand",
        "We help DTC and e-commerce founders hire creative strategists who scale paid social.",
        url="https://www.constanthire.com/creative-strategist-recruitment-agency")
    assert not pipeline.not_a_hiring_post(
        "Senior Creative Strategist Job | Lilo Social Careers",
        "Lilo Social is seeking a Senior Creative Strategist. We help founders grow. You'll hire no one on day one.",
        url="https://lilosocial.com/careers/senior-creative-strategist/")
    assert pipeline.not_a_hiring_post(
        "Copywriter Recruitment Agency – Hire Copywriters and Content Writers",
        "Hire Copywriters and Hire Content writers, proof readers, writers, editors.",
        url="https://www.80twenty.com/speciality/copywriting-content/")
    assert not pipeline.not_a_hiring_post(
        "Senior Copywriter",
        "Join our recruitment agency. We are hiring a senior copywriter for a client brand.",
        url="https://jobs.example.com/senior-copywriter")
    assert not pipeline.not_a_hiring_post(
        "Senior Copywriter at a recruitment agency",
        "We are hiring a senior copywriter. Clients hire us for campaigns.",
        url="https://jobs.example.com/senior-copywriter-agency")
    assert pipeline.not_a_hiring_post(
        "Creative Strategist freelancers - Hire",
        "Write down these goals clearly. This will help the creative strategist understand the brief.")
    assert not pipeline.not_a_hiring_post(
        "We're hiring a creative strategist",
        "What does a creative strategist do? You'll own the ads. We also keep a freelancers - hire list.")
    assert not pipeline.not_a_hiring_post(
        "We're hiring a creative strategist",
        "PSA to anyone hiring: send work. How to make a good first week. Examples: what works in our niche.")
    assert not pipeline.not_a_hiring_post(
        "We're hiring a funnel builder",
        "The best funnel builder for recruiting in 2026 is who we need. Easy guide in the doc.")
    assert not pipeline.not_a_hiring_post(
        "We're hiring a Creative Strategist",
        "What makes a great creative strategist? This is a fast growing job and we need one.")
    assert not pipeline.not_a_hiring_post(
        "We're hiring a copywriter",
        "How can I get hired? Send a portfolio. Seeking advice from the team is part of week one.")
    assert pipeline.not_a_hiring_post(
        "Hiring a creative strategist? - Melissa Jaalaid's Post",
        "I just did this for a client. They needed someone to pump out 30-40+ creatives weekly.")
    assert not pipeline.not_a_hiring_post(
        "Hiring a Senior Creative Strategist / Copywriter for a fast-growing brand",
        "Remote, apply here")
    assert not pipeline.not_a_hiring_post(
        "We're hiring a copywriter",
        "A checklist for your first week, and how to become comfortable with the brand voice")
    assert not pipeline.not_a_hiring_post(
        "We're hiring a copywriter",
        "Here's how to get hired onto the account: send a portfolio")
    assert pipeline.not_a_hiring_post(
        "BD Funnel Builder is hiring Software Engineering Manager", "Full-time", query='hiring "funnel builder"')
    assert pipeline.not_a_hiring_post(
        "Remote Senior Platform Engineer at Perspective",
        "Perspective - #1 Mobile Funnel Builder.", query='hiring "funnel builder"')
    assert pipeline.not_a_hiring_post(
        "Perspective is hiring a remote Senior Fullstack Engineer to join the team",
        "Perspective - #1 Mobile Funnel Builder.", query='hiring "funnel builder"')
    assert pipeline.not_a_hiring_post(
        "Remote Senior Backend Engineer (f/m/d) at Perspective",
        "Perspective - #1 Mobile Funnel Builder.", query='hiring "funnel builder"')
    assert pipeline.not_a_hiring_post(
        "NoDesk",
        "This is a full-time position that can be done remotely anywhere in Europe. Perspective - #1 Mobile Funnel Builder.",
        query='hiring "funnel builder"')
    assert not pipeline.not_a_hiring_post(
        "We're hiring a funnel builder",
        "You'll use Perspective, the #1 Mobile Funnel Builder, to ship the pages.",
        query='hiring "funnel builder"')
    assert pipeline.not_a_hiring_post(
        "Say hello to the Heyflow funnel builder",
        "Heyflow gives you the right funnel builder at your fingertips.",
        query='hiring "funnel builder"',
        url="https://heyflow.com/funnel-builder/")
    assert not pipeline.not_a_hiring_post(
        "We're hiring a funnel builder",
        "You'll have a funnel builder at your fingertips and a designer on the team.",
        query='hiring "funnel builder"')
    assert pipeline.not_a_hiring_post(
        "Hiring Funnel Builder | Spearlance",
        "Create professional job listings with AI-generated descriptions and Google for Jobs schema. Paying $4,000+ Per Hire. Own Your Hiring Pipeline.",
        query='hiring "funnel builder"',
        url="https://my.hiringapp.io/")
    assert not pipeline.not_a_hiring_post(
        "We're hiring a funnel builder",
        "You'll add a Google for Jobs schema to the career page and own your hiring pipeline.",
        query='hiring "funnel builder"')
    assert pipeline.not_a_hiring_post(
        "Find freelance funnel builder jobs",
        "Looking for clients as a freelance funnel builder? Sign up and offer your services.",
        query='hiring "funnel builder"',
        url="https://befreelancr.com/en/find-freelance-jobs/funnel-builder")
    assert not pipeline.not_a_hiring_post(
        "We're hiring a freelance funnel builder",
        "Looking for a freelance funnel builder to join the team.",
        query='hiring "funnel builder"')
    assert pipeline.not_a_hiring_post(
        "Writer jobs - Get A Copywriter",
        "We provide copywriting services for a wide range of industries.",
        url="https://jobs.getacopywriter.com/")
    assert not pipeline.not_a_hiring_post(
        "Copywriter",
        "We are hiring a copywriter. We provide copywriting services for our clients.",
        url="https://example.com/careers/copywriter")
    assert not pipeline.not_a_hiring_post(
        "NoDesk",
        "Perspective - #1 Mobile Funnel Builder.",
        query="hiring copywriter")
    assert not pipeline.not_a_hiring_post(
        "Now hiring - GoFunnels is looking for a full-time Funnel Builder",
        "Expertise with ClickFunnels, Go High Level, Kajabi", query='hiring "funnel builder"')
    assert not pipeline.not_a_hiring_post(
        "We're hiring a funnel builder",
        "You'll work with our backend engineers to ship the pages", query='hiring "funnel builder"')
    assert not pipeline.not_a_hiring_post(
        "We're hiring a copywriter", "for a software engineering company", query="hiring copywriter")
    assert pipeline.not_a_hiring_post(
        "#hiring | Marina Aghbalyan",
        "AI FUNNEL BUILDER I have 4+ years of hands-on experience with GoHighLevel")
    assert pipeline.not_a_hiring_post(
        "LinkedIn Ghostwriter | Newsletter & Case Study Writer",
        "Hi, I'm a copywriter and content writer specializing in helping startups")
    assert not pipeline.not_a_hiring_post(
        "Agency post",
        "Hi, I'm a copywriter and we're hiring a designer to pair with me")
    assert not pipeline.not_a_hiring_post(
        "Carol Dasaro's Post",
        "Hi everyone, I'm hiring for a freelance copywriter. We're looking for someone with 3-7 years")
    assert not pipeline.not_a_hiring_post(
        "Founder post",
        "Hi, I'm a founder and we're hiring a copywriter")
    assert not pipeline.not_a_hiring_post(
        "I am a Funnel Builder/Designer & CRM automation specialist",
        "Now Hiring: Funnel Builder Genius. We're looking for a skilled funnel builder")
    assert not pipeline.not_a_hiring_post(
        "We're hiring a funnel builder",
        "I have 8 years running this studio and we need a builder")
    assert pipeline.not_a_hiring_post(
        "[Hiring] 2D Animation Tutor — $30/hr (Remote)",
        "[HIRING] Creative Strategist · r/forhire",
        query='site:reddit.com/r/hiring/comments hiring "creative strategist"',
        url="https://www.reddit.com/r/hiring/comments/abc/2d_animation_tutor/")
    assert pipeline.not_a_hiring_post(
        "[HIRING] Viral Manager : r/hiring",
        "[HIRING] Creative Strategist",
        query='hiring "creative strategist"',
        url="https://www.reddit.com/r/hiring/comments/abc/viral_manager/")
    assert pipeline.not_a_hiring_post(
        "[HIRING] TikTok Content Poster – Remote Side Job",
        "Create and post 5 TikTok carousel posts per day",
        query="site:reddit.com/r/hiring/comments hiring copywriter",
        url="https://www.reddit.com/r/hiring/comments/abc/tiktok/")
    assert not pipeline.not_a_hiring_post(
        "[HIRING] Creative Strategist (Fully Remote)",
        "full-time",
        query='site:reddit.com/r/forhire/comments hiring "creative strategist"',
        url="https://www.reddit.com/r/forhire/comments/abc/creative_strategist/")
    assert not pipeline.not_a_hiring_post(
        "[Hiring] Copywriter with B2B SaaS Experience",
        "Website pages and cold emails",
        query="site:reddit.com/r/forhire/comments hiring copywriter",
        url="https://www.reddit.com/r/forhire/comments/abc/copywriter/")
    assert not pipeline.not_a_hiring_post(
        "Carol Dasaro's Post",
        "Hi everyone, I'm hiring for a freelance copywriter.",
        query="hiring copywriter",
        url="https://www.linkedin.com/posts/carol_activity")
    assert pipeline.not_a_hiring_post(
        '"is anyone hiring for a brand & creative strategist in nyc?',
        "is anyone hiring for a brand & creative strategist in nyc? my friend with 9+ yoe is looking to move")
    assert not pipeline.not_a_hiring_post(
        "We're hiring a creative strategist",
        "is anyone hiring? No. We're hiring a creative strategist for the brand team")


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
