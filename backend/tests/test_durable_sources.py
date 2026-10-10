"""Durable scrapers (RemoteOK, ProBlogger, Greenhouse) against SQLite.

Fetch helpers are monkeypatched. No network and no Postgres.
"""
from __future__ import annotations

from contextlib import contextmanager
from datetime import datetime, timezone

import pytest
from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session, sessionmaker
from sqlalchemy.pool import StaticPool

from app.db import Base
from app.models import Job, ScrapeRun, Source
from app.scrapers import greenhouse, problogger, remoteok


class SqliteDB:
    def __init__(self) -> None:
        self.engine = create_engine(
            "sqlite://",
            connect_args={"check_same_thread": False},
            poolclass=StaticPool,
        )
        Base.metadata.create_all(self.engine)

    def session_factory(self, session_cls: type[Session] | None = None):
        return sessionmaker(
            bind=self.engine,
            class_=session_cls or Session,
            autocommit=False,
            autoflush=False,
            future=True,
            expire_on_commit=False,
        )

    def install(self, monkeypatch, session_cls: type[Session] | None = None):
        factory = self.session_factory(session_cls)

        @contextmanager
        def session_scope():
            db = factory()
            try:
                yield db
                db.commit()
            except Exception:
                db.rollback()
                raise
            finally:
                db.close()

        for mod in (remoteok, problogger, greenhouse):
            monkeypatch.setattr(mod, "session_scope", session_scope)
        return factory


@pytest.fixture
def sqlite_db(monkeypatch):
    db = SqliteDB()
    db.install(monkeypatch)
    return db


def _rows(factory, model):
    db = factory()
    try:
        return list(db.scalars(select(model).order_by(model.id)).all())
    finally:
        db.close()


def _patch_clock(monkeypatch, module):
    """Each datetime.now() call is one second later than the previous.

    datetime.now is read-only on Python 3.12, so replace the name the
    scraper imported rather than patching the method in place.
    """
    real = module.datetime
    ticks = {"n": 0}

    class _Clock:
        @staticmethod
        def now(tz=None):
            ticks["n"] += 1
            return datetime(2026, 6, 1, 12, 0, ticks["n"], tzinfo=timezone.utc)

        @staticmethod
        def fromisoformat(value):
            return real.fromisoformat(value)

    monkeypatch.setattr(module, "datetime", _Clock)
    return ticks


def _remoteok_row(**overrides) -> dict:
    row = {
        "position": "Email copywriter",
        "company": "Acme",
        "url": "https://remoteok.com/remote-jobs/1",
        "description": "email marketing and copywriting",
        "tags": ["copywriting"],
        "date": "2026-01-15T00:00:00Z",
        "location": "Worldwide",
    }
    row.update(overrides)
    return row


def test_remoteok_string_salaries_do_not_abort_and_are_numeric(sqlite_db, monkeypatch):
    rows = [
        _remoteok_row(
            url="https://remoteok.com/remote-jobs/1",
            salary_min="80000",
            salary_max="120000",
        ),
        _remoteok_row(
            url="https://remoteok.com/remote-jobs/2",
            position="Copywriter, lifecycle",
            salary_min="negotiable",
            salary_max="nope",
        ),
        _remoteok_row(
            url="https://remoteok.com/remote-jobs/3",
            position="Landing page copywriter",
            salary_min=50000,
            salary_max=60000,
        ),
        _remoteok_row(
            url="https://remoteok.com/remote-jobs/4",
            position="Growth marketing lead",
        ),
    ]
    monkeypatch.setattr(remoteok, "fetch_jobs", lambda: rows)

    stats = remoteok.run()

    assert stats["added"] == 4
    assert stats.get("errors", 0) == 0
    jobs = {job.source_url: job for job in _rows(sqlite_db.session_factory(), Job)}
    paid = jobs["https://remoteok.com/remote-jobs/1"]
    assert paid.pay_min == 80000
    assert paid.pay_max == 120000
    assert isinstance(paid.pay_min, float)
    assert isinstance(paid.pay_max, float)
    assert paid.pay_text == "$80,000-$120,000"
    assert paid.pay_period == "year"

    unpaid = jobs["https://remoteok.com/remote-jobs/2"]
    assert unpaid.pay_min is None
    assert unpaid.pay_max is None
    assert unpaid.pay_text is None
    assert unpaid.pay_period is None

    ints = jobs["https://remoteok.com/remote-jobs/3"]
    assert ints.pay_min == 50000
    assert ints.pay_text == "$50,000-$60,000"

    missing = jobs["https://remoteok.com/remote-jobs/4"]
    assert missing.pay_text is None
    assert missing.pay_period == "year"

    run = _rows(sqlite_db.session_factory(), ScrapeRun)[0]
    assert run.status == "ok"


def test_remoteok_truncates_columns_before_insert(sqlite_db, monkeypatch):
    monkeypatch.setattr(
        remoteok,
        "fetch_jobs",
        lambda: [
            _remoteok_row(
                position="Email copywriter " + ("T" * 600),
                company="C" * 300,
                location="L" * 200,
                description="D" * 5000,
                salary_min=1000,
                salary_max=2000,
            )
        ],
    )

    stats = remoteok.run()

    assert stats["added"] == 1
    assert stats.get("errors", 0) == 0
    job = _rows(sqlite_db.session_factory(), Job)[0]
    assert len(job.title) == 512
    assert job.title.startswith("Email copywriter ")
    assert len(job.company_or_poster) == 256
    assert len(job.location) == 128
    assert len(job.raw_snippet) == 200
    assert len(job.description) == 4000
    assert len(job.pay_text) <= 128


def test_remoteok_row_error_does_not_abort_batch(sqlite_db, monkeypatch):
    class BoomSession(Session):
        def flush(self, objects=None):
            for obj in list(self.new):
                if isinstance(obj, Job) and str(obj.source_url).endswith("/bad"):
                    raise RuntimeError("value too long for column")
            return super().flush(objects)

    sqlite_db.install(monkeypatch, BoomSession)
    monkeypatch.setattr(
        remoteok,
        "fetch_jobs",
        lambda: [
            _remoteok_row(url="https://remoteok.com/remote-jobs/bad", salary_min=1, salary_max=2),
            _remoteok_row(url="https://remoteok.com/remote-jobs/good", position="Funnel copywriter"),
        ],
    )

    stats = remoteok.run()

    assert stats["added"] == 1
    assert stats["errors"] == 1
    jobs = _rows(sqlite_db.session_factory(), Job)
    assert [job.source_url for job in jobs] == ["https://remoteok.com/remote-jobs/good"]
    run = _rows(sqlite_db.session_factory(), ScrapeRun)[0]
    assert run.status == "failed"
    assert run.error


def test_remoteok_duplicate_hash_counts_as_updated(sqlite_db, monkeypatch):
    row = _remoteok_row(salary_min=10, salary_max=20)
    monkeypatch.setattr(remoteok, "fetch_jobs", lambda: [row])
    first = remoteok.run()
    assert first["added"] == 1

    class HideJobs(Session):
        def scalar(self, statement, *args, **kwargs):
            text = str(statement).lower()
            if "from jobs" in text and "dedupe_hash" in text:
                return None
            return super().scalar(statement, *args, **kwargs)

    sqlite_db.install(monkeypatch, HideJobs)
    second = remoteok.run()

    assert second["added"] == 0
    assert second["updated"] == 1
    assert second.get("errors", 0) == 0
    runs = _rows(sqlite_db.session_factory(), ScrapeRun)
    assert len(runs) == 2
    assert runs[-1].status == "ok"
    assert len(_rows(sqlite_db.session_factory(), Job)) == 1


def test_remoteok_fetch_failure_persists_failed_run(sqlite_db, monkeypatch):
    def boom():
        raise RuntimeError("remoteok down")

    monkeypatch.setattr(remoteok, "fetch_jobs", boom)

    stats = remoteok.run()

    assert "remoteok down" in stats.get("error", "")
    sources = _rows(sqlite_db.session_factory(), Source)
    assert len(sources) == 1
    assert sources[0].key == "remoteok"
    runs = _rows(sqlite_db.session_factory(), ScrapeRun)
    assert len(runs) == 1
    assert runs[0].status == "failed"
    assert runs[0].source_id == sources[0].id
    assert "remoteok down" in (runs[0].error or "")
    assert runs[0].finished_at is not None
    assert _rows(sqlite_db.session_factory(), Job) == []


def test_problogger_relative_href_becomes_absolute(sqlite_db, monkeypatch):
    monkeypatch.setattr(
        problogger,
        "fetch_listings",
        lambda: [
            {"url": "/jobs/some-role", "title": "Landing page writer", "snippet": "contract"},
            {
                "url": "https://example.com/already-absolute",
                "title": "Email copywriter",
                "snippet": "S" * 5000,
            },
        ],
    )

    stats = problogger.run()

    assert stats["added"] == 2
    assert stats.get("errors", 0) == 0
    jobs = {job.title: job for job in _rows(sqlite_db.session_factory(), Job)}
    assert jobs["Landing page writer"].source_url == "https://problogger.com/jobs/some-role"
    assert jobs["Email copywriter"].source_url == "https://example.com/already-absolute"
    assert len(jobs["Email copywriter"].title) <= 512
    assert len(jobs["Email copywriter"].raw_snippet) == 200
    assert len(jobs["Email copywriter"].description) == 4000
    run = _rows(sqlite_db.session_factory(), ScrapeRun)[0]
    assert run.status == "ok"


def test_problogger_fetch_failure_persists_failed_run(sqlite_db, monkeypatch):
    def boom():
        raise RuntimeError("problogger down")

    monkeypatch.setattr(problogger, "fetch_listings", boom)

    stats = problogger.run()

    assert "problogger down" in stats.get("error", "")
    sources = _rows(sqlite_db.session_factory(), Source)
    assert [source.key for source in sources] == ["problogger"]
    runs = _rows(sqlite_db.session_factory(), ScrapeRun)
    assert len(runs) == 1
    assert runs[0].status == "failed"
    assert "problogger down" in (runs[0].error or "")
    assert runs[0].finished_at is not None


def test_greenhouse_html_description_is_plain_text(sqlite_db, monkeypatch):
    long_copy = "email marketing " + ("word " * 2000)
    monkeypatch.setattr(greenhouse, "GREENHOUSE_COMPANIES", ("acme",))

    def fetch_board(company: str):
        assert company == "acme"
        return [
            {
                "title": "Lifecycle Marketing Manager",
                "absolute_url": "https://boards.greenhouse.io/acme/jobs/1",
                "location": {"name": "Remote — " + ("L" * 200)},
                "updated_at": "2026-02-01T00:00:00Z",
                "content": f"<p>Hello <b>world</b></p>\n<p>Email   marketing</p><div>{long_copy}</div>",
            },
            {
                "title": "Email copywriter",
                "absolute_url": "https://boards.greenhouse.io/acme/jobs/2",
                "content": "<p></p>",
                "location": {"name": "NYC"},
            },
            {
                "title": "Creative strategist",
                "absolute_url": "https://boards.greenhouse.io/acme/jobs/3",
                "content": "   ",
            },
        ]

    monkeypatch.setattr(greenhouse, "fetch_board", fetch_board)

    stats = greenhouse.run()

    assert stats["added"] == 3
    assert stats.get("errors", 0) == 0
    jobs = {job.source_url: job for job in _rows(sqlite_db.session_factory(), Job)}
    description = jobs["https://boards.greenhouse.io/acme/jobs/1"].description
    assert description is not None
    assert "<" not in description and ">" not in description
    assert "Hello world" in description
    assert "Email marketing" in description
    assert "  " not in description
    assert "\n" not in description
    assert len(description) == 4000
    assert len(jobs["https://boards.greenhouse.io/acme/jobs/1"].location) == 128
    assert jobs["https://boards.greenhouse.io/acme/jobs/2"].description is None
    assert jobs["https://boards.greenhouse.io/acme/jobs/3"].description is None
    run = _rows(sqlite_db.session_factory(), ScrapeRun)[0]
    assert run.status == "ok"


def test_greenhouse_company_fetch_error_does_not_abort_others(sqlite_db, monkeypatch):
    monkeypatch.setattr(greenhouse, "GREENHOUSE_COMPANIES", ("broken", "acme"))

    def fetch_board(company: str):
        if company == "broken":
            raise RuntimeError("board down")
        return [
            {
                "title": "CRM manager",
                "absolute_url": "https://boards.greenhouse.io/acme/jobs/9",
                "content": "<p>Own the lifecycle.</p>",
            }
        ]

    monkeypatch.setattr(greenhouse, "fetch_board", fetch_board)

    stats = greenhouse.run()

    assert stats["added"] == 1
    assert stats["errors"] == 1
    jobs = _rows(sqlite_db.session_factory(), Job)
    assert len(jobs) == 1
    assert jobs[0].description == "Own the lifecycle."
    run = _rows(sqlite_db.session_factory(), ScrapeRun)[0]
    assert run.status == "failed"
    assert run.error
    assert "1" in run.error


@pytest.mark.parametrize(
    "module, fetch_name, payload",
    [
        (
            remoteok,
            "fetch_jobs",
            [_remoteok_row(salary_min=1000, salary_max=2000)],
        ),
        (
            problogger,
            "fetch_listings",
            [{"url": "/jobs/some-role", "title": "Copywriter", "snippet": "short"}],
        ),
        (
            greenhouse,
            "fetch_board",
            None,
        ),
    ],
)
def test_finished_at_is_after_the_row_timestamp(sqlite_db, monkeypatch, module, fetch_name, payload):
    _patch_clock(monkeypatch, module)
    if module is greenhouse:
        monkeypatch.setattr(greenhouse, "GREENHOUSE_COMPANIES", ("acme",))

        def fetch_board(_company: str):
            return [
                {
                    "title": "Email marketing",
                    "absolute_url": "https://boards.greenhouse.io/acme/jobs/1",
                    "content": "<p>Hi</p>",
                }
            ]

        monkeypatch.setattr(greenhouse, "fetch_board", fetch_board)
    else:
        monkeypatch.setattr(module, fetch_name, lambda: payload)

    stats = module.run()

    assert stats["added"] == 1
    assert stats.get("errors", 0) == 0
    job = _rows(sqlite_db.session_factory(), Job)[0]
    run = _rows(sqlite_db.session_factory(), ScrapeRun)[0]
    assert run.status == "ok"
    assert run.finished_at is not None
    assert job.first_seen_at is not None
    assert run.finished_at > job.first_seen_at
