import json
import shutil
import subprocess
from pathlib import Path

import pytest

COMMON = Path(__file__).resolve().parents[1] / "app" / "static" / "site" / "common.js"


@pytest.mark.skipif(shutil.which("node") is None, reason="node not installed")
def test_money_has_no_minus_on_zero():
    script = (
        "global.window={};global.document={};"
        f"require({json.dumps(str(COMMON))});"
        "const m=window.CF.money;"
        "console.log(JSON.stringify([m(-0.0004),m(-0.0004,4),m(-0.5),m(0),m(1.5),m(-0.004,3)]));"
    )
    out = subprocess.run(["node", "-e", script], capture_output=True, text=True, timeout=20)
    assert out.returncode == 0, out.stderr
    assert json.loads(out.stdout) == ["$0.00", "-$0.0004", "-$0.50", "$0.00", "$1.50", "-$0.004"]
