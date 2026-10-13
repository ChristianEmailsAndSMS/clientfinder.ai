"""A single failed poll while following a live search must not end the follow (the search still runs and charges)."""
import json
import shutil
import subprocess
from pathlib import Path

import pytest

APP = (Path(__file__).resolve().parent.parent / "app" / "static" / "site" / "app.js").read_text()

HARNESS = r"""
const src = process.argv[2];
const start = src.indexOf("async function follow(");
const end = src.indexOf("async function showResult(");
const body = src.slice(start, end);
const polls = JSON.parse(process.argv[3]);   // list of {ok,status,data}
const log = { balance: 0, after: 0, shown: null, polls: 0 };
const mk = () => ({ isConnected: true, append() {}, });
const out = mk();
const h = () => ({}); const clear = (o) => { o.cleared = true; return { append(n) { if (n && n.text) log.shown = n.text; } }; };
const detail = (r) => "Something went wrong (" + r.status + ").";
const api = async () => polls[Math.min(log.polls++, polls.length - 1)];
const showResult = async (d) => { log.shown = "result:" + d.status; };
const refreshBalance = async () => { log.balance++; };
const origTimeout = setTimeout; global.setTimeout = (f) => origTimeout(f, 0);
const h2 = (cls, props) => ({ text: props && props.text });
const fn = new Function("h", "clear", "detail", "api", "showResult", "refreshBalance", "document", body + "; return follow;")(
  (c, p) => ({ text: p && p.text }), clear, detail, api, showResult, refreshBalance, {});
fn(1, out, null, () => { log.after++; }).then(() => console.log(JSON.stringify(log)));
"""


def run(polls):
    r = subprocess.run(["node", "-e", HARNESS, "x", APP, json.dumps(polls)], capture_output=True, text=True, timeout=60)
    assert r.returncode == 0, r.stderr
    return json.loads(r.stdout.strip().splitlines()[-1])


@pytest.mark.skipif(shutil.which("node") is None, reason="node not installed")
def test_one_bad_poll_does_not_end_the_follow():
    bad = {"ok": False, "status": 502, "data": None}
    done = {"ok": True, "status": 200, "data": {"status": "done"}}
    log = run([bad, done])
    assert log["shown"] == "result:done" and log["balance"] == 1 and log["after"] == 1


@pytest.mark.skipif(shutil.which("node") is None, reason="node not installed")
def test_giving_up_refreshes_the_balance():
    log = run([{"ok": False, "status": 0, "data": None}])
    assert log["balance"] == 1 and log["after"] == 1 and "may still have finished" in log["shown"]


@pytest.mark.skipif(shutil.which("node") is None, reason="node not installed")
def test_auth_errors_stop_at_once():
    log = run([{"ok": False, "status": 404, "data": None}])
    assert log["polls"] == 1
