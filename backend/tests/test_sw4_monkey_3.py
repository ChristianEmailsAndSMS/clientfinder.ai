"""Signed-in visitors on the home page: the sign-up call to action must not be a dead click that throws."""
import json
import os

import pytest
from fastapi.testclient import TestClient

from app.main import app

CHROME = "/opt/pw-browsers/chromium-1194/chrome-linux/chrome"
pw = pytest.importorskip("playwright.sync_api")


@pytest.mark.skipif(not os.path.exists(CHROME), reason="no browser")
@pytest.mark.parametrize("how", ["row", "hash"])
def test_signed_in_home_signup_cta_goes_to_app(how):
    client = TestClient(app)
    job = {"title": "Email copywriter", "platform": "linkedin", "type": None, "remote": True, "pay": None, "seen_at": "2026-10-12T10:00:00Z"}
    stubs = {"/auth/status": {"signed_in": True, "auth_configured": True}, "/auth/me": {"email": "a@b.co", "is_admin": False},
             "/public/preview": [job], "/public/config": {"signup_bonus_usd": 0, "search_price_usd": 0.5},
             "/public/stats": {"jobs_total": 1, "jobs_new_24h": 1, "sources": 1, "last_update": None}}
    errors = []
    with pw.sync_playwright() as p:
        b = p.chromium.launch(executable_path=CHROME, args=["--no-sandbox"])
        page = b.new_page()
        page.on("pageerror", lambda e: errors.append(str(e)))

        def handle(route):
            path = route.request.url.split("localhost", 1)[1].split("/", 1)[1]
            path = "/" + path.split("?")[0].split("#")[0]
            if path in stubs:
                return route.fulfill(status=200, content_type="application/json", body=json.dumps(stubs[path]))
            if path == "/app":
                return route.fulfill(status=200, content_type="text/html", body="<html>app</html>")
            r = client.get(path)
            route.fulfill(status=r.status_code, body=r.content, headers={"content-type": r.headers.get("content-type", "text/plain")})

        page.route("http://localhost/**", handle)
        page.goto("http://localhost/")
        page.wait_for_selector(".welcome")
        page.wait_for_selector(".prow")
        if how == "row":
            page.locator(".prow").first.click()
        else:
            page.evaluate("location.hash = '#signup'")
        page.wait_for_url("**/app", timeout=5000)
        b.close()
    assert not errors
