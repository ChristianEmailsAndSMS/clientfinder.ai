"""Customer UI accuracy regressions found in the frontend audit. The UI has no browser test rig, so these pin the exact
logic that was wrong (each assertion failed on the code before the fix)."""
import re
from pathlib import Path

SITE = Path(__file__).resolve().parent.parent / "app" / "static" / "site"
APP = (SITE / "app.js").read_text()
HOME = (SITE / "home.html").read_text()


def _fn(name: str) -> str:
    start = APP.index(name)
    return APP[start:start + 3000]


def test_url_is_read_before_the_search_box_is_built():
    # /app?q=copywriter filtered the list but showed an empty search box, because the box was created before readUrl() ran
    assert re.search(r"readUrl\(\);\s*buildHeader\(\)", APP)


def test_short_service_text_is_not_sent_for_a_quote_or_a_search():
    body = _fn("async function estimate()")
    # asSearch() prefixes "hiring ", so q.length < 3 was never true and typing "a" quoted (and could run) a paid search for "hiring a"
    assert "q.length < 3" not in body and "input.value.trim().length < 3" in body
    assert 'input.value.trim().length < 3) return;' in APP        # the run button re-checks


def test_only_the_newest_estimate_may_update_the_search_modal():
    body = _fn("async function estimate()")
    assert "estSeq" in body and "seq !== estSeq" in body          # changing the time window used to let a slow old quote ("Free") win


def test_onboarding_does_not_dead_end_when_there_are_no_places_to_pick():
    assert "kindsAll.length === 0 ||" in APP                       # empty database / failed facets left step 3 unfinishable


def test_unlimited_accounts_are_not_told_they_have_unlimited_searches():
    assert "unlimited searches" not in APP                         # the daily cap applies to them too (docs/SECURITY.md)
    assert "last-24-hours limit" in APP or "daily limit" in APP


def test_pay_minimum_is_only_sent_with_a_pay_period():
    assert re.search(r'if \(state\.minPay !== "" && state\.payPeriod', APP)     # $60 per hour and $60k per year were compared as the same number
    assert "Pick a pay period first" in APP


def test_home_page_does_not_promise_what_the_search_form_lacks():
    assert "where to look" not in HOME                             # there is no site picker in the search dialog
    assert "exact price before you run it" not in HOME             # the dialog shows "about $x"; the charge is from real usage
