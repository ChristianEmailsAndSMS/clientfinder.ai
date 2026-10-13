"""Fixes from the combined live test: crawlers and previews, readable text, one wording, and a sign-in answer that says when you are locked."""
import re
from pathlib import Path

import pytest

import authkit
from app.main import app

SITE = Path(__file__).resolve().parent.parent / "app" / "static" / "site"


@pytest.fixture
def env(monkeypatch):
    e = authkit.make_env(monkeypatch)
    yield e
    app.dependency_overrides.clear()


def test_sitemap_favicon_and_robots(env):
    r = env.client.get("/sitemap.xml")
    assert r.status_code == 200 and r.headers["content-type"].startswith("application/xml") and "<loc>https://clientfinder.ai/</loc>" in r.text
    f = env.client.get("/favicon.ico")
    assert f.status_code == 200 and f.headers["content-type"] == "image/png" and f.content[:4] == b"\x89PNG"
    assert "Sitemap: https://clientfinder.ai/sitemap.xml" in env.client.get("/robots.txt").text


def test_social_preview_tags_on_the_public_pages():
    for page in ("home.html", "login.html"):
        html = (SITE / page).read_text()
        for tag in ('property="og:title"', 'property="og:description"', 'property="og:url"', 'property="og:type"', 'name="twitter:title"', 'name="twitter:description"', 'property="og:image"'):
            assert tag in html, (page, tag)


def _lum(h):
    r, g, b = [int(h[i:i + 2], 16) / 255 for i in (1, 3, 5)]
    f = lambda c: c / 12.92 if c <= .03928 else ((c + .055) / 1.055) ** 2.4
    return .2126 * f(r) + .7152 * f(g) + .0722 * f(b)


def test_the_faint_text_colour_is_readable_on_every_background():
    css = (SITE / "site.css").read_text()
    col = lambda name: re.search(rf"--{name}:(#[0-9a-fA-F]{{6}})", css).group(1)
    for bg in ("bg", "card"):
        hi, lo = sorted((_lum(col("dim")), _lum(col(bg))), reverse=True)
        assert (hi + .05) / (lo + .05) >= 4.5, (bg, col("dim"))                     # WCAG AA for normal text


def test_one_brand_spelling_and_one_create_account_button_wording():
    text = "".join((SITE / f).read_text() for f in ("home.html", "login.html", "app.js", "login.js", "home.js"))
    assert "Create my account" not in text and "Client Finder" not in text and "ClientFinder" not in text
    assert not re.search(r"Clientfinder(?!\.ai)", text.replace("clientfinder", ""))   # always "Clientfinder.ai"
    assert "is on us" not in text and "Your first live search is free" in text


def test_switching_between_sign_in_and_create_account_clears_the_other_tabs_error():
    js = (SITE / "login.js").read_text()
    assert 'say($("err-in"), ""); say($("err-up"), "")' in js


def test_the_show_password_button_is_a_real_tap_target():
    css = (SITE / "site.css").read_text()
    assert re.search(r"\.pwrow \.link\{[^}]*min-height:40px", css) and re.search(r"\.pwrow \.link\{[^}]*font-size:13\.5px", css)
