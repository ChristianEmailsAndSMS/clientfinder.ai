"""Welcome credit (one per mailbox, daily cap), the public-page fixes, and the 'also show posts with no location' filter."""
import re
from datetime import timedelta
from pathlib import Path

import pytest
from sqlalchemy import select

import authkit
from authkit import CSRF, PW
from app.api.auth import mailbox_key
from app.config import settings
from app.main import app
from app.models import CreditEntry, User

SITE = Path(__file__).resolve().parent.parent / "app" / "static" / "site"


@pytest.fixture
def env(monkeypatch):
    e = authkit.make_env(monkeypatch)
    yield e
    app.dependency_overrides.clear()


def signup(c, email):
    return c.post("/auth/signup", json={"email": email, "password": PW}, headers=CSRF)


def balance(env, email):
    with env.scope() as db:
        return db.scalar(select(User.balance_micro).where(User.email == email))


def test_mailbox_key_folds_aliases_that_reach_the_same_inbox():
    assert mailbox_key("Jane.Doe+promo@Gmail.com") == mailbox_key("janedoe@googlemail.com") == "janedoe@gmail.com"
    assert mailbox_key("a.b+x@example.com") == "a.b@example.com"                       # dots only count at gmail
    assert mailbox_key("a@example.com") != mailbox_key("a@example.org")


def test_welcome_credit_is_given_once_per_mailbox(env, monkeypatch):
    monkeypatch.setattr(settings, "signup_bonus_usd", 0.75)
    assert signup(env.client, "pat.smith@gmail.com").status_code == 201
    assert balance(env, "pat.smith@gmail.com") == 750_000
    assert signup(env.client, "patsmith+again@gmail.com").status_code == 201            # same inbox: account yes, free credit no
    assert balance(env, "patsmith+again@gmail.com") == 0
    assert signup(env.client, "someone.else@example.com").status_code == 201
    assert balance(env, "someone.else@example.com") == 750_000


def test_welcome_credit_stops_at_the_daily_cap_and_is_no_longer_advertised(env, monkeypatch):
    monkeypatch.setattr(settings, "signup_bonus_usd", 0.75)
    monkeypatch.setattr(settings, "signup_bonus_daily_cap", 2)
    assert env.client.get("/public/config").json()["signup_bonus_usd"] == 0.75
    for n in range(3):
        assert signup(env.client, f"user{n}@example.com").status_code == 201
    assert [balance(env, f"user{n}@example.com") for n in range(3)] == [750_000, 750_000, 0]
    assert env.client.get("/public/config").json()["signup_bonus_usd"] == 0              # offer disappears once today's cap is used
    with env.scope() as db:                                                              # a day later the cap resets
        for e in db.scalars(select(CreditEntry)).all():
            e.created_at = e.created_at - timedelta(hours=25)
    assert env.client.get("/public/config").json()["signup_bonus_usd"] == 0.75


def test_welcome_credit_is_off_by_default_and_a_zero_cap_means_no_limit(env, monkeypatch):
    assert signup(env.client, "plain@example.com").status_code == 201 and balance(env, "plain@example.com") == 0
    monkeypatch.setattr(settings, "signup_bonus_usd", 0.5)
    monkeypatch.setattr(settings, "signup_bonus_daily_cap", 0)
    for n in range(4):
        signup(env.client, f"free{n}@example.com")
    assert all(balance(env, f"free{n}@example.com") == 500_000 for n in range(4))


def test_status_says_whether_you_are_signed_in(env):
    assert env.client.get("/auth/status").json()["signed_in"] is False
    signup(env.client, "in@example.com")
    assert env.client.get("/auth/status").json()["signed_in"] is True


def test_public_pages_have_no_unfinished_screens_and_quiet_error_lines():
    for page in ("home.html", "login.html"):
        html = (SITE / page).read_text()
        assert "v-2fa" not in html and 'id="qr"' not in html and "<img id=" not in html
        assert not re.search(r'<p[^>]*class="error"[^>]*role=', html), page                  # an empty alert box is announced by screen readers
        assert len(re.findall(r'class="error"[^>]*hidden', html)) >= 3, page
        assert html.count("novalidate") == 2, page                                          # our own messages, not the browser bubble
        assert 'id="btn-forgot"' in html and 'id="forgot-note"' in html and "mailto:christian@emailsandsms.com" in html


def test_signup_line_follows_the_tab_and_the_script_builds_the_2fa_screen_itself():
    js = (SITE / "login.js").read_text()
    assert "Sign in to pick up where you left off." in js and "function enrolScreen" in js
    assert 'api("GET", "/auth/status")' in js and "signed_in" in js and 'status.then((r) => (r.ok && r.data.signed_in ? api("GET", "/auth/me")' in js
    assert ".innerHTML" not in js


def test_home_preview_rows_open_the_sign_up_panel():
    js = (SITE / "home.js").read_text()
    assert 'role: "link"' in js and "openSignup" in js and 'getElementById("tab-up").click()' in js
    assert "Click any job to create your free account" in (SITE / "home.html").read_text()


def test_wizard_shows_job_counts_per_region_and_the_filter_offers_unspecified_posts():
    js = (SITE / "app.js").read_text()
    assert "regionShown(r, true, true)" in js and "jobs`" in js
    assert "Also show posts that do not say where" in js and "include_unspecified" in js
