import re
import shutil
import subprocess
from pathlib import Path

import pytest

APP_JS = Path(__file__).resolve().parents[1] / "app" / "static" / "site" / "app.js"


@pytest.mark.skipif(not shutil.which("node"), reason="node not installed")
def test_min_pay_scientific_notation_is_normalised_to_plain_amount():
    src = APP_JS.read_text()
    m = re.search(r"function normPay\(v\) \{.*?\n", src)
    assert m, "normPay helper missing"
    script = m.group(0) + """
const out = ["1e5", "50000", "12.5", "", "-3", "1e-7", "abc", "1e30"].map(normPay);
console.log(JSON.stringify(out));
"""
    r = subprocess.run(["node", "-e", script], capture_output=True, text=True, check=True)
    assert r.stdout.strip() == '["100000","50000","12.5","","","","",""]'
    # the input handler must use it
    assert "state.minPay = normPay(minPay.value)" in src
