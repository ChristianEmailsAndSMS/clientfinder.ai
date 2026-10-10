"""Regression tests for the bugs found by the debug swarm."""
from datetime import datetime, timedelta, timezone

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from app.db import Base, get_db
from app.dedup import dedupe_hash, normalize_url
from app.main import app
from app.models import Job
from app.schemas import SearchResult
from app.scrapers.google_search import _platform_from_url, search
from app.scrapers.greenhouse import _is_remote, _location_name, _matches_title
from app.scrapers.llm_extractor import _build_prompt, _heuristic_mock, _loads_extraction, _parse_pay
from app.scrapers.pipeline import _google_source_key
from app.scrapers.problogger import parse_listings
from app.scrapers.remoteok import _amount, _matches, _pay_text


def _result(title: str, snippet: str, url: str, platform: str) -> SearchResult:
    return SearchResult(url=url, title=title, snippet=snippet, source_query="hiring copywriter", platform=platform)


def test_extraction_prompt_keeps_literal_json_braces():
    result = _result("Role", "body", "https://example.com/1", "twitter")
    prompt = _build_prompt(result, 'pay is $75/hr and a {brace}')
    assert '"is_real_job"' in prompt
    assert "twitter" in prompt
    assert "$75/hr" in prompt
    assert "<<PAGE_TEXT>>" not in prompt


def test_heuristic_pay_on_fixture_phrases():
    cases = [
        ("$75/hr remote", 75, None, "hour"),
        ("We run a $2M/yr Shopify store. $3,000/mo retainer.", 3000, None, "month"),
        ("$800-1,500 per page", 800, 1500, "project"),
        ("$2k project", 2000, None, "project"),
        ("$95,000-$130,000 + equity", 95000, 130000, "year"),
        ("Launching a $497 course. $1,200 fixed.", 1200, None, "project"),
        ("Senior Copywriter | $120k-$150k | Remote", 120000, 150000, "year"),
        ("$120-150k + stock", 120000, 150000, "year"),
        ("$50-70/hr", 50, 70, "hour"),
        ("$5 kids club", None, None, None),
    ]
    for text, pay_min, pay_max, period in cases:
        _, got_min, got_max, got_period = _parse_pay(text)
        assert (got_min, got_max, got_period) == (pay_min, pay_max, period), text


def test_heuristic_does_not_mark_non_remote_as_remote():
    job = _heuristic_mock(
        _result("Writer", "not remote, on-site $40/hr", "https://example.com/job", "web"),
        "",
    )
    assert job.remote is False
    assert job.pay_min == 40
    assert job.pay_period == "hour"


def test_copywriter_counts_as_copywriting_skill():
    job = _heuristic_mock(
        _result("Copywriter wanted", "$70-90/hr", "https://example.com/j", "web"),
        "",
    )
    assert "copywriting" in job.skills


def test_model_json_fences_and_null_skills():
    data = _loads_extraction('```JSON\n{"title": "A", "skills": null, "posted_at": "October 1, 2026"}\n```')
    assert data["title"] == "A"
    assert data["skills"] == []
    assert data["posted_at"] is None

    aware = _loads_extraction('{"title": "B", "skills": ["email"], "posted_at": "2026-10-01T12:00:00"}')
    assert aware["posted_at"].tzinfo is not None


def test_fixture_search_does_not_reuse_copywriter_sample(monkeypatch):
    monkeypatch.setattr("app.scrapers.google_search.settings.dev_fixtures", True)
    assert search("hiring videographer", num=5) == []
    hits = search('"hiring copywriter"', num=3)
    assert len(hits) == 3
    assert hits[0].platform == "twitter"


def test_platform_uses_hostname_not_substring():
    assert _platform_from_url("https://box.com/files") == "web"
    assert _platform_from_url("https://nottwitter.com/jobs") == "web"
    assert _platform_from_url("https://en.wikipedia.org/wiki/X.com") == "web"
    assert _platform_from_url("https://clever.com") == "web"
    assert _platform_from_url("https://x.com/ecom/status/1") == "twitter"
    assert _platform_from_url("https://jobs.lever.co/brand/role") == "lever"
    assert _platform_from_url("https://boards.greenhouse.io/stripe/jobs/1") == "greenhouse"


def test_long_source_key_fits_column():
    query = "hiring senior lifecycle email marketing copywriter for ecommerce brand remote"
    key = _google_source_key(query)
    assert len(key) <= 64
    assert key.startswith("google:")


def test_dedupe_keeps_non_twitter_s_param_and_collapses_www():
    a = dedupe_hash(url="https://example.com/jobs?s=copywriter", title="A")
    b = dedupe_hash(url="https://example.com/jobs?s=designer", title="A")
    assert a != b
    tw1 = dedupe_hash(url="https://twitter.com/x/status/1?s=1", title="A")
    tw2 = dedupe_hash(url="https://twitter.com/x/status/1?s=2", title="A")
    assert tw1 == tw2
    assert dedupe_hash(url="https://www.problogger.com/jobs/job/1", title="A") == dedupe_hash(
        url="https://problogger.com/jobs/job/1", title="A"
    )
    assert dedupe_hash(url="https://x.com/u/status/9", title="A") == dedupe_hash(
        url="https://twitter.com/u/status/9", title="A"
    )
    assert normalize_url("   ") == ""


def test_remoteok_zero_salary_and_description_false_positive():
    assert _amount(0) is None
    assert _amount("80000") == 80000
    assert _pay_text(None, None) is None
    assert _matches({
        "position": "Operations Engineer",
        "tags": ["python"],
        "description": "<p>We need a landing page and email marketing copy.</p>",
    }) is False
    assert _matches({"position": "Email Marketing Copywriter", "tags": ["klaviyo"], "description": ""}) is True


def test_greenhouse_title_and_location():
    assert _matches_title("Staff Software Engineer, Email Deliverability") is False
    assert _matches_title("Product Lifecycle Manager") is False
    assert _matches_title("Content Writer") is True
    assert _matches_title("Funnel Builder") is True
    assert _matches_title("Lifecycle Marketing Lead") is True
    assert _location_name("Remote, US") == "Remote, US"
    assert _location_name({"name": "New York"}) == "New York"
    assert _is_remote("non-remote") is False
    assert _is_remote("Remote") is True


PROBLOGGER_HTML = """
<article class="single-article"><a href="https://problogger.com/jobs/post-a-job/">Post a Job</a></article>
<div class="wpjb-grid-row wpjb-type-contract">
  <div class="wpjb-col-title">
    <span class="wpjb-line-major"><a href="/jobs/job/real-role/">Real Role</a></span>
    <span class="wpjb-sub">Acme Co</span>
  </div>
  <div class="wpjb-col-location">
    <span class="wpjb-line-major"><span class="wpjb-icon-location">Remote</span></span>
    <span class="wpjb-sub">Contract</span>
  </div>
  <div class="wpjb-grid-col-last"><span class="wpjb-line-major">Oct, 07</span></div>
</div>
<div class="wpjb-grid-row">
  <a class="job-listing" href="/category/jobs/">Jobs</a>
</div>
"""


def test_problogger_parses_wpjb_rows_not_nav_links():
    rows = parse_listings(PROBLOGGER_HTML)
    assert len(rows) == 1
    assert rows[0]["title"] == "Real Role"
    assert rows[0]["url"] == "https://problogger.com/jobs/job/real-role/"
    assert rows[0]["company"] == "Acme Co"
    assert rows[0]["type"] == "contract"
    assert rows[0]["remote"] is True
    assert rows[0]["posted_at"] is not None


@pytest.fixture()
def client():
    engine = create_engine(
        "sqlite://",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    Base.metadata.create_all(engine)
    Session = sessionmaker(bind=engine, autocommit=False, autoflush=False)
    session = Session()
    now = datetime.now(timezone.utc)

    def add(**kwargs):
        defaults = dict(
            dedupe_hash=kwargs["title"],
            source_url="https://example.com/" + kwargs["title"],
            platform="web",
            source_key="test",
            first_seen_at=now,
            last_seen_at=now,
            is_real_job=True,
        )
        defaults.update(kwargs)
        session.add(Job(**defaults))

    add(title="Hourly only min", pay_min=75, pay_max=None, pay_period="hour", type="hourly")
    add(title="Wide range low floor", pay_min=20, pay_max=100, pay_period="hour", type="hourly")
    add(title="Salary band", pay_min=80000, pay_max=120000, pay_period="year", type="full_time")
    add(
        title="Old post scraped today",
        posted_at=now - timedelta(days=30),
        first_seen_at=now,
    )
    add(title="No post date", posted_at=None, first_seen_at=now, type=None)
    add(title="100% match_me", raw_snippet="underscore_role")
    session.commit()

    def override():
        try:
            yield session
        finally:
            pass

    app.dependency_overrides[get_db] = override
    yield TestClient(app), session
    app.dependency_overrides.clear()
    session.close()


def test_job_filters(client):
    http, _session = client
    min_pay = http.get("/jobs", params={"min_pay": 50}).json()
    titles = {row["title"] for row in min_pay}
    assert "Hourly only min" in titles
    assert "Wide range low floor" not in titles
    assert "Salary band" in titles

    hourly = http.get("/jobs", params={"min_pay": 50, "pay_period": "hour"}).json()
    assert {row["title"] for row in hourly} == {"Hourly only min"}

    recent = http.get("/jobs", params={"posted_within_days": 7}).json()
    recent_titles = {row["title"] for row in recent}
    assert "Old post scraped today" not in recent_titles
    assert "No post date" in recent_titles

    percent = http.get("/jobs", params={"q": "%"}).json()
    assert [row["title"] for row in percent] == ["100% match_me"]
    assert http.get("/jobs", params={"q": "100%"}).json()[0]["title"] == "100% match_me"
    assert http.get("/jobs", params={"q": "match_me"}).json()[0]["title"] == "100% match_me"
    assert http.get("/jobs", params={"q": "underscoreXrole"}).json() == []

    stats = http.get("/jobs/stats").json()
    assert "null" not in stats["by_type"]
    assert None not in stats["by_type"]
    assert stats["by_type"]["hourly"] == 2

    page = http.get("/jobs", params={"limit": 2, "offset": 0}).json()
    assert len(page) == 2
    assert len({row["id"] for row in page}) == 2


def test_list_jobs_orders_ties_by_id(client):
    http, _session = client
    rows = http.get("/jobs", params={"limit": 50}).json()
    ids = [row["id"] for row in rows]
    assert ids == sorted(ids, reverse=True)
