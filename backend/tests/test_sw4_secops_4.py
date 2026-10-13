"""admin_cli.py reset-user must work for a customer (who is not in ADMIN_EMAILS)."""
import importlib.util
from argparse import Namespace
from pathlib import Path

from sqlalchemy import select

from app.models import User
from app.security import verify_password
from test_customer_api import env, member  # noqa: F401  (fixtures)

PW = "a-long-new-password-123"


def _cli():
    path = Path(__file__).resolve().parent.parent / "scripts" / "admin_cli.py"
    spec = importlib.util.spec_from_file_location("admin_cli_sw4", path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def test_reset_user_changes_a_customer_password(env, monkeypatch):
    member(env, "cust1@example.com")
    cli = _cli()
    monkeypatch.setattr(cli, "session_scope", env.scope)
    monkeypatch.setattr(cli.getpass, "getpass", lambda *a, **k: PW)
    with env.scope() as db:
        before = db.scalar(select(User).where(User.email == "cust1@example.com")).session_version
    assert cli.cmd_reset_user(Namespace(email=" Cust1@Example.com ")) == 0
    with env.scope() as db:
        u = db.scalar(select(User).where(User.email == "cust1@example.com"))
        assert verify_password(u.password_hash, PW)
        assert not u.is_admin
        assert u.session_version == before + 1


def test_reset_user_needs_an_address(env, monkeypatch):
    cli = _cli()
    monkeypatch.setattr(cli, "session_scope", env.scope)
    assert cli.cmd_reset_user(Namespace(email=None)) == 1
