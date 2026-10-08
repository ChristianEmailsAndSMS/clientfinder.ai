#!/usr/bin/env python3
"""Server-side admin tools. They need shell access to the server, which is the point: only someone who can run this can
create or recover an admin account.

    python scripts/admin_cli.py create-admin [EMAIL]      create the owner account (prompts for a password; EMAIL must be in ADMIN_EMAILS)
    python scripts/admin_cli.py reset-admin  [EMAIL]      lost password or phone: new password, 2FA cleared, all sessions ended
    python scripts/admin_cli.py list-admins
    python scripts/admin_cli.py unlock EMAIL              clear a login lockout
    python scripts/admin_cli.py grant EMAIL AMOUNT_USD    add credit to any account (also available in the dashboard)

After create-admin, sign in at https://clientfinder.ai/login. The first sign-in shows a QR code to turn on 2FA."""
from __future__ import annotations

import argparse
import getpass
import sys
from decimal import Decimal
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from sqlalchemy import select, update  # noqa: E402

from app import credits  # noqa: E402
from app.db import session_scope  # noqa: E402
from app.models import User  # noqa: E402
from app.security import admin_emails, hash_password, password_problem, secrets_ready  # noqa: E402


def _email(arg: str | None) -> str | None:
    email = (arg or (sorted(admin_emails())[0] if admin_emails() else "")).strip().lower()
    if email not in admin_emails():
        print(f"{email or '(none)'} is not in ADMIN_EMAILS ({', '.join(sorted(admin_emails())) or 'empty'}). Add it to .env first.")
        return None
    return email


def _ask_password(email: str) -> str | None:
    for _ in range(3):
        pw = getpass.getpass("New password (14+ characters, not shown): ")
        problem = password_problem(pw, email, admin=True)
        if problem:
            print(f"  {problem}")
            continue
        if getpass.getpass("Repeat password: ") != pw:
            print("  The two passwords did not match.")
            continue
        return pw
    print("Giving up after 3 tries.")
    return None


def _warn_secrets() -> None:
    ok, why = secrets_ready()
    if not ok:
        print(f"WARNING: sign-in is disabled until this is fixed: {why}\n"
              f"  Add JWT_SECRET to .env (python3 -c \"import secrets; print(secrets.token_urlsafe(48))\") and restart the API.\n")


def cmd_create(args) -> int:
    email = _email(args.email)
    if not email:
        return 1
    with session_scope() as db:
        existing = db.scalar(select(User).where(User.email == email))
        if existing:
            print(f"{email} already has an account. If you lost the password or phone use: admin_cli.py reset-admin {email}")
            return 1
    pw = _ask_password(email)
    if not pw:
        return 1
    with session_scope() as db:
        db.add(User(email=email, password_hash=hash_password(pw), is_admin=True, is_active=True, totp_enabled=False))
    _warn_secrets()
    print(f"Created admin account {email}.\nNow sign in at https://clientfinder.ai/login. The first sign-in will show a QR code to turn on 2FA.")
    return 0


def cmd_reset(args) -> int:
    email = _email(args.email)
    if not email:
        return 1
    with session_scope() as db:
        if db.scalar(select(User.id).where(User.email == email)) is None:
            print("No such account; use create-admin instead.")
            return 1
    pw = _ask_password(email)
    if not pw:
        return 1
    with session_scope() as db:
        u = db.scalar(select(User).where(User.email == email))
        u.password_hash, u.is_admin, u.is_active = hash_password(pw), True, True
        u.totp_enabled, u.totp_secret_enc, u.totp_last_step = False, None, None
        u.session_version += 1                                   # ends every open session
        u.locked_until, u.failed_logins = None, 0
    _warn_secrets()
    print(f"{email}: password changed, 2FA cleared, signed out everywhere.\nSign in at https://clientfinder.ai/login and scan the new QR code.")
    return 0


def cmd_list(args) -> int:
    with session_scope() as db:
        rows = db.execute(select(User.email, User.is_active, User.totp_enabled, User.last_login_at, User.locked_until)
                          .where(User.is_admin.is_(True))).all()
    for email, active, totp, last, locked in rows:
        print(f"{email}  active={active} 2fa={totp} last_login={last} locked_until={locked}")
    if not rows:
        print("No admin accounts yet. Run: admin_cli.py create-admin")
    return 0


def cmd_unlock(args) -> int:
    with session_scope() as db:
        n = db.execute(update(User).where(User.email == args.email.strip().lower()).values(locked_until=None, failed_logins=0)).rowcount
    print("Unlocked." if n else "No such user.")
    return 0 if n else 1


def cmd_grant(args) -> int:
    try:
        micro = credits.usd_to_micro(Decimal(args.amount))
    except Exception as e:
        print(f"Bad amount: {e}")
        return 1
    if micro <= 0:
        print("Amount must be positive.")
        return 1
    with session_scope() as db:
        u = db.scalar(select(User).where(User.email == args.email.strip().lower()))
        if not u:
            print("No such user.")
            return 1
        credits.apply(db, u.id, micro, "grant", f"CLI grant: {args.reason}")
        print(f"{u.email}: balance now ${credits.micro_to_usd(credits.balance(db, u.id)):,.4f}")
    return 0


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True)
    for name, fn in (("create-admin", cmd_create), ("reset-admin", cmd_reset)):
        p = sub.add_parser(name); p.add_argument("email", nargs="?"); p.set_defaults(fn=fn)
    sub.add_parser("list-admins").set_defaults(fn=cmd_list)
    p = sub.add_parser("unlock"); p.add_argument("email"); p.set_defaults(fn=cmd_unlock)
    p = sub.add_parser("grant"); p.add_argument("email"); p.add_argument("amount"); p.add_argument("--reason", default="manual"); p.set_defaults(fn=cmd_grant)
    args = ap.parse_args()
    return args.fn(args)


if __name__ == "__main__":
    raise SystemExit(main())
