"""Docs/config truth: every .env.example setting is read by the app, CHECKLIST quotes the real migration range."""
import re
from pathlib import Path

from dotenv import dotenv_values

BACKEND = Path(__file__).resolve().parent.parent
ROOT = BACKEND.parent


def test_every_env_example_setting_is_read_by_app_code():
    src = "\n".join(p.read_text() for d in ("app", "alembic") for p in (BACKEND / d).rglob("*.py") if p.name != "config.py")
    dead = [k for k in dotenv_values(BACKEND / ".env.example")
            if not re.search(r"\b(settings|s|cfg|get_settings\(\))\.%s\b|\b%s\b" % (k.lower(), k.lower()), src)
            and k not in {"DATABASE_URL", "RESEND_API_KEY", "EMAIL_FROM"}]   # email: documented "nothing is sent yet"
    assert not dead, f"settings that nothing in app/ reads: {dead}"


def test_checklist_migration_range_matches_alembic():
    nums = sorted(int(m.group(1)) for p in (BACKEND / "alembic/versions").glob("*.py")
                  if (m := re.match(r"(\d{4})_", p.name)))
    text = (ROOT / "CHECKLIST.md").read_text()
    quoted = re.findall(r"migrations 0001-(\d{4})", text)
    assert quoted and all(int(q) == nums[-1] for q in quoted), (quoted, nums[-1])


def test_docs_do_not_claim_one_time_admin_code_or_charging_failed_calls():
    assert "one-time code" not in (BACKEND / ".env.example").read_text()
    assert "Charge for failed" not in (ROOT / "CHECKLIST.md").read_text()
