"""Source-key length, fixture HTML escaping, and duplicate-job upsert.

These tests do not touch the network or Postgres. The upsert case uses an
in-memory SQLite session passed straight into _upsert_job.
"""
from __future__ import annotations

from datetime import datetime, timezone
from types import SimpleNamespace

import pytest
from sqlalchemy import create_engine, select
from sqlalchemy.orm import sessionmaker

from app.db import Base
from app.models import Job
from app.schemas import ExtractedJob, SearchResult
from app.scrapers import page_fetcher
from app.scrapers.page_fetcher import fetch_for_result
from app.scrapers.pipeline import _upsert_job, source_display_name, source_key_for_query
from app.config import settings


@pytest.fixture()
def db():
    engine = create_engine("sqlite://")
    Base.metadata.create_all(engine)
    session = sessionmaker(bind=engine, autoflush=False, expire_on_commit=False)()
    try:
        yield session
    finally:
        session.close()


def _job_inputs(url: str = "https://example.com/jobs/1") -> tuple[SearchResult, ExtractedJob]:
    result = SearchResult(
        url=url,
        title="Copywriter",
        snippet="We are hiring",
        source_query="hiring copywriter",
        platform="web",
    )
    extracted = ExtractedJob(
        is_real_job=True,
        title="Copywriter",
        company_or_poster="Acme",
        apply_url=url,
    )
    return result, extracted


def _as_utc(value: datetime) -> datetime:
    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)


def test_source_display_name_fits_column():
    assert source_display_name("hiring copywriter") == "Google: hiring copywriter"
    long = source_display_name("q" * 200)
    assert len(long) <= 128
    assert long.endswith("...")


def test_source_key_short_query_keeps_readable_slug():
    assert source_key_for_query('"hiring copywriter"') == "google:hiring_copywriter"


def test_source_key_long_query_is_bounded_stable_and_distinct():
    first = "a" * 200
    second = "a" * 199 + "b"
    key_a = source_key_for_query(first)
    key_b = source_key_for_query(second)

    assert len(key_a) <= 64
    assert len(key_b) <= 64
    assert key_a == source_key_for_query(first)
    assert key_a == source_key_for_query(first.upper())
    assert key_a == source_key_for_query(f'"{first}"')
    assert key_a != key_b
    assert key_a.startswith("google:" + "a" * 20)
    assert key_b.startswith("google:" + "a" * 20)

    fits = "c" * 57
    assert source_key_for_query(fits) == f"google:{fits}"
    assert len(source_key_for_query(fits)) == 64
    overflows = "d" * 58
    overflow_key = source_key_for_query(overflows)
    assert len(overflow_key) <= 64
    assert overflow_key != f"google:{overflows}"


def test_fetch_for_result_escapes_fixture_html(monkeypatch):
    monkeypatch.setattr(settings, "dev_fixtures", True)

    def _no_network(*_args, **_kwargs):
        raise AssertionError("fetch_for_result tried to hit the network")

    monkeypatch.setattr(page_fetcher, "fetch", _no_network)

    title = '<b>Hiring & "quoted"</b>'
    snippet = "<script>alert('x')</script> & more"
    url = "https://example.com/apply?q=1&role=\"writer\""
    page = fetch_for_result(
        SearchResult(url=url, title=title, snippet=snippet, source_query="hiring copywriter")
    )

    assert "&lt;b&gt;Hiring &amp; &quot;quoted&quot;&lt;/b&gt;" in page
    assert "<b>" not in page
    assert "<script>" not in page
    assert "&lt;script&gt;alert(&#x27;x&#x27;)&lt;/script&gt; &amp; more" in page
    assert "q=1&amp;role=&quot;writer&quot;" in page


def test_upsert_job_duplicate_refreshes_last_seen(db):
    source = SimpleNamespace(key="google:hiring_copywriter")
    result, extracted = _job_inputs()
    stale = datetime(2020, 1, 1, tzinfo=timezone.utc)

    added, updated = _upsert_job(db, source, result, extracted, set())
    assert (added, updated) == (True, False)
    db.flush()

    job = db.scalar(select(Job))
    job.last_seen_at = stale
    db.flush()
    db.expunge_all()

    added, updated = _upsert_job(db, source, result, extracted, set())
    assert (added, updated) == (False, True)
    db.flush()

    job = db.scalar(select(Job))
    assert _as_utc(job.last_seen_at) > stale


def test_upsert_job_integrity_error_refreshes_last_seen_and_keeps_session(db):
    """Race: the pre-insert lookup misses a row that the unique index still has.

    The second _upsert_job must roll back its savepoint, touch that row's
    last_seen_at, and leave the session usable for the next insert.
    """
    source = SimpleNamespace(key="google:hiring_copywriter")
    result, extracted = _job_inputs()
    stale = datetime(2020, 1, 1, tzinfo=timezone.utc)

    added, updated = _upsert_job(db, source, result, extracted, set())
    assert (added, updated) == (True, False)
    db.flush()

    job = db.scalar(select(Job))
    dedupe = job.dedupe_hash
    first_seen = job.first_seen_at
    job.last_seen_at = stale
    db.flush()
    db.expunge_all()

    original_scalar = db.scalar
    calls = {"n": 0}

    def miss_existing_once(statement, *args, **kwargs):
        calls["n"] += 1
        if calls["n"] == 1:
            return None
        return original_scalar(statement, *args, **kwargs)

    db.scalar = miss_existing_once

    added, updated = _upsert_job(db, source, result, extracted, set())
    assert (added, updated) == (False, True)
    assert calls["n"] >= 2

    db.scalar = original_scalar
    db.flush()
    rows = db.scalars(select(Job).where(Job.dedupe_hash == dedupe)).all()
    assert len(rows) == 1
    assert _as_utc(rows[0].last_seen_at) > stale
    assert rows[0].first_seen_at == first_seen

    other_result, other_extracted = _job_inputs("https://example.com/jobs/2")
    added, updated = _upsert_job(db, source, other_result, other_extracted, set())
    assert (added, updated) == (True, False)
    db.flush()
    assert db.scalar(select(Job).where(Job.source_url == "https://example.com/jobs/2")) is not None
