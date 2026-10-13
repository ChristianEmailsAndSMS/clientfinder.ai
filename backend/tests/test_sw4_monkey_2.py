"""A failed 'Show more' must not wipe the jobs already on screen (only a failed first page replaces the list)."""
import re
from pathlib import Path

APP = (Path(__file__).resolve().parent.parent / "app" / "static" / "site" / "app.js").read_text()


def test_failed_show_more_keeps_the_list_and_offers_retry():
    body = APP[APP.index("async function loadJobs(reset)"):]
    body = body[:body.index("function renderActive")]
    keep = body.index("if (!r.ok && !reset)")
    wipe = body.index('if (!r.ok) { $("count").textContent = ""')
    assert keep < wipe                                       # the append-failure branch runs before the list-wiping one
    branch = body[keep:wipe]
    assert "clear($(\"list\"))" not in branch and '$("count")' not in branch
    assert "loadJobs(false)" in branch and "return;" in branch
