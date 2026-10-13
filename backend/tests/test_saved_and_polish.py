"""Saved jobs (private bookmarks) and the second round of polish from the live test."""
from pathlib import Path

import pytest
from sqlalchemy import select
from fastapi.testclient import TestClient

import authkit
from authkit import CSRF, PW
from app.main import app
from app.models import Job, SavedJob, User

SITE = Path(__file__).resolve().parent.parent / "app" / "static" / "site"


@pytest.fixture
def env(monkeypatch):
    e = authkit.make_env(monkeypatch)
    yield e
    app.dependency_overrides.clear()


def member(env, email="saver@example.com"):
    c = TestClient(app)
    assert c.post("/auth/signup", json={"email": email, "password": PW}, headers=CSRF).status_code == 201
    return c


def add_job(env, title="Copywriter wanted", real=True):
    with env.scope() as db:
        n = db.query(Job).count()
        j = Job(title=title, platform="reddit", source_key="s", source_url=f"https://example.com/{n}", dedupe_hash=f"h{n}", is_real_job=real)
        db.add(j); db.flush()
        return j.id


def test_save_list_and_unsave_a_job(env):
    c = member(env); a, b = add_job(env, "Job A"), add_job(env, "Job B")
    assert c.get("/account/saved/ids").json() == []
    assert c.put(f"/account/saved/{a}", headers=CSRF).json() == {"saved": True}
    assert c.put(f"/account/saved/{a}", headers=CSRF).status_code == 200                 # saving twice is fine
    assert c.put(f"/account/saved/{b}", headers=CSRF).status_code == 200
    assert sorted(c.get("/account/saved/ids").json()) == sorted([a, b])
    assert [j["title"] for j in c.get("/account/saved").json()] == ["Job B", "Job A"]     # most recently saved first
    assert c.delete(f"/account/saved/{a}", headers=CSRF).json() == {"saved": False}
    assert c.delete(f"/account/saved/{a}", headers=CSRF).status_code == 200               # removing twice is fine
    assert c.get("/account/saved/ids").json() == [b]


def test_saved_jobs_are_private_and_need_a_signed_in_user_and_csrf(env):
    c1, c2 = member(env, "one@example.com"), member(env, "two@example.com")
    j = add_job(env)
    c1.put(f"/account/saved/{j}", headers=CSRF)
    assert c2.get("/account/saved/ids").json() == [] and c2.get("/account/saved").json() == []
    anon = TestClient(app)
    for method, url in (("get", "/account/saved"), ("get", "/account/saved/ids"), ("put", f"/account/saved/{j}"), ("delete", f"/account/saved/{j}")):
        assert getattr(anon, method)(url, headers=CSRF).status_code == 401, (method, url)
    assert c1.put(f"/account/saved/{j}").status_code == 403 and c1.delete(f"/account/saved/{j}").status_code == 403   # needs the CSRF header
    c2.delete(f"/account/saved/{j}", headers=CSRF)                                         # someone else's removal changes nothing
    assert c1.get("/account/saved/ids").json() == [j]


def test_only_real_listed_jobs_can_be_saved_and_there_is_a_cap(env, monkeypatch):
    c = member(env)
    assert c.put("/account/saved/999999", headers=CSRF).status_code == 404
    hidden = add_job(env, "Not a job", real=False)
    assert c.put(f"/account/saved/{hidden}", headers=CSRF).status_code == 404
    from app.api import account
    monkeypatch.setattr(account, "MAX_SAVED", 2)
    ids = [add_job(env, f"J{n}") for n in range(3)]
    assert [c.put(f"/account/saved/{i}", headers=CSRF).status_code for i in ids] == [200, 200, 409]


def test_a_removed_job_drops_out_of_the_saved_list(env):
    c = member(env); j = add_job(env)
    c.put(f"/account/saved/{j}", headers=CSRF)
    with env.scope() as db:
        db.get(Job, j).is_real_job = False
        db.commit()
    assert all(x["id"] != j for x in c.get("/account/saved").json())


def test_the_migration_matches_the_model():
    src = (Path(__file__).resolve().parent.parent / "alembic" / "versions" / "0010_saved_jobs.py").read_text()
    assert 'down_revision: Union[str, None] = "0009"' in src and '"saved_jobs"' in src and "ondelete=\"CASCADE\"" in src
    assert [c.name for c in SavedJob.__table__.columns] == ["user_id", "job_id", "created_at"]


def test_frontend_polish_round_two():
    app_js, common, login, home = ((SITE / f).read_text() for f in ("app.js", "common.js", "login.js", "home.js"))
    assert "CF.signOut" in common and "signed_in" in common and "CF.signOut()" in app_js and "CF.signOut()" in login          # sign-out is verified
    assert "Find clients <br>" in (SITE / "home.html").read_text()                                                             # the space screen readers need
    assert "CF.typeLabel" in common and "typeGroups" in app_js                                                                  # one label per job type
    assert "CF.cleanPay" in common and "CF.cleanPay" in home and "rate not stated" in common                                     # no bare "1300", no "Salary unspecified"
    assert "draft.all" in app_js and "Choose All of them, or pick just the places you want" in app_js                          # All vs a pick, never both
    assert "function showMine" in app_js and "Your earlier search and filters were cleared" in app_js
    assert "/account/saved" in app_js and "function openSaved" in app_js and "Saved jobs" in app_js
    assert "Copy email" in app_js and "use this email so we can match the payment" in app_js
    assert "A search costs about" in app_js                                                                                    # no more "20 searches left" next to "$0.00"
    assert ".innerHTML" not in app_js + common + login + home


def test_dialogs_take_focus_trap_tab_and_return_focus():
    app_js = (SITE / "app.js").read_text()
    assert "function trapFocus" in app_js and app_js.count("trapFocus(ov, m);") == 2     # wizard and every other modal
    assert "opener.focus()" in app_js                                                  # focus goes back to the button that opened it
    assert 'e.key !== "Tab"' in app_js
    assert "data-lock" in app_js and "m.dataset.lock" in app_js                         # Escape does not drop the first-run wizard
    assert "!dialogOpen" in app_js                                                      # "/" does not jump to the page behind a dialog

def test_save_buttons_share_one_repaint():
    """Unsaving inside the Saved jobs window must repaint the Save button in the main list (it used to stay on "Saved" and then re-save on click)."""
    app_js = (SITE / "app.js").read_text()
    assert "function repaintSaved" in app_js and 'data-save-id' in app_js
    assert app_js.count("repaintSaved()") >= 3                  # after a toggle, after a failed toggle, and after the window loads
    assert "count.hidden = !n" in app_js                         # the "N saved jobs" line follows the window

def test_wizard_allows_clearing_roles_after_first_run():
    app_js = (SITE / "app.js").read_text()
    assert "ok: () => !first || draft.roles.size > 0" in app_js            # only first run insists on a role; later edits can return to "everything"
    assert "Leave all unselected to see every job" in app_js

def test_pay_chip_uses_the_same_validity_check_as_the_request():
    """A typed negative minimum is not sent to the API, so it must not show a 'Pay ≥ -5/hr' chip either."""
    app_js = (SITE / "app.js").read_text()
    assert 'state.minPay !== "" && state.payPeriod && Number(state.minPay) >= 0) add(`Pay' in app_js
    assert 'state.minPay !== "" && state.payPeriod) add(`Pay' not in app_js

def test_app_js_sends_signed_out_customers_to_login_on_any_call():
    """A 401 from any call on /app (session ended in another tab) must redirect, not just toast 'Not signed in'."""
    js = (SITE / "app.js").read_text()
    assert "const { h, api," not in js and "CF.api(method, path, body)" in js
    assert "r.status === 401) toLogin()" in js
