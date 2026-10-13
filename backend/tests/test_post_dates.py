"""'691 months ago': an impossible posting date must never reach a customer, in any sort, filter or card."""
from datetime import datetime, timedelta, timezone

import pytest
from fastapi.testclient import TestClient

import authkit
from authkit import CSRF, PW
from app.dates import MIN_POSTED, sane_posted_at
from app.main import app
from app.models import Job
from app.scrapers.common import parse_dt

NOW = datetime(2026, 10, 13, 12, 0, tzinfo=timezone.utc)


@pytest.fixture
def env(monkeypatch):
    e = authkit.make_env(monkeypatch)
    yield e
    app.dependency_overrides.clear()


def test_the_believable_date_rule():
    assert sane_posted_at(None) is None
    assert sane_posted_at(datetime(1970, 1, 1, tzinfo=timezone.utc)) is None
    assert sane_posted_at(datetime(1969, 2, 1)) is None                                   # naive dates are read as UTC
    assert sane_posted_at(datetime(2014, 12, 31, tzinfo=timezone.utc)) is None
    assert sane_posted_at(MIN_POSTED) == MIN_POSTED
    assert sane_posted_at(NOW - timedelta(days=30), NOW) == NOW - timedelta(days=30)
    assert sane_posted_at(NOW + timedelta(hours=20), NOW) is not None                      # time zones
    assert sane_posted_at(NOW + timedelta(days=3), NOW) is None                             # the future is not a posting date


def test_parsers_drop_epoch_zero_and_ancient_values():
    assert parse_dt(0) is None and parse_dt(-86400 * 365) is None
    assert parse_dt("1970-01-01T00:00:00Z") is None and parse_dt("0001-01-01") is None
    assert parse_dt(1_700_000_000) == datetime.fromtimestamp(1_700_000_000, tz=timezone.utc)      # a real one still works
    assert parse_dt(1_700_000_000_000) == datetime.fromtimestamp(1_700_000_000, tz=timezone.utc)   # milliseconds too


def add(env, title, posted, seen):
    with env.scope() as db:
        n = db.query(Job).count()
        db.add(Job(title=title, platform="reddit", source_key="s", source_url=f"https://example.com/{n}", dedupe_hash=f"h{n}",
                   is_real_job=True, posted_at=posted, first_seen_at=seen, last_seen_at=seen))


def test_api_never_returns_an_impossible_date_and_sorts_by_the_shown_date(env):
    now = datetime.now(timezone.utc)
    add(env, "Real, posted yesterday", now - timedelta(days=1), now - timedelta(hours=1))
    add(env, "Epoch zero", datetime(1970, 1, 1, tzinfo=timezone.utc), now - timedelta(days=5))
    add(env, "Year 1969", datetime(1969, 2, 1, tzinfo=timezone.utc), now - timedelta(days=9))
    add(env, "From the future", now + timedelta(days=400), now - timedelta(days=2))
    c = TestClient(app)
    assert c.post("/auth/signup", json={"email": "d@example.com", "password": PW}, headers=CSRF).status_code == 201
    rows = c.get("/jobs?sort=oldest").json()
    assert [r["title"] for r in rows] == ["Year 1969", "Epoch zero", "From the future", "Real, posted yesterday"]    # by first seen, not by 1969
    for r in rows:
        shown = r["posted_at"] or r["first_seen_at"]
        assert datetime.fromisoformat(shown.replace("Z", "+00:00")).replace(tzinfo=timezone.utc) >= MIN_POSTED
        assert r["title"] == "Real, posted yesterday" or r["posted_at"] is None
    assert [r["title"] for r in c.get("/jobs?sort=newest").json()][0] == "Real, posted yesterday"
    assert {r["title"] for r in c.get("/jobs?posted_within_days=3").json()} == {"Real, posted yesterday", "From the future"}   # filter uses the shown date


def test_migration_0011_clears_bad_dates(tmp_path):
    from pathlib import Path
    src = (Path(__file__).resolve().parent.parent / "alembic" / "versions" / "0011_clear_impossible_post_dates.py").read_text()
    assert 'down_revision: Union[str, None] = "0010"' in src and "posted_at = NULL" in src and "datetime(2015, 1, 1" in src
