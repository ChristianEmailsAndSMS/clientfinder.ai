"""Is the database schema the one this code expects?

The server is updated by hand (git pull, then `alembic upgrade head`, then restart). If the pull happens and the migration is
skipped, the first query that touches a new column fails with a 500. This module compares the revision stored in the database
(`alembic_version`) with the newest revision shipped in `alembic/versions`, so the API can say so at startup, in /health
and on the admin dashboard instead of failing on a customer's request."""
from __future__ import annotations

import logging
from pathlib import Path

from sqlalchemy import text
from sqlalchemy.engine import Engine

log = logging.getLogger(__name__)
_ALEMBIC_DIR = Path(__file__).resolve().parent.parent / "alembic"
FIX = "cd /root/clientfinder.ai/backend && .venv/bin/alembic upgrade head && systemctl restart clientfinder-api clientfinder-scheduler"


def code_head() -> str | None:
    """The single newest migration revision in the code (None if the alembic folder is missing or has several heads)."""
    from alembic.script import ScriptDirectory
    try:
        heads = ScriptDirectory(str(_ALEMBIC_DIR)).get_heads()
    except Exception:                                      # pragma: no cover - broken checkout
        log.exception("could not read the alembic scripts")
        return None
    return heads[0] if len(heads) == 1 else None


def check(engine: Engine) -> dict:
    """{"state": ok | behind | unversioned | unreachable | unknown, "current": rev|None, "head": rev|None, "fix": str|None}.

    behind      = the database revision is not the code's head (migration not applied, or database newer than code)
    unversioned = no alembic_version table: expected on SQLite (tests, local dev), a failure on Postgres (never migrated)
    unreachable = could not ask the database; this says nothing about the schema, so callers must not treat it as behind"""
    head = code_head()
    out = {"state": "unknown", "current": None, "head": head, "fix": None}
    try:
        with engine.connect() as c:
            try:
                rows = c.execute(text("SELECT version_num FROM alembic_version")).scalars().all()
            except Exception:
                c.rollback()
                out["state"] = "unversioned" if engine.dialect.name == "sqlite" else "behind"
                out["fix"] = None if out["state"] == "unversioned" else FIX
                return out
    except Exception as e:
        out["state"] = "unreachable"
        out["error"] = type(e).__name__
        return out
    out["current"] = rows[0] if len(rows) == 1 else ",".join(sorted(rows)) or None
    if head is None:
        return out
    out["state"] = "ok" if rows == [head] else "behind"
    out["fix"] = None if out["state"] == "ok" else FIX
    return out


def require_current(engine: Engine, strict: bool) -> dict:
    """Startup guard. Logs one CRITICAL line that says what to run; with strict=True also refuses to start."""
    r = check(engine)
    if r["state"] == "behind":
        msg = (f"DATABASE SCHEMA IS NOT CURRENT: database is at {r['current'] or 'no revision'}, code expects {r['head']}. "
               f"Run: {FIX}")
        log.critical(msg)
        if strict:
            raise RuntimeError(msg)
    elif r["state"] == "unreachable":
        log.warning("schema check skipped: database not reachable (%s)", r.get("error"))
    return r
