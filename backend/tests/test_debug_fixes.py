"""Regression tests for the bugs the debug swarm found."""
from datetime import datetime, timedelta, timezone

from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from app.db import Base, get_db
from app.dedup import dedupe_hash, normalize_url
from app.main import app
from app.models import Job
from app.schemas import ExtractedJob, JobOut
from app.scrapers.google_search import _fixture_results, _result_from_item
from app.scrapers.greenhouse import _matches_title
from app.scrapers.llm_extractor import _heuristic_mock, _parse_pay, build_extraction_prompt
from app.scrapers.pipeline import source_key_for_query
from app.scrapers.problogger import parse_listing_html
from app.scrapers.remoteok import _matches
from app.schemas import SearchResult


def test_extraction_prompt_survives_braces():
    page = 'pay is { "amount": 50 } and <div>more</div>'
    prompt = build_extraction_prompt("reddit", page)
    assert "reddit" in prompt
    assert page in prompt
    assert "{amount" not in prompt or '"amount"' in prompt


def test_pay_regex_keeps_real_units():
    text, lo, hi, period = _parse_pay("role $3,000/mo remote")
    assert text == "$3,000/mo"
    assert lo == 3000
    assert hi is None
    assert period is None

    text, lo, hi, period = _parse_pay("budget $800-1,500 per page")
    assert text == "$800-1,500"
    assert lo == 800
    assert hi == 1500
    assert period is None

    text, lo, hi, period = _parse_pay("pays $50/hr")
    assert text == "$50/hr"
    assert lo == 50
    assert period == "hour"


def test_heuristic_reads_page_html():
    result = SearchResult(
        url="https://example.com/job",
        title="Email marketer",
        snippet="",
        source_query="hiring email marketer",
    )
    job = _heuristic_mock(result, "<html><body><p>Pays $200/hr, remote</p></body></html>")
    assert job.pay_min == 200
    assert job.pay_period == "hour"
    assert job.remote is True


def test_null_skills_do_not_crash_models():
    extracted = ExtractedJob(skills=None)
    assert extracted.skills == []
    now = datetime.now(timezone.utc)
    out = JobOut(
        id=1,
        source_url="https://example.com",
        platform="web",
        title="Role",
        skills={"not": "a list"},
        first_seen_at=now,
        last_seen_at=now,
        source_key="google:test",
        is_real_job=True,
    )
    assert out.skills == []


def test_dedupe_keeps_job_identifying_params():
    a = dedupe_hash(url="https://example.com/job?source=12345", title="A", company="Acme")
    b = dedupe_hash(url="https://example.com/job?source=99999", title="B", company="Other")
    assert a != b
    tracked = dedupe_hash(url="https://example.com/job?utm_source=newsletter", title="A", company="Acme")
    plain = dedupe_hash(url="https://example.com/job", title="A", company="Acme")
    assert tracked == plain


def test_twitter_tracking_params_are_host_specific():
    assert normalize_url("https://x.com/post?s=abc") == normalize_url("https://x.com/post?s=zzz")
    assert normalize_url("https://jobs.example.com/role?s=abc") != normalize_url("https://jobs.example.com/role?s=zzz")


def test_source_key_fits_column():
    key = source_key_for_query("hiring " + ("copywriter " * 40))
    assert len(key) <= 64
    assert key.startswith("google:")
    short = source_key_for_query('"hiring copywriter"')
    assert short == "google:hiring_copywriter"


def test_fixture_search_respects_num_and_null_snippet():
    rows = _fixture_results('"hiring email marketer"', num=2)
    assert 1 <= len(rows) <= 2
    row = _result_from_item({"link": "https://example.com/j", "title": None, "snippet": None}, "q")
    assert row is not None
    assert row.snippet == ""
    assert row.title == ""


def test_problogger_parser_skips_post_a_job():
    html = """
    <article class="single-article">
      <a href="/jobs/post-a-job/">Post a Job</a>
      <div class="wpjb-grid-row wpjb-click-area">
        <a href="/jobs/job/email-writer/">Email Writer</a>
        <p>$800-1,500 per page</p>
      </div>
      <div class="wpjb-grid-row wpjb-click-area">
        <a href="https://problogger.com/jobs/job/funnel/">Funnel Builder</a>
      </div>
    </article>
    """
    rows = parse_listing_html(html, "https://problogger.com/jobs/?show_results=1&page=1")
    assert [r["title"] for r in rows] == ["Email Writer", "Funnel Builder"]
    assert rows[0]["url"] == "https://problogger.com/jobs/job/email-writer/"
    assert parse_listing_html("<article><a href='/jobs/post-a-job/'>Post a Job</a></article>", "https://problogger.com/jobs/") == []


def test_greenhouse_title_filter_skips_lifecycle_infra():
    assert _matches_title("Staff Engineer, Datacenter Server Lifecycle") is False
    assert _matches_title("Lifecycle Marketing Manager") is True
    assert _matches_title("Email Copywriter") is True


def test_remoteok_ignores_description_only_matches():
    assert _matches({"position": "Project Manager", "tags": [], "description": "we do copywriting"}) is False
    assert _matches({"position": "Email Marketing Manager", "tags": ["saas"], "description": ""}) is True


def _session():
    # StaticPool keeps the in-memory database on one connection across threads.
    engine = create_engine(
        "sqlite://",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    Base.metadata.create_all(engine)
    return sessionmaker(bind=engine)()


def _client(db):
    def override():
        yield db

    app.dependency_overrides[get_db] = override
    return TestClient(app)


def _job(**kwargs) -> Job:
    now = datetime.now(timezone.utc)
    defaults = dict(
        dedupe_hash=kwargs.get("dedupe_hash", "h"),
        source_url="https://example.com/job",
        platform="web",
        title="Role",
        first_seen_at=now,
        last_seen_at=now,
        source_key="test",
        is_real_job=True,
    )
    defaults.update(kwargs)
    return Job(**defaults)


def test_pay_and_posted_filters():
    db = _session()
    now = datetime.now(timezone.utc)
    old = now - timedelta(days=100)
    db.add_all([
        _job(dedupe_hash="a", title="Hourly only", pay_min=100, pay_max=None, posted_at=old, first_seen_at=now),
        _job(dedupe_hash="b", title="Cap only", pay_min=None, pay_max=80, posted_at=now),
        _job(dedupe_hash="c", title="Band", pay_min=50, pay_max=150, posted_at=now),
        _job(dedupe_hash="d", title="No pay old", posted_at=old, first_seen_at=old),
    ])
    db.commit()

    def titles(path: str) -> list[str]:
        res = client.get(path)
        assert res.status_code == 200, res.text
        return sorted(row["title"] for row in res.json())

    client = _client(db)
    try:
        assert titles("/jobs?min_pay=90") == ["Band", "Hourly only"]
        assert titles("/jobs?max_pay=90") == ["Band", "Cap only"]
        recent = titles("/jobs?posted_within_days=7")
        assert "Hourly only" not in recent
        assert "Band" in recent
        assert "No pay old" not in recent
    finally:
        app.dependency_overrides.clear()


def test_jobs_feed_is_html_and_bad_skills_do_not_500():
    db = _session()
    now = datetime.now(timezone.utc)
    db.add(_job(dedupe_hash="ok", title="Good <role>", skills=["email"]))
    db.add(_job(
        dedupe_hash="bad",
        title="Bad skills",
        skills={"x": 1},
        first_seen_at=now - timedelta(seconds=1),
    ))
    db.commit()

    try:
        client = _client(db)
        listing = client.get("/jobs")
        assert listing.status_code == 200
        body = listing.json()
        assert {row["title"] for row in body} == {"Good <role>", "Bad skills"}
        feed = client.get("/jobs/feed")
        assert feed.status_code == 200
        assert "text/html" in feed.headers["content-type"]
        assert "Good &lt;role&gt;" in feed.text
        assert "Today&#39;s jobs" in feed.text or "Today's jobs" in feed.text
        home = client.get("/")
        assert 'href="/jobs/feed"' in home.text
    finally:
        app.dependency_overrides.clear()
