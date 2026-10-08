"""Regression tests for the security fixes: secret leaks, SSRF, stored-URL validation, headers, defaults."""
import io
import logging
import socket

import httpx
import pytest
from fastapi.testclient import TestClient

from app.config import settings
from app.logging_setup import setup_logging
from app.main import app
from app.schemas import ExtractedJob, SearchResult
from app.scrapers import google_search, page_fetcher, problogger
from app.security_utils import UnsafeUrl, assert_public_url, redact, safe_job_url

SECRET = "SUPER-SECRET-KEY-123"


# ---------- redaction ----------
@pytest.mark.parametrize("raw", [
    f"GET https://serpapi.com/search.json?q=x&api_key={SECRET}&num=10",
    f"Authorization: Bearer {SECRET}abcdef",
    f"X-Admin-Token: {SECRET}",
    f"token={SECRET}",
    "key sk-ant-api03-AbCdEf_123-xyz leaked",
    f"password={SECRET}",
])
def test_redact_removes_credentials(raw):
    out = redact(raw)
    assert SECRET not in out and "AbCdEf" not in out and "[REDACTED]" in out


def test_redact_leaves_normal_text_alone():
    text = "failed to fetch https://example.org/jobs?page=2&sort=new: HTTP 403"
    assert redact(text) == text


def test_log_records_are_redacted_and_httpx_url_logging_is_off():
    stream = io.StringIO()
    root = logging.getLogger()
    saved = root.handlers[:]
    for h in saved:
        root.removeHandler(h)
    handler = logging.StreamHandler(stream)
    root.addHandler(handler)
    try:
        setup_logging()          # basicConfig is a no-op now (handlers exist) but filters + levels still apply
        logging.getLogger("app.test").warning("calling https://x.test/?api_key=%s now", SECRET)
        out = stream.getvalue()
        assert SECRET not in out and "api_key=[REDACTED]" in out
        assert logging.getLogger("httpx").level == logging.WARNING        # would print the full URL at INFO
    finally:
        root.removeHandler(handler)
        for h in saved:
            root.addHandler(h)


def test_search_errors_never_contain_the_api_key(monkeypatch):
    req = httpx.Request("GET", f"https://serpapi.com/search.json?q=x&api_key={SECRET}")

    def fake_get(url, **kw):
        return httpx.Response(401, request=req)       # raise_for_status() text would include the full URL

    monkeypatch.setattr(google_search.httpx, "get", fake_get)
    monkeypatch.setattr(settings, "dev_fixtures", False)
    monkeypatch.setattr(settings, "serpapi_api_key", SECRET)
    monkeypatch.setattr(settings, "google_search_provider", "serpapi")
    with pytest.raises(google_search.SearchError) as ei:
        google_search.search("q")
    assert SECRET not in str(ei.value) and "401" in str(ei.value)


def test_network_errors_do_not_leak_the_url(monkeypatch):
    def boom(url, **kw):
        raise httpx.ConnectError(f"cannot connect to {url}?api_key={SECRET}")
    monkeypatch.setattr(google_search.httpx, "get", boom)
    monkeypatch.setattr(settings, "dev_fixtures", False)
    monkeypatch.setattr(settings, "serpapi_api_key", SECRET)
    with pytest.raises(google_search.SearchError) as ei:
        google_search.search("q")
    assert SECRET not in str(ei.value)


def test_pipeline_stores_redacted_search_error(monkeypatch):
    from app.scrapers import pipeline
    monkeypatch.setattr(settings, "dev_fixtures", False)
    monkeypatch.setattr(settings, "anthropic_api_key", "k")
    def leaky(*a, **k):
        raise RuntimeError(f"boom https://x.test/?api_key={SECRET}")
    monkeypatch.setattr(google_search, "search", leaky)
    assert SECRET not in pipeline.run_pipeline_for_query("q")["search_error"]


# ---------- SSRF guard ----------
def resolve_to(monkeypatch, mapping):
    def fake(host, port, *a, **k):
        ips = mapping.get(host)
        if ips is None:
            raise socket.gaierror("nope")
        return [(socket.AF_INET6 if ":" in ip else socket.AF_INET, socket.SOCK_STREAM, 6, "", (ip, 0)) for ip in ips]
    monkeypatch.setattr("app.security_utils.socket.getaddrinfo", fake)


@pytest.mark.parametrize("url", [
    "http://127.0.0.1/", "http://127.0.0.1:8000/admin", "http://10.0.0.5/", "http://192.168.1.1/",
    "http://172.16.0.1/", "http://169.254.169.254/latest/meta-data/", "http://0.0.0.0/", "http://[::1]/",
    "http://[::ffff:127.0.0.1]/", "http://100.64.0.1/",
])
def test_private_and_special_addresses_are_blocked(url, monkeypatch):
    resolve_to(monkeypatch, {})
    with pytest.raises(UnsafeUrl):
        assert_public_url(url)


@pytest.mark.parametrize("url", [
    "file:///etc/passwd", "ftp://example.org/x", "gopher://example.org/", "javascript:alert(1)",
    "http://user:pw@example.org/", "https://example.org:5432/", "http://example.org:6380/", "http:///nohost", "not a url",
])
def test_bad_schemes_credentials_and_ports_are_blocked(url, monkeypatch):
    resolve_to(monkeypatch, {"example.org": ["93.184.216.34"]})
    with pytest.raises(UnsafeUrl):
        assert_public_url(url)


def test_hostname_resolving_to_private_is_blocked_even_if_one_answer_is_public(monkeypatch):
    resolve_to(monkeypatch, {"evil.test": ["93.184.216.34", "127.0.0.1"], "internal.test": ["10.1.2.3"], "ok.test": ["93.184.216.34"]})
    for host in ("evil.test", "internal.test"):
        with pytest.raises(UnsafeUrl):
            assert_public_url(f"https://{host}/")
    assert_public_url("https://ok.test/job/1")
    assert_public_url("http://ok.test:80/")
    with pytest.raises(UnsafeUrl):
        assert_public_url("https://unresolvable.test/")


def client_factory(monkeypatch, handler):
    real = httpx.Client
    monkeypatch.setattr(page_fetcher.httpx, "Client", lambda **kw: real(transport=httpx.MockTransport(handler), **kw))


def test_redirect_to_internal_address_is_blocked(monkeypatch):
    resolve_to(monkeypatch, {"public.test": ["93.184.216.34"], "internal.test": ["127.0.0.1"]})
    hits = []

    def handler(request):
        hits.append(str(request.url))
        if request.url.host == "public.test":
            return httpx.Response(302, headers={"location": "http://internal.test/admin"})
        return httpx.Response(200, text="SECRET INTERNAL PAGE")

    client_factory(monkeypatch, handler)
    with pytest.raises(UnsafeUrl):
        page_fetcher.fetch_simple("https://public.test/job")
    assert hits == ["https://public.test/job"]            # the internal URL was never requested


def test_relative_redirects_are_followed_and_checked(monkeypatch):
    resolve_to(monkeypatch, {"public.test": ["93.184.216.34"]})
    def handler(request):
        if request.url.path == "/job":
            return httpx.Response(301, headers={"location": "/job/final"})
        return httpx.Response(200, text="<html>final</html>")
    client_factory(monkeypatch, handler)
    assert "final" in page_fetcher.fetch_simple("https://public.test/job")


def test_redirect_loop_is_cut_off(monkeypatch):
    resolve_to(monkeypatch, {"public.test": ["93.184.216.34"]})
    client_factory(monkeypatch, lambda request: httpx.Response(302, headers={"location": "/again"}))
    with pytest.raises(UnsafeUrl):
        page_fetcher.fetch_simple("https://public.test/")


def test_response_size_is_capped(monkeypatch):
    resolve_to(monkeypatch, {"public.test": ["93.184.216.34"]})
    client_factory(monkeypatch, lambda request: httpx.Response(200, content=b"a" * (page_fetcher.MAX_BYTES * 3)))
    assert len(page_fetcher.fetch_simple("https://public.test/")) == page_fetcher.MAX_BYTES


def test_fetch_failures_fall_back_without_crashing(monkeypatch):
    """An unsafe URL must surface as 'could not fetch' (snippet fallback), never as a request to the internal host."""
    resolve_to(monkeypatch, {"internal.test": ["127.0.0.1"]})
    monkeypatch.setattr(settings, "dev_fixtures", False)
    monkeypatch.setattr(settings, "playwright_fallback", False)
    monkeypatch.setattr(settings, "firecrawl_api_key", "")
    r = SearchResult(url="http://internal.test/", title="t", snippet="s", source_query="q", platform="web")
    html, fetched = page_fetcher.fetch_page_for_result(r)
    assert fetched is False and "s" in html


# ---------- stored URLs ----------
@pytest.mark.parametrize("bad", [None, "", "javascript:alert(1)", "data:text/html,<script>", "ftp://x.test/a", "//x.test/a",
                                 "/relative/path", "https://u:p@x.test/", "https://x.test/a b", "https://x.test/a\nb", "https://x.test/a\x00b", "https://" + "a" * 2100 + ".test/"])
def test_safe_job_url_rejects(bad):
    assert safe_job_url(bad) is None


def test_safe_job_url_accepts_normal_links():
    assert safe_job_url("  https://jobs.lever.co/acme/1?x=1  ") == "https://jobs.lever.co/acme/1?x=1"
    assert safe_job_url("http://example.org/a") == "http://example.org/a"


def test_dangerous_apply_url_from_the_model_is_never_stored():
    from contextlib import contextmanager
    from sqlalchemy import create_engine, select
    from sqlalchemy.orm import sessionmaker
    from app.db import Base
    from app.models import Job
    from app.scrapers import pipeline
    engine = create_engine("sqlite://")
    Base.metadata.create_all(engine)
    S = sessionmaker(bind=engine)
    db = S()
    src = pipeline._get_or_create_source(db, "k", "google_search", "k")
    good = SearchResult(url="https://example.org/real", title="t", snippet="s", source_query="q", platform="web")
    ex = ExtractedJob(is_real_job=True, title="Copywriter", apply_url="javascript:alert(document.cookie)")
    pipeline._upsert_job(db, src, good, ex, set())
    assert db.scalar(select(Job)).source_url == "https://example.org/real"      # fell back to the page we found
    bad = good.model_copy(update={"url": "javascript:alert(1)"})
    assert pipeline._upsert_job(db, src, bad, ex, set()) == (False, False)
    assert db.query(Job).count() == 1


def test_problogger_relative_links_become_absolute(monkeypatch):
    html = '<html><body><article><a href="/jobs/email-writer/">Email Writer</a> remote</article></body></html>'
    monkeypatch.setattr(problogger.httpx, "get", lambda *a, **k: httpx.Response(200, text=html, request=httpx.Request("GET", "https://problogger.com/jobs/")))
    rows = problogger.fetch_listings()
    assert rows[0]["url"] == "https://problogger.com/jobs/email-writer/"


# ---------- app surface ----------
def test_docs_and_schema_are_not_public_by_default():
    c = TestClient(app)
    for path in ("/docs", "/redoc", "/openapi.json"):
        assert c.get(path).status_code == 404


def test_security_headers_on_every_response_and_csp_on_admin():
    c = TestClient(app)
    r = c.get("/health")
    assert r.headers["x-content-type-options"] == "nosniff" and r.headers["x-frame-options"] == "DENY"
    assert "max-age" in r.headers["strict-transport-security"] and r.headers["referrer-policy"] == "same-origin"
    a = c.get("/admin/sources")
    assert a.headers["cache-control"] == "no-store" and "script-src 'self'" in a.headers["content-security-policy"]
    assert "content-security-policy" not in r.headers          # landing page keeps working


def test_risky_defaults_are_off():
    fields = type(settings).model_fields
    assert fields["playwright_fallback"].default is False
    assert fields["enable_docs"].default is False
    assert fields["dev_fixtures"].default is False
