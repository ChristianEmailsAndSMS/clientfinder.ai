"""The off-server backup script, run for real against a stand-in `rclone`, and the dashboard's view of its status."""
import json
import os
import subprocess
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from app.api.admin import offsite_info

ROOT = Path(__file__).resolve().parent.parent.parent
SCRIPT = ROOT / "deploy" / "offsite_backup.sh"
STUB = """#!/usr/bin/env bash
echo "$@" >> "$STUB_LOG"
case "$1" in
  listremotes) [[ -n "${STUB_NO_REMOTE:-}" ]] || echo "cfcrypt:" ;;
  copy)   exit "${STUB_COPY_RC:-0}" ;;
  check)  exit "${STUB_CHECK_RC:-0}" ;;
  copyto) exit "${STUB_COPYTO_RC:-0}" ;;
esac
exit 0
"""


@pytest.fixture
def box(tmp_path):
    (tmp_path / "bk").mkdir()
    (tmp_path / "bk" / "clientfinder-2026-10-12_033000.dump").write_bytes(b"dump")
    (tmp_path / ".env").write_text("SECRET=1\n")
    stub = tmp_path / "rclone"; stub.write_text(STUB); stub.chmod(0o755)
    env = {**os.environ, "BACKUP_DIR": str(tmp_path / "bk"), "ENV_FILE": str(tmp_path / ".env"), "RCLONE_BIN": str(stub),
           "STUB_LOG": str(tmp_path / "calls.log")}
    for k in ("STUB_NO_REMOTE", "STUB_COPY_RC", "STUB_CHECK_RC", "STUB_COPYTO_RC"):
        env.pop(k, None)
    return tmp_path, env


def run(env):
    return subprocess.run(["bash", str(SCRIPT)], env=env, capture_output=True, text=True, timeout=30)


def status(tmp):
    return json.loads((tmp / "bk" / "offsite.json").read_text())


def test_a_good_run_copies_dumps_and_env_verifies_and_records_success(box):
    tmp, env = box
    r = run(env)
    assert r.returncode == 0, r.stderr
    st = status(tmp)
    assert st["ok"] is True and st["detail"] == "clientfinder-2026-10-12_033000.dump"
    calls = (tmp / "calls.log").read_text()
    assert "copy " + str(tmp / "bk") + " cfcrypt:db" in calls and "check " in calls and "--size-only" in calls
    assert "copyto " + str(tmp / ".env") + " cfcrypt:env/env-" in calls
    assert "delete cfcrypt:db --min-age 60d" in calls                                  # retention
    assert (tmp / "bk" / "offsite.json").stat().st_mode & 0o077 == 0                   # private


@pytest.mark.parametrize("var,why", [("STUB_NO_REMOTE", "not configured"), ("STUB_COPY_RC", "copy of the dumps failed"),
                                     ("STUB_CHECK_RC", "verification failed"), ("STUB_COPYTO_RC", "copy of .env failed")])
def test_every_failure_is_loud_and_recorded(box, var, why):
    tmp, env = box
    env[var] = "1"
    r = run(env)
    assert r.returncode == 1 and why in r.stderr
    st = status(tmp)
    assert st["ok"] is False and why.split()[0] in st["detail"]


def test_no_local_dump_is_a_failure_not_a_silent_success(box):
    tmp, env = box
    for f in (tmp / "bk").glob("*.dump"):
        f.unlink()
    r = run(env)
    assert r.returncode == 1 and "no local backup" in r.stderr and status(tmp)["ok"] is False


def test_missing_env_file_still_backs_up_the_database_but_says_so(box):
    tmp, env = box
    (tmp / ".env").unlink()
    r = run(env)
    assert r.returncode == 0 and ".env" in r.stderr and "NOT copied" in r.stderr
    assert "copyto" not in (tmp / "calls.log").read_text()


def test_missing_rclone_is_reported(box):
    tmp, env = box
    env["RCLONE_BIN"] = str(tmp / "no-such-rclone")
    assert run(env).returncode == 1 and "not installed" in status(tmp)["detail"]


def test_dashboard_states_for_the_offsite_copy(tmp_path):
    now = datetime.now(timezone.utc)
    def write(ok, hours_ago):
        (tmp_path / "offsite.json").write_text(json.dumps({"ts": (now - timedelta(hours=hours_ago)).strftime("%Y-%m-%dT%H:%M:%SZ"), "ok": ok, "detail": "x"}))
    assert offsite_info(tmp_path, now.timestamp())["status"] == "none"
    write(True, 5); assert offsite_info(tmp_path, now.timestamp())["status"] == "ok"
    write(True, 40); assert offsite_info(tmp_path, now.timestamp())["status"] == "stale"
    write(False, 1); assert offsite_info(tmp_path, now.timestamp())["status"] == "failed"
    (tmp_path / "offsite.json").write_text("not json"); assert offsite_info(tmp_path, now.timestamp())["status"] == "none"


def test_systemd_units_are_consistent_with_the_script():
    svc = (ROOT / "deploy" / "clientfinder-offsite.service").read_text()
    assert "ExecStart=/root/clientfinder.ai/deploy/offsite_backup.sh" in svc and "After=clientfinder-backup.service" in svc
    assert "OnCalendar=*-*-* 04:15:00 UTC" in (ROOT / "deploy" / "clientfinder-offsite.timer").read_text()
