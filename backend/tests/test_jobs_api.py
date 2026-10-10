"""API tests for job listing filters and stats.

Uses an in-memory SQLite database so the suite never opens Postgres.
"""

from datetime import datetime, timezone

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from app.api.jobs import get_db
from app.db import Base
from app.main import app
from app.models import Job


@pytest.fixture
def jobs_api():
    engine = create_engine(
        "sqlite://",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    Base.metadata.create_all(engine)
    Session = sessionmaker(bind=engine, autocommit=False, autoflush=False, future=True)
    seq = {"n": 0}

    def override_get_db():
        db = Session()
        try:
            yield db
        finally:
            db.close()

    def add_job(**overrides) -> Job:
        seq["n"] += 1
        n = seq["n"]
        now = datetime.now(timezone.utc)
        fields = {
            "dedupe_hash": f"dedupe-{n}",
            "source_url": f"https://example.com/jobs/{n}",
            "platform": "web",
            "title": f"Job {n}",
            "first_seen_at": now,
            "last_seen_at": now,
            "source_key": "test",
            "is_real_job": True,
        }
        fields.update(overrides)
        db = Session()
        try:
            job = Job(**fields)
            db.add(job)
            db.commit()
            db.refresh(job)
            return job
        finally:
            db.close()

    app.dependency_overrides[get_db] = override_get_db
    try:
        with TestClient(app) as client:
            yield client, add_job
    finally:
        app.dependency_overrides.pop(get_db, None)
        engine.dispose()


def _titles(response) -> set[str]:
    assert response.status_code == 200, response.text
    return {row["title"] for row in response.json()}


def test_min_pay_includes_job_with_null_pay_max(jobs_api):
    client, add_job = jobs_api
    add_job(title="hourly-80", pay_min=80, pay_max=None)
    add_job(title="hourly-20", pay_min=20, pay_max=None)
    add_job(title="ranged", pay_min=40, pay_max=90)

    found = _titles(client.get("/jobs", params={"min_pay": 50}))
    assert "hourly-80" in found
    assert "ranged" in found
    assert "hourly-20" not in found

    # Missing pay_max is the known pay_min, so the point range must sit inside the window.
    assert "hourly-80" in _titles(client.get("/jobs", params={"min_pay": 50, "max_pay": 100}))
    assert "hourly-80" not in _titles(client.get("/jobs", params={"min_pay": 50, "max_pay": 70}))


def test_max_pay_includes_job_with_null_pay_min(jobs_api):
    client, add_job = jobs_api
    add_job(title="up-to-40", pay_min=None, pay_max=40)
    add_job(title="up-to-120", pay_min=None, pay_max=120)
    add_job(title="ranged", pay_min=30, pay_max=80)

    found = _titles(client.get("/jobs", params={"max_pay": 50}))
    assert "up-to-40" in found
    assert "ranged" in found
    assert "up-to-120" not in found


def test_pay_filter_excludes_jobs_with_both_bounds_null(jobs_api):
    client, add_job = jobs_api
    add_job(title="unknown-pay", pay_min=None, pay_max=None)
    add_job(title="known-pay", pay_min=60, pay_max=70)

    for params in ({"min_pay": 50}, {"max_pay": 100}, {"min_pay": 50, "max_pay": 100}):
        found = _titles(client.get("/jobs", params=params))
        assert "unknown-pay" not in found
        assert "known-pay" in found


def test_stats_null_type_returns_unknown_key(jobs_api):
    client, add_job = jobs_api
    add_job(title="untyped-a", type=None, platform="reddit")
    add_job(title="untyped-b", type=None, platform="reddit")
    add_job(title="contract", type="contract", platform="upwork")

    response = client.get("/jobs/stats")
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["total"] == 3
    assert body["by_type"]["unknown"] == 2
    assert body["by_type"]["contract"] == 1
    assert body["by_platform"]["reddit"] == 2
    assert body["by_platform"]["upwork"] == 1


def test_keyword_escapes_like_wildcards(jobs_api):
    client, add_job = jobs_api
    add_job(title="100 dollars")
    add_job(title="Raise of 100% this year")
    add_job(title="Copywriter")
    add_job(title="senior_dev")
    add_job(title="seniorxdev")
    add_job(title=r"file\path")

    percent = _titles(client.get("/jobs", params={"q": "%"}))
    assert percent == {"Raise of 100% this year"}

    one_hundred_percent = _titles(client.get("/jobs", params={"q": "100%"}))
    assert "100 dollars" not in one_hundred_percent
    assert "Raise of 100% this year" in one_hundred_percent

    underscore = _titles(client.get("/jobs", params={"q": "_"}))
    assert underscore == {"senior_dev"}

    # Once ESCAPE is '\', a raw backslash in the query must stay literal.
    backslash = _titles(client.get("/jobs", params={"q": "\\"}))
    assert backslash == {r"file\path"}


def test_keyword_matches_copywriter_case_insensitively(jobs_api):
    client, add_job = jobs_api
    add_job(title="Copywriter", company_or_poster="Acme", raw_snippet="Write landing pages")
    add_job(title="Accountant", raw_snippet="Close the books")

    found = _titles(client.get("/jobs", params={"q": "copy"}))
    assert found == {"Copywriter"}
