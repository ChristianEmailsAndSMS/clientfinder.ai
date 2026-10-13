"""The hand-run maintenance scripts must print usage for --help and must not touch data or the network."""
import os
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent


@pytest.mark.parametrize("script", ["backfill_tags.py", "recompute_platforms.py", "run_durable_sources.py"])
def test_help_prints_usage_and_does_nothing(script, tmp_path):
    env = dict(os.environ, DATABASE_URL=f"sqlite:///{tmp_path / 'none.db'}")
    env.pop("DEV_FIXTURES", None)
    r = subprocess.run([sys.executable, str(ROOT / "scripts" / script), "--help"], capture_output=True, text=True,
                       env=env, cwd=ROOT, timeout=60)
    assert r.returncode == 0, r.stderr
    assert "usage" in r.stdout.lower()
    assert "retagged" not in r.stdout and "Would change" not in r.stdout and '"source"' not in r.stdout
    assert not (tmp_path / "none.db").exists() or (tmp_path / "none.db").stat().st_size == 0
