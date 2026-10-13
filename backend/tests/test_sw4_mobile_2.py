"""The phone Filters drawer behaves as a dialog: focus moves in, Tab stays inside, Escape or a tap outside closes it, focus goes back.
Runs the real app.js in chromium with the API mocked; skipped where playwright or chromium is not installed."""
import json
import os
from pathlib import Path

import pytest

SITE = Path(__file__).resolve().parent.parent / "app" / "static" / "site"
CHROME = os.environ.get("CF_CHROMIUM", "/opt/pw-browsers/chromium-1194/chrome-linux/chrome")
ME = {"id": 1, "email": "a@b.co", "onboarded": True, "prefs": {}, "balance_usd": 1, "unlimited": False, "is_admin": False}
FACETS = {"types": [], "kinds": [], "platforms": [], "regions": [], "countries": [], "total": 0}


def _api(path):
    if path.startswith("/auth/me"):
        return ME
    if path.startswith("/jobs/facets"):
        return FACETS
    return []


def test_filters_drawer_is_an_accessible_dialog():
    sync = pytest.importorskip("playwright.sync_api")
    if not Path(CHROME).exists():
        pytest.skip("chromium not installed")
    types = {".js": "application/javascript", ".css": "text/css", ".html": "text/html", ".png": "image/png"}

    def handle(route):
        u = route.request.url.split("://", 1)[1].split("/", 1)[1].split("?")[0]
        path = "/" + u
        if path == "/app":
            return route.fulfill(body=(SITE / "app.html").read_bytes(), content_type="text/html")
        if path.startswith("/assets/") and (SITE / path[8:]).is_file():
            f = SITE / path[8:]
            return route.fulfill(body=f.read_bytes(), content_type=types.get(f.suffix, "application/octet-stream"))
        return route.fulfill(body=json.dumps(_api(path)), content_type="application/json")

    with sync.sync_playwright() as p:
        b = p.chromium.launch(executable_path=CHROME, args=["--no-sandbox"])
        page = b.new_page(viewport={"width": 390, "height": 600})
        page.route("http://cf.test/**", handle)
        page.goto("http://cf.test/app")
        page.wait_for_selector("#filters .chip", state="attached")
        state = lambda: page.evaluate("""() => ({open: document.getElementById('filters').classList.contains('open'),
            inside: document.getElementById('filters').contains(document.activeElement),
            btn: document.activeElement && document.activeElement.id === 'btn-filters',
            role: document.getElementById('filters').getAttribute('role'), modal: document.getElementById('filters').getAttribute('aria-modal')})""")
        page.click("#btn-filters")
        s = state()
        assert s["open"] and s["inside"] and s["role"] == "dialog" and s["modal"] == "true"
        for _ in range(60):                                   # Tab never leaves the drawer
            page.keyboard.press("Tab")
            assert state()["inside"]
        page.keyboard.press("Escape")
        s = state()
        assert not s["open"] and s["btn"] and s["role"] is None
        page.click("#btn-filters")
        assert state()["open"]
        page.mouse.click(10, 300)                             # the dimmed strip left of the drawer
        s = state()
        assert not s["open"] and s["btn"]
        page.click("#btn-filters")
        page.click("#filters .chip")                          # a filter tap rebuilds nothing that closes it
        assert state()["open"]
        b.close()
