"""Parser + ingest tests for the direct sources.

The sample payloads below are HAND-WRITTEN from the public API formats, not captured from the live
services (the build sandbox cannot reach them). They prove the parsing logic and idempotency; they do
NOT prove the live endpoints still look like this. Run scripts/check_sources.py on the server for that."""
from contextlib import contextmanager

import pytest
from sqlalchemy import create_engine, func, select
from sqlalchemy.orm import sessionmaker

from app.db import Base
from app.models import Job, ScrapeRun, Source
from app.scrapers import ashby, common, lever, remotive, weworkremotely
from app.scrapers.common import CollectResult, NormalizedJob, employment_type, parse_dt, title_matches


# ---------- helpers ----------
def test_title_matching_is_title_only_and_specific():
    assert title_matches("Senior Email Marketing Manager")
    assert title_matches("Direct Response Copywriter (Remote)")
    assert not title_matches("Staff Software Engineer, Email Infrastructure")
    assert not title_matches("Accountant")


@pytest.mark.parametrize("raw,want", [("Full-time", "full_time"), ("full_time", "full_time"), ("FullTime", "full_time"),
                                      ("Contract", "contract"), ("freelance", "contract"), ("Intern", None), (None, None)])
def test_employment_type(raw, want):
    assert employment_type(raw) == want


def test_parse_dt_variants():
    assert parse_dt("2026-10-01T12:00:00Z").tzinfo is not None
    assert parse_dt("2026-10-01T12:00:00").tzinfo is not None           # naive treated as UTC
    assert parse_dt(1759320000000).year == 2025                          # epoch ms
    assert parse_dt("garbage") is None and parse_dt(None) is None


# ---------- remotive ----------
REMOTIVE = {"job-count": 3, "jobs": [
    {"id": 1, "url": "https://remotive.com/remote-jobs/marketing/email-marketer-1", "title": "Email Marketer",
     "company_name": "Acme", "job_type": "contract", "publication_date": "2026-10-01T10:00:00",
     "candidate_required_location": "Worldwide", "salary": "$40-60/hr", "tags": ["email", "klaviyo"],
     "description": "<p>Run <b>flows</b></p>"},
    {"id": 2, "url": "https://remotive.com/remote-jobs/software-dev/dev-2", "title": "Backend Engineer", "company_name": "X"},
    {"id": 3, "title": "Copywriter without url"},
]}


def test_remotive_parse():
    r = remotive.parse(REMOTIVE)
    assert r.fetched == 3 and len(r.jobs) == 1
    j = r.jobs[0]
    assert (j.company, j.type, j.pay_text, j.remote) == ("Acme", "contract", "$40-60/hr", True)
    assert j.description == "Run flows" and j.skills == ["email", "klaviyo"] and j.posted_at.year == 2026


# ---------- we work remotely ----------
RSS = """<?xml version="1.0"?><rss version="2.0"><channel><title>WWR</title>
<item><title>Acme Co: Senior Copywriter</title><link>https://weworkremotely.com/remote-jobs/acme-senior-copywriter</link>
<pubDate>Wed, 01 Oct 2026 10:00:00 +0000</pubDate><region>Anywhere in the World</region><type>Full-Time</type>
<description>&lt;p&gt;Write things&lt;/p&gt;</description></item>
<item><title>Foo: DevOps Engineer</title><link>https://weworkremotely.com/remote-jobs/foo-devops</link></item>
<item><title>No colon lifecycle marketing lead</title><link>https://weworkremotely.com/remote-jobs/nc</link></item>
</channel></rss>"""


def test_wwr_parse():
    r = weworkremotely.parse_feed(RSS)
    assert r.fetched == 3 and [j.title for j in r.jobs] == ["Senior Copywriter", "No colon lifecycle marketing lead"]
    first = r.jobs[0]
    assert (first.company, first.type, first.location) == ("Acme Co", "full_time", "Anywhere in the World")
    assert first.posted_at.year == 2026 and r.jobs[1].company is None


def test_wwr_one_dead_feed_does_not_lose_the_others(monkeypatch):
    class Resp:
        text = RSS
    def fake_get(url, **k):
        if "sales" in url:
            raise RuntimeError("404")
        return Resp()
    monkeypatch.setattr(weworkremotely, "get", fake_get)
    r = weworkremotely.collect()
    assert len(r.errors) == 1 and "sales" in r.errors[0] and len(r.jobs) == 4 and r.fetched == 6


# ---------- lever ----------
LEVER = [
    {"id": "a", "text": "Lifecycle Marketing Manager", "hostedUrl": "https://jobs.lever.co/acme/a", "createdAt": 1759320000000,
     "categories": {"commitment": "Full-time", "location": "Remote - US"}, "workplaceType": "remote",
     "descriptionPlain": "Own CRM.", "salaryRange": {"min": 90000, "max": 120000, "currency": "USD", "interval": "per-year-salary"}},
    {"id": "b", "text": "Data Scientist", "hostedUrl": "https://jobs.lever.co/acme/b", "categories": {}},
]


def test_lever_parse():
    r = lever.parse("acme", LEVER)
    assert r.fetched == 2 and len(r.jobs) == 1
    j = r.jobs[0]
    assert (j.type, j.pay_min, j.pay_max, j.pay_period, j.remote) == ("full_time", 90000, 120000, "year", True)
    assert j.pay_text == "USD 90,000-120,000" and j.posted_at.year == 2025


def test_lever_404_company_is_an_error_not_a_crash(monkeypatch):
    def fake_get(url, **k):
        raise RuntimeError("404")
    monkeypatch.setattr(lever, "get", fake_get)
    r = lever.collect(("nope",))
    assert r.errors and not r.jobs and r.fetched == 0


# ---------- ashby ----------
ASHBY = {"jobs": [
    {"title": "Creative Strategist", "jobUrl": "https://jobs.ashbyhq.com/acme/1", "isListed": True, "isRemote": True,
     "employmentType": "Contract", "publishedAt": "2026-10-02T09:00:00.000+00:00", "location": "Remote",
     "compensation": {"compensationTierSummary": "$100K – $130K"}, "descriptionPlain": "Concepts."},
    {"title": "Landing Page Designer", "jobUrl": "https://jobs.ashbyhq.com/acme/2", "isListed": False},
    {"title": "Recruiter", "jobUrl": "https://jobs.ashbyhq.com/acme/3"},
]}


def test_ashby_parse_skips_unlisted_and_nonmatching():
    r = ashby.parse("acme", ASHBY)
    assert r.fetched == 3 and len(r.jobs) == 1
    j = r.jobs[0]
    assert (j.type, j.remote, j.pay_text, j.company) == ("contract", True, "$100K – $130K", "acme")


# ---------- ingest (sqlite stand-in for Postgres) ----------
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

    monkeypatch.setattr(common, "session_scope", scope)
    return scope


def _result(n=2, errors=()):
    jobs = [NormalizedJob(url=f"https://x.test/j/{i}?utm_source=a", title=f"Copywriter {i}", company="Acme") for i in range(n)]
    return CollectResult(fetched=n + 3, jobs=jobs, errors=list(errors))


def test_ingest_is_idempotent_and_tracks_runs(db):
    first = common.ingest("t", "Test", "t", _result())
    assert (first["added"], first["updated"]) == (2, 0)
    again = common.ingest("t", "Test", "t", _result())
    assert (again["added"], again["updated"]) == (0, 2)
    with db() as s:
        assert s.scalar(select(func.count(Job.id))) == 2
        assert s.scalar(select(func.count(Source.id))) == 1
        assert [r.status for r in s.scalars(select(ScrapeRun).order_by(ScrapeRun.id))] == ["ok", "ok"]


def test_ingest_dedupes_tracking_params_and_within_batch(db):
    r = _result(1)
    r.jobs.append(NormalizedJob(url="https://x.test/j/0?utm_campaign=z", title="Copywriter 0", company="Acme"))
    assert common.ingest("t", "Test", "t", r)["added"] == 1


def test_ingest_run_status_all_feeds_failed(db):
    out = common.ingest("t", "Test", "t", CollectResult(fetched=0, errors=["a: 404", "b: 404"]))
    assert out["errors"] == 2
    with db() as s:
        run = s.scalar(select(ScrapeRun))
        assert run.status == "failed" and "a: 404" in run.error


def test_ingest_partial_failure_is_ok_with_note(db):
    common.ingest("t", "Test", "t", _result(1, errors=["lever/x: 404"]))
    with db() as s:
        run = s.scalar(select(ScrapeRun))
        assert run.status == "ok" and "lever/x" in run.error
