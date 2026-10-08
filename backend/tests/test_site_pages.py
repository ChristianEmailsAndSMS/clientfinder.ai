"""The customer-facing pages: served correctly, safe under the strict CSP, honest, and the redirect helper cannot be abused."""
import json
import re
import shutil
import subprocess
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

import authkit
from authkit import CSRF
from app.main import app

SITE = Path(__file__).resolve().parent.parent / "app" / "static" / "site"
ADMIN = Path(__file__).resolve().parent.parent / "app" / "static" / "admin"


@pytest.fixture
def env(monkeypatch):
    e = authkit.make_env(monkeypatch)
    yield e
    app.dependency_overrides.clear()


def test_public_pages_are_served(env):
    for path, marker in (("/", "Find clients"), ("/login", "Create account")):
        r = env.client.get(path)
        assert r.status_code == 200 and marker in r.text and r.headers["content-type"].startswith("text/html")
    assert "Create your admin account" not in env.client.get("/login").text           # no setup-code screen any more


def test_home_page_has_the_account_panel_built_in(env):
    html = env.client.get("/").text
    for needed in ('id="auth-panel"', 'id="form-up"', 'id="form-in"', 'id="tab-up"', 'id="tab-in"', 'id="v-2fa"', 'id="form-2fa"'):
        assert needed in html, needed
    assert html.index("/assets/common.js") < html.index("/assets/login.js") < html.index("/assets/home.js")    # script order matters
    assert 'href="#signup"' in html and 'href="#signin"' in html                                              # nav buttons open the panel
    assert not re.search(r"href=\"/login", html)                      # no link on the home page sends you elsewhere to sign in
    assert 'data-page="home"' in html and 'data-page="login"' in env.client.get("/login").text


def test_every_element_id_the_account_script_needs_exists_on_both_pages():
    js = (SITE / "login.js").read_text()
    ids = set(re.findall(r'\$\("([a-z0-9\-]+)"\)', js))
    for page in ("home.html", "login.html"):
        html = (SITE / page).read_text()
        missing = [i for i in ids if f'id="{i}"' not in html and not (page == "login.html" and i in ("auth-panel", "auth-title"))]
        assert not missing, (page, missing)


def test_app_page_needs_a_session_and_redirects_back_after_login(env):
    r = env.client.get("/app", follow_redirects=False)
    assert r.status_code == 302 and r.headers["location"] == "/login?next=/app"
    assert env.client.get("/app/", follow_redirects=False).status_code == 302
    assert env.client.post("/auth/signup", json={"email": "ui@example.com", "password": authkit.PW}, headers=CSRF).status_code == 201
    ok = env.client.get("/app")
    assert ok.status_code == 200 and "filters" in ok.text and "btn-run" in ok.text


def test_assets_are_allow_listed_and_cached_briefly(env):
    for name in ("site.css", "common.js", "home.js", "login.js", "app.js"):
        r = env.client.get(f"/assets/{name}")
        assert r.status_code == 200 and "max-age=300" in r.headers["cache-control"], name
    for bad in ("../main.py", "..%2fmain.py", "app.html", "home.html", "app.js.map", ".env", "%2e%2e/config.py"):
        assert env.client.get(f"/assets/{bad}").status_code == 404, bad


def test_old_routes_that_returned_raw_json_to_visitors_are_closed(env):
    anon = TestClient(app)
    assert anon.get("/jobs").status_code == 401 and anon.get("/jobs/stats").status_code == 401
    assert anon.get("/docs").status_code == 404


@pytest.mark.parametrize("page", ["home.html", "login.html", "app.html"])
def test_pages_have_no_inline_script_style_or_handlers_or_third_party_code(page):
    html = (SITE / page).read_text()
    assert not re.search(r"<script(?![^>]*\bsrc=)", html), "inline script"
    assert not re.search(r"\sstyle\s*=", html), "inline style attribute"
    assert not re.search(r"\son[a-z]+\s*=", html), "inline event handler"
    assert not re.search(r"(src|href)=\"(https?:)?//(?!emailsandsms\.com)", html.replace("https://emailsandsms.com", "")), "third-party resource"
    assert "<style" not in html


@pytest.mark.parametrize("path", [*sorted(SITE.glob("*.js")), *sorted(ADMIN.glob("*.js"))], ids=lambda p: f"{p.parent.name}/{p.name}")
def test_scripts_never_parse_html_or_run_strings_as_code(path):
    code = "\n".join(l for l in path.read_text().splitlines() if not l.lstrip().startswith("//"))
    for banned in ("innerHTML", "outerHTML", "insertAdjacentHTML", "document.write", "eval(", "new Function", "setAttribute(\"style\"", "setAttribute('style'", "javascript:"):
        assert banned not in code, banned
    assert not re.search(r"https?://(?!www\.w3\.org)", code), "scripts must only talk to our own origin"


def test_css_has_no_remote_resources():
    css = (SITE / "site.css").read_text()
    assert "@import" not in css and not re.search(r"url\(\s*['\"]?(https?:)?//", css)


def test_home_page_makes_no_claims_the_product_cannot_back_up():
    text = (SITE / "home.html").read_text() + (SITE / "home.js").read_text()
    for claim in ("Mediabistro", "Upwork +", "LLM-powered", "Twitter +", "guarantee", "unlimited credits", "5,000"):
        assert claim not in text, claim
    # every source it lists is one we actually ingest
    from app.scrapers.registry import DURABLE
    listed = re.search(r"const SOURCES = \[(.*?)\]", (SITE / "home.js").read_text()).group(1)
    for name in re.findall(r'"([a-z]+)"', listed):
        assert name in DURABLE, name


@pytest.mark.skipif(shutil.which("node") is None, reason="node not installed")
def test_post_login_redirect_helper_blocks_open_redirects():
    cases = {
        "/app": "/app", "/admin": "/admin", "/app?q=email&sort=newest": "/app?q=email&sort=newest",
        "//evil.example": "/app", "/\\evil.example": "/app", "https://evil.example": "/app", "http://evil.example/x": "/app",
        "javascript:alert(1)": "/app", "data:text/html,x": "/app", "": "/app", None: "/app", "app": "/app", "/\nhttps://evil.example": "/app",
        "/%2f%2fevil.example": "/%2f%2fevil.example",  # percent-encoded stays inside our origin: the browser will not treat it as a host
    }
    js = (SITE / "common.js").read_text()
    script = "global.window = {}; global.document = {}; " + js + "; console.log(JSON.stringify(Object.entries(%s).map(([k,v]) => [k, window.CF.safeNext(k === 'null' ? null : k, '/app')])))" % json.dumps(
        {("null" if k is None else k): v for k, v in cases.items()})
    out = subprocess.run(["node", "-e", script], capture_output=True, text=True, timeout=20)
    assert out.returncode == 0, out.stderr
    got = dict(json.loads(out.stdout))
    for k, want in cases.items():
        assert got["null" if k is None else k] == want, (k, got["null" if k is None else k], want)
