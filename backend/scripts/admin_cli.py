#!/usr/bin/env python3
"""Server-side admin tools. Needs shell access to the server, which is the whole point: only someone who can run this
can create or recover an admin account.

    python scripts/admin_cli.py setup-code [--email E] [--minutes 30]   one-time code for the "Create your admin account" screen
    python scripts/admin_cli.py list-admins
    python scripts/admin_cli.py unlock EMAIL                            clear a login lockout
    python scripts/admin_cli.py reset-admin EMAIL                       lost phone / locked out: disable the account and issue a new setup code
"""
from __future__ import annotations

import argparse
import sys
from datetime import timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from sqlalchemy import select, update  # noqa: E402

from app.config import settings  # noqa: E402
from app.db import session_scope  # noqa: E402
from app.models import SetupCode, User  # noqa: E402
from app.security import admin_emails, hash_setup_code, new_setup_code, secrets_ready, utcnow  # noqa: E402


def issue_code(email: str, minutes: int) -> str:
    code = new_setup_code()
    with session_scope() as db:
        # one live code per email: older unused ones are burned
        db.execute(update(SetupCode).where(SetupCode.email == email, SetupCode.used_at.is_(None)).values(used_at=utcnow()))
        db.add(SetupCode(email=email, code_hash=hash_setup_code(code), expires_at=utcnow() + timedelta(minutes=minutes)))
    return code


def cmd_setup_code(args) -> int:
    email = (args.email or sorted(admin_emails())[0]).strip().lower()
    if email not in admin_emails():
        print(f"{email} is not in ADMIN_EMAILS ({', '.join(sorted(admin_emails()))}). Add it to .env first.")
        return 1
    ok, why = secrets_ready()
    if not ok:
        print(f"WARNING: sign-in is disabled until this is fixed: {why}\n"
              f"  Add JWT_SECRET to .env (python3 -c \"import secrets; print(secrets.token_urlsafe(48))\") and restart the API.\n")
    with session_scope() as db:
        existing = db.scalar(select(User).where(User.email == email))
        if existing and existing.is_active and existing.totp_enabled:
            print(f"{email} already has an active account. To recover it use: admin_cli.py reset-admin {email}")
            return 1
    code = issue_code(email, args.minutes)
    print(f"Setup code for {email} (valid {args.minutes} min, works once):\n\n    {code}\n")
    print("Open https://clientfinder.ai/admin and use the 'Create your admin account' screen.")
    return 0


def cmd_list(args) -> int:
    with session_scope() as db:
        rows = db.execute(select(User.email, User.is_active, User.totp_enabled, User.last_login_at, User.locked_until)
                          .where(User.is_admin.is_(True))).all()
    for email, active, totp, last, locked in rows or []:
        print(f"{email}  active={active} 2fa={totp} last_login={last} locked_until={locked}")
    if not rows:
        print("No admin accounts yet. Run: admin_cli.py setup-code")
    return 0


def cmd_unlock(args) -> int:
    with session_scope() as db:
        n = db.execute(update(User).where(User.email == args.email.strip().lower()).values(locked_until=None, failed_logins=0)).rowcount
    print("Unlocked." if n else "No such user.")
    return 0 if n else 1


def cmd_reset(args) -> int:
    email = args.email.strip().lower()
    if email not in admin_emails():
        print(f"{email} is not in ADMIN_EMAILS.")
        return 1
    with session_scope() as db:
        u = db.scalar(select(User).where(User.email == email))
        if u is None:
            print("No such account; use setup-code instead.")
            return 1
        u.is_active, u.totp_enabled, u.totp_secret_enc, u.totp_last_step = False, False, None, None
        u.session_version += 1                                   # ends every open session
        u.locked_until, u.failed_logins = None, 0
    code = issue_code(email, 30)
    print(f"{email} disabled and signed out everywhere.\nNew setup code (30 min, once):\n\n    {code}\n")
    print("Use it on https://clientfinder.ai/admin to set a new password and a new 2FA device.")
    return 0


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True)
    p = sub.add_parser("setup-code"); p.add_argument("--email"); p.add_argument("--minutes", type=int, default=30); p.set_defaults(fn=cmd_setup_code)
    sub.add_parser("list-admins").set_defaults(fn=cmd_list)
    p = sub.add_parser("unlock"); p.add_argument("email"); p.set_defaults(fn=cmd_unlock)
    p = sub.add_parser("reset-admin"); p.add_argument("email"); p.set_defaults(fn=cmd_reset)
    args = ap.parse_args()
    return args.fn(args)


if __name__ == "__main__":
    raise SystemExit(main())
