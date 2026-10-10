"""Regression tests for the debug-swarm fixes. SQLite only; no Postgres."""
import json
from datetime import datetime, timedelta, timezone
from pathlib import Path

from configparser import ConfigParser

from sqlalchemy import create_engine, select
from sqlalchemy.orm import sessionmaker

from app.api.jobs import filtered_jobs_stmt, job_stats
from app.config import settings
from app.db import Base, add_ignoring_conflict
from app.dedup import dedupe_hash, normalize_url
from app.models import Job
from app.scrapers.google_search import _platform_from_url, search
from app.scrapers.greenhouse import _matches_title, is_remote, plain_content, posted_at_of
from app.scrapers.llm_extractor import build_extraction_prompt, infer_pay, parse_model_payload
from app.scrapers.pipeline import source_key_for_query
from app.scrapers.problogger import parse_listings
from app.scrapers.remoteok import _matches, job_type, pay_fields, plain_text

FIXTURE = Path(__file__).resolve().parent.parent / "fixtures" / "serpapi_hiring_copywriter.json"


def _session():
    engine = create_engine("sqlite://")
    Base.metadata.create_all(engine)
    return sessionmaker(bind=engine)()


def _job(**kwargs) -> Job:
    now = datetime.now(timezone.utc)
    base = dict(
        dedupe_hash="h",
        source_url="https://example.com/j",
        platform="web",
        title="Copywriter",
        first_seen_at=now,
        last_seen_at=now,
        source_key="test",
        is_real_job=True,
    )
    base.update(kwargs)
    return Job(**base)


def test_metadata_create_all_has_one_platform_index():
    engine = create_engine("sqlite://")
    Base.metadata.create_all(engine)
    indexes = {idx.name for idx in Job.__table__.indexes}
    assert "ix_jobs_platform" in indexes
    assert sum(1 for idx in Job.__table__.indexes if "platform" in idx.columns) == 1


def test_infer_pay_matches_fixture_salaries():
    raw = json.loads(FIXTURE.read_text())
    parsed = {}
    for item in raw["organic_results"]:
        combined = f"{item['title']} {item['snippet']}".lower()
        parsed[item["position"]] = infer_pay(combined)

    assert parsed[1][1:] == (75.0, None, "hour")
    assert parsed[2][1:] == (3000.0, None, "month")
    assert parsed[3][1:] == (800.0, 1500.0, "project")
    assert parsed[4][1:] == (2000.0, None, "project")
    assert parsed[5][1:] == (95000.0, 130000.0, None)
    assert parsed[6][1:] == (80.0, None, "hour")
    assert parsed[7][1:] == (1200.0, None, "project")
    assert parsed[8][1:] == (120000.0, 150000.0, "year")
    assert parsed[9][1:] == (50.0, 70.0, "hour")
    assert parsed[10][1:] == (70.0, 90.0, "hour")


def test_infer_pay_does_not_treat_the_next_word_as_thousands():
    text, lo, hi, period = infer_pay("$50 kitchen remodel")
    assert (lo, hi, period) == (50.0, None, None)
    assert text == "$50"


def test_prompt_substitution_keeps_json_braces():
    page = "Pay is {not_a_field} and a literal {page_text}"
    prompt = build_extraction_prompt("twitter", page)
    assert '"is_real_job"' in prompt
    assert "Context source: twitter." in prompt
    assert page in prompt
    assert "{source_hint}" not in prompt


def test_model_json_tolerates_fences_and_loose_fields():
    job = parse_model_payload(
        'Sure\n```JSON\n{"title": "Writer", "skills": null, "posted_at": "yesterday"}\n```',
        "https://example.com/job",
    )
    assert job.title == "Writer"
    assert job.skills == []
    assert job.posted_at is None
    assert job.is_real_job is False
    assert job.apply_url == "https://example.com/job"


def test_platform_uses_hostname():
    assert _platform_from_url("https://netflix.com/jobs") == "web"
    assert _platform_from_url("https://jobs.dropbox.com/x") == "web"
    assert _platform_from_url("https://box.com/careers") == "web"
    assert _platform_from_url("https://nottwitter.com/hire") == "web"
    assert _platform_from_url("https://x.com/ecom/status/1") == "twitter"
    assert _platform_from_url("https://mobile.twitter.com/x/status/1") == "twitter"
    assert _platform_from_url("https://boards.greenhouse.io/acme/jobs/1") == "greenhouse"
    assert _platform_from_url("https://jobs.lever.co/brand/role") == "lever"


def test_fixture_search_respects_num(monkeypatch):
    monkeypatch.setattr(settings, "dev_fixtures", True)
    assert len(search('"hiring copywriter"', num=3)) == 3


def test_normalize_url_keeps_job_identity():
    assert normalize_url("https://jobs.example.com/apply?t=101") != normalize_url(
        "https://jobs.example.com/apply?t=202"
    )
    assert normalize_url("http://www.example.com/jobs/1") == normalize_url("https://example.com/jobs/1/")
    assert normalize_url("https://example.com/jobs/1?utm_source=newsletter") == normalize_url(
        "https://example.com/jobs/1"
    )
    assert normalize_url("https://example.com/careers#/job/1") != normalize_url(
        "https://example.com/careers#/job/2"
    )
    assert dedupe_hash(url="   ", title="Writer", company="Acme") == dedupe_hash(
        url=None, title="Writer", company="Acme"
    )


def test_long_query_source_key_fits_column():
    key = source_key_for_query("looking for a freelance email copywriter for klaviyo flows")
    assert len(key) <= 64
    assert source_key_for_query('"hiring copywriter"') == "google:hiring_copywriter"


def test_percent_encoded_database_url_survives_configparser():
    url = "postgresql+psycopg://clientfinder:p%40ss@localhost:5433/clientfinder"
    cp = ConfigParser()
    cp.read_string("[alembic]\nsqlalchemy.url = placeholder\n")
    cp.set("alembic", "sqlalchemy.url", url.replace("%", "%%"))
    assert cp.get("alembic", "sqlalchemy.url") == url


def test_problogger_parser_skips_post_a_job_cta():
    html = """
    <article class="single-article"><a href="/post-a-job/">Post a Job</a></article>
    <div class="wpjb-grid-row wpjb-type-freelance">
      <div class="wpjb-col-title"><a href="/jobs/agriculture-specialist/">Agriculture business specialist</a></div>
      <div class="wpjb-sub">Acme Farms</div>
      <div class="wpjb-icon-location">Remote</div>
    </div>
    <div class="wpjb-grid-row wpjb-type-full-time">
      <div class="wpjb-col-title"><a href="https://problogger.com/jobs/editor/">Staff editor</a></div>
    </div>
    """
    rows = parse_listings(html)
    assert [row["title"] for row in rows] == ["Agriculture business specialist", "Staff editor"]
    assert rows[0]["url"] == "https://problogger.com/jobs/agriculture-specialist/"
    assert rows[0]["company"] == "Acme Farms"
    assert rows[0]["type"] == "contract"
    assert rows[1]["type"] == "full_time"


def test_remoteok_match_pay_and_type():
    assert _matches({"position": "Writer", "tags": ["email", "marketing"], "description": "<p>ignore</p>"})
    assert not _matches({
        "position": "Operations Engineer",
        "tags": ["python"],
        "description": "<p>Done For You Email Marketing</p>",
    })
    assert pay_fields(0, 0) == (None, None, None, None)
    assert pay_fields(100000, 120000) == ("$100,000-$120,000", 100000.0, 120000.0, "year")
    assert job_type(["Design", "part time"]) == "part_time"
    assert job_type(["part-time"]) == "part_time"
    assert job_type(["internship"]) == "internship"
    assert job_type(["internal", "tools"]) == "full_time"
    assert plain_text("<p>Hello <b>there</b></p>") == "Hello there"


def test_greenhouse_title_date_remote_and_html():
    assert not _matches_title("Staff Engineer, Datacenter Server Lifecycle")
    assert _matches_title("Lifecycle Marketing Manager")
    assert _matches_title("Email Copywriter")
    assert plain_content("&lt;h2&gt;&lt;strong&gt;Who we are&lt;/strong&gt;&lt;/h2&gt;") == "Who we are"
    posted = posted_at_of({
        "first_published": "2026-09-09T00:00:00Z",
        "updated_at": "2026-10-07T00:00:00Z",
    })
    assert posted == datetime(2026, 9, 9, tzinfo=timezone.utc)
    assert is_remote(
        {"metadata": [{"name": "Location Type", "value": "On-Site"}]},
        "Remote-Friendly, United States",
    ) is False
    assert is_remote({}, "Remote-Friendly, United States") is None
    assert is_remote({}, "Remote, United States") is True


def test_min_pay_keeps_single_sided_rates_and_hides_old_posts():
    db = _session()
    now = datetime.now(timezone.utc)
    old = now - timedelta(days=400)
    db.add(_job(dedupe_hash="solo", title="$75/hr", pay_min=75, pay_max=None, posted_at=now, first_seen_at=now))
    db.add(_job(dedupe_hash="band", title="band", pay_min=50, pay_max=70, posted_at=now, first_seen_at=now))
    db.add(_job(dedupe_hash="low", title="low", pay_min=10, pay_max=20, posted_at=now, first_seen_at=now))
    db.add(_job(dedupe_hash="old", title="old post", pay_min=200, pay_max=200, posted_at=old, first_seen_at=now))
    db.add(_job(dedupe_hash="undated", title="undated", posted_at=None, first_seen_at=now))
    db.commit()

    titles = {row.title for row in db.scalars(filtered_jobs_stmt(min_pay=50))}
    assert titles == {"$75/hr", "band", "old post"}

    recent = {row.title for row in db.scalars(filtered_jobs_stmt(posted_within_days=7))}
    assert "old post" not in recent
    assert "$75/hr" in recent
    assert "undated" in recent
    db.close()


def test_keyword_underscore_is_literal_and_pages_do_not_overlap():
    db = _session()
    now = datetime.now(timezone.utc)
    db.add(_job(dedupe_hash="a", title="Alpha", first_seen_at=now))
    db.add(_job(dedupe_hash="b", title="B_C", first_seen_at=now))
    db.commit()

    assert [row.title for row in db.scalars(filtered_jobs_stmt(q="_"))] == ["B_C"]
    page1 = db.scalars(filtered_jobs_stmt(limit=1, offset=0)).all()
    page2 = db.scalars(filtered_jobs_stmt(limit=1, offset=1)).all()
    assert {page1[0].id, page2[0].id} == {row.id for row in db.scalars(select(Job))}
    assert page1[0].id != page2[0].id
    db.close()


def test_stats_drop_null_type_and_conflict_does_not_abort_the_session():
    db = _session()
    db.add(_job(dedupe_hash="a", type=None, platform="web"))
    db.add(_job(dedupe_hash="b", type="contract", platform="reddit"))
    db.commit()
    stats = job_stats(db)
    assert stats["by_type"] == {"contract": 1}
    assert None not in stats["by_platform"]

    assert add_ignoring_conflict(db, _job(dedupe_hash="a", title="duplicate")) is False
    assert add_ignoring_conflict(db, _job(dedupe_hash="c", title="after conflict")) is True
    db.commit()
    assert db.scalar(select(Job).where(Job.dedupe_hash == "c")) is not None
    db.close()
