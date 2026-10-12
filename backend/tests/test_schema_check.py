"""The schema guard: a migration skipped after `git pull` must be loud (log, /health, admin), not a 500 on first request."""
import logging
import os
from pathlib import Path

import pytest
from sqlalchemy import create_engine, text
from sqlalchemy.pool import StaticPool

import authkit
from app import schema_check
from app.db import Base
from app.main import app, lifespan


def _engine(version=None):
    e = create_engine("sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool)
    if version is not None:
        with e.begin() as c:
            c.execute(text("CREATE TABLE alembic_version (version_num VARCHAR(32) NOT NULL PRIMARY KEY)"))
            c.execute(text("INSERT INTO alembic_version VALUES (:v)"), {"v": version})
    return e


def test_code_has_exactly_one_head_and_a_linear_chain():
    from alembic.script import ScriptDirectory
    sd = ScriptDirectory(str(Path(schema_check._ALEMBIC_DIR)))
    revs = list(sd.walk_revisions())
    assert len(sd.get_heads()) == 1
    assert all(r.down_revision is None or isinstance(r.down_revision, str) for r in revs)       # no merge revisions
    assert len({r.down_revision for r in revs}) == len(revs)                  # nobody shares a parent: no branches
    assert schema_check.code_head() == sd.get_heads()[0]


def test_states():
    head = schema_check.code_head()
    assert schema_check.check(_engine(head))["state"] == "ok"
    old = schema_check.check(_engine("0001"))
    assert old["state"] == "behind" and old["current"] == "0001" and old["head"] == head and "alembic upgrade head" in old["fix"]
    assert schema_check.check(_engine())["state"] == "unversioned"             # sqlite without alembic (tests/dev): not an error
    dead = create_engine("postgresql+psycopg://x:y@127.0.0.1:1/none", connect_args={"connect_timeout": 1})
    assert schema_check.check(dead)["state"] == "unreachable"


def test_postgres_without_alembic_version_counts_as_behind(monkeypatch):
    e = _engine()
    monkeypatch.setattr(e.dialect, "name", "postgresql")
    assert schema_check.check(e)["state"] == "behind"


def test_require_current_strict_raises_and_logs(caplog):
    with caplog.at_level(logging.CRITICAL):
        with pytest.raises(RuntimeError, match="alembic upgrade head"):
            schema_check.require_current(_engine("0001"), strict=True)
    assert "NOT CURRENT" in caplog.text
    assert schema_check.require_current(_engine("0001"), strict=False)["state"] == "behind"     # escape hatch still logs
    assert schema_check.require_current(_engine(schema_check.code_head()), strict=True)["state"] == "ok"


def test_health_is_503_when_behind(monkeypatch):
    env = authkit.make_env(monkeypatch)
    import app.main as m
    monkeypatch.setattr(m, "engine", _engine("0001"))
    r = env.client.get("/health")
    assert r.status_code == 503 and r.json() == {"ok": False, "service": "clientfinder.ai", "schema": "behind"}
    assert "0001" not in r.text                                                  # revisions are not public
    monkeypatch.setattr(m, "engine", _engine(schema_check.code_head()))
    assert env.client.get("/health").status_code == 200
    monkeypatch.setattr(m, "engine", _engine())
    assert env.client.get("/health").json()["ok"] is True


def test_startup_refuses_when_behind(monkeypatch):
    import asyncio
    import app.main as m
    from app.config import settings
    monkeypatch.setattr(m, "engine", _engine("0001"))
    monkeypatch.setattr(settings, "schema_check_strict", True)

    async def go():
        async with lifespan(app):
            pass
    with pytest.raises(RuntimeError, match="NOT CURRENT|not current|alembic"):
        asyncio.run(go())
    monkeypatch.setattr(settings, "schema_check_strict", False)
    asyncio.run(go())


def test_admin_overview_reports_schema(monkeypatch):
    env = authkit.make_env(monkeypatch)
    with env.scope() as db:
        _, secret = authkit.make_user(db, "christian@emailsandsms.com", admin=True)
    assert authkit.login(env, env.client, "christian@emailsandsms.com", secret=secret).status_code == 200
    o = env.client.get("/admin/overview")
    assert o.status_code == 200 and o.json()["schema"]["state"] == "unversioned"      # sqlite test database
    from app.api import admin as admin_api
    monkeypatch.setattr(admin_api.schema_check, "check", lambda e: {"state": "behind", "current": "0007", "head": "0008", "fix": "alembic upgrade head"})
    assert env.client.get("/admin/overview").json()["schema"]["state"] == "behind"
    js = (Path(__file__).resolve().parent.parent / "app/static/admin/dashboard.js").read_text()
    assert "o.schema.state === \"behind\"" in js and "Database is out of date" in js


def test_model_indexes_match_migrations():
    """The jobs.region index is created by migration 0007; the model must declare it too (alembic drift)."""
    from app.models import Job
    assert "ix_jobs_region" in {i.name for i in Job.__table__.indexes}


@pytest.mark.skipif(not os.environ.get("TEST_PG_URL"), reason="set TEST_PG_URL to an empty throwaway Postgres database")
def test_models_match_migrations_on_postgres(monkeypatch):
    """alembic upgrade head on a real Postgres, then autogenerate must find nothing to change."""
    import subprocess, sys
    from alembic.autogenerate import compare_metadata
    from alembic.migration import MigrationContext
    env = dict(os.environ, DATABASE_URL=os.environ["TEST_PG_URL"], MIGRATION_DATABASE_URL="")
    root = Path(__file__).resolve().parent.parent
    subprocess.run([sys.executable, "-m", "alembic", "upgrade", "head"], cwd=root, env=env, check=True)
    e = create_engine(os.environ["TEST_PG_URL"])
    try:
        with e.connect() as c:
            diff = compare_metadata(MigrationContext.configure(c, opts={"compare_type": True}), Base.metadata)
        assert diff == []
        assert schema_check.check(e)["state"] == "ok"
    finally:
        subprocess.run([sys.executable, "-m", "alembic", "downgrade", "base"], cwd=root, env=env, check=True)


def test_scheduler_refuses_to_start_when_behind(monkeypatch):
    from app import scheduler, db
    from app.config import settings
    monkeypatch.setattr(db, "engine", _engine("0001"))
    monkeypatch.setattr(settings, "schema_check_strict", True)
    monkeypatch.setattr(scheduler, "build_scheduler", lambda: pytest.fail("scheduler must not start on a stale schema"))
    with pytest.raises(RuntimeError, match="alembic upgrade head"):
        scheduler.main()
