"""A deploy must never leave a browser or Cloudflare pairing new HTML with an old cached script (it broke sign-in messages on the live site)."""
import re
import shutil
import subprocess
from pathlib import Path

import pytest

import authkit
from app.api import site
from app.main import app

SITE = Path(__file__).resolve().parent.parent / "app" / "static" / "site"


@pytest.fixture
def env(monkeypatch):
    e = authkit.make_env(monkeypatch)
    yield e
    app.dependency_overrides.clear()


def test_pages_name_every_script_style_and_image_by_content_hash(env):
    for path in ("/", "/login"):
        r = env.client.get(path)
        assert r.headers["cache-control"] in ("no-cache", "no-store"), path               # the page itself is never reused blindly
        refs = re.findall(r'(?:src|href)="(/assets/[a-z\-]+\.(?:js|css|png))(\?v=[0-9a-f]{10})?"', r.text)
        assert refs and all(v for _, v in refs), (path, [u for u, v in refs if not v])


def test_a_versioned_asset_is_cached_for_a_year_and_a_stale_url_for_a_minute(env):
    ref = re.search(r'src="(/assets/login\.js\?v=[0-9a-f]{10})"', env.client.get("/").text).group(1)
    assert env.client.get(ref).headers["cache-control"] == "public, max-age=31536000, immutable"
    old = env.client.get("/assets/login.js?v=0000000000")
    assert old.status_code == 200 and "max-age=60" in old.headers["cache-control"] and "immutable" not in old.headers["cache-control"]
    assert "max-age=60" in env.client.get("/assets/login.js").headers["cache-control"]
    assert env.client.get("/assets/../home.html").status_code in (404, 400)            # still the allow-list


def test_the_version_changes_when_the_file_changes(tmp_path, monkeypatch):
    shutil.copy(SITE / "login.js", tmp_path / "login.js")
    monkeypatch.setattr(site, "_DIR", tmp_path); site._hashes.clear()
    before = site._version("login.js")
    (tmp_path / "login.js").write_text((tmp_path / "login.js").read_text() + "\n// changed\n")
    assert site._version("login.js") != before and re.fullmatch(r"[0-9a-f]{10}", before)
    site._hashes.clear()


@pytest.mark.skipif(shutil.which("node") is None, reason="node is not installed")
def test_pay_text_shows_commas_and_hides_non_amounts():
    js = (SITE / "common.js").read_text()
    script = "global.window = {}; global.document = {getElementById: () => null};\n" + js + """
      const c = window.CF.cleanPay;
      console.log(JSON.stringify([c("$255000-$320000"), c("Salary unspecified"), c("Hourly"), c("1300"), c("$50-70/hr"), c("$120k"), c("")]));"""
    out = subprocess.run(["node", "-e", script], capture_output=True, text=True, timeout=20)
    assert out.returncode == 0, out.stderr
    assert out.stdout.strip() == '["$255,000-$320,000","","","1,300 (rate not stated)","$50-70/hr","$120k",""]'


def test_old_script_with_new_html_is_the_login_js_108_error_and_the_current_script_cannot_do_it():
    """A cached pre-change login.js called $("form-2fa").addEventListener on line 108; the new HTML has no such element. Every id the CURRENT
    script looks up at load time must exist on both pages (guards against re-introducing the mismatch), and nothing may be wired to a missing element."""
    js = (SITE / "login.js").read_text()
    for page in ("home.html", "login.html"):
        html = (SITE / page).read_text()
        for el_id in re.findall(r'\$\("([a-z0-9\-]+)"\)\.addEventListener', js):
            assert f'id="{el_id}"' in html, (page, el_id)
    assert "form-2fa" not in js and "form-2fa" not in (SITE / "home.html").read_text()


def test_accessibility_labels_from_the_retest():
    app_js, app_html = (SITE / "app.js").read_text(), (SITE / "app.html").read_text()
    assert 'id="sort" aria-label="Sort jobs"' in app_html
    assert 'detailsBtn.setAttribute("aria-expanded"' in app_js and 'desc.hidden ? "Details" : "Hide details"' in app_js
    assert '"See my matches" : "Next"' not in app_js                      # the wizard's last button no longer repeats the name of the next screen's button
