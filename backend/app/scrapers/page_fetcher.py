"""Fetch rendered HTML for a URL.

Default: httpx + a browser-ish User-Agent (fast, works for most non-JS pages).
Upgrade path: Playwright (if installed) for JS-rendered pages, Firecrawl API (if key set) for hostile sites.

In DEV_FIXTURES mode we don't actually fetch — we read the SerpAPI fixture's snippet-plus-title so the pipeline
can run offline. Real page fetch kicks in when DEV_FIXTURES=0."""
from __future__ import annotations

import html
import logging

import httpx
from bs4 import BeautifulSoup

from urllib.parse import urljoin

from ..config import settings
from ..schemas import SearchResult
from ..security_utils import UnsafeUrl, assert_public_url

log = logging.getLogger(__name__)

UA = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/130.0.0.0 Safari/537.36"
)


MAX_BYTES = 2_000_000     # a job page is tiny; this stops memory bombs (incl. decompression bombs)
MAX_REDIRECTS = 5


def fetch_simple(url: str, timeout: float = 20.0) -> str:
    """GET with the SSRF guard on the first URL and on every redirect hop, and a hard cap on body size."""
    headers = {"User-Agent": UA, "Accept-Language": "en-US,en;q=0.9"}
    current = url
    with httpx.Client(timeout=timeout, follow_redirects=False, headers=headers) as client:
        for _ in range(MAX_REDIRECTS + 1):
            assert_public_url(current)
            with client.stream("GET", current) as r:
                if r.is_redirect and r.headers.get("location"):
                    current = urljoin(current, r.headers["location"])
                    continue
                r.raise_for_status()
                body = bytearray()
                for chunk in r.iter_bytes():
                    body.extend(chunk)
                    if len(body) > MAX_BYTES:
                        break
                return bytes(body[:MAX_BYTES]).decode(r.encoding or "utf-8", errors="replace")
    raise UnsafeUrl("too many redirects")


def fetch_firecrawl(url: str) -> str:
    """Fallback: Firecrawl returns clean markdown. Requires FIRECRAWL_API_KEY."""
    if not settings.firecrawl_api_key:
        raise RuntimeError("FIRECRAWL_API_KEY not set")
    if not url.lower().startswith(("http://", "https://")):
        raise UnsafeUrl("scheme not allowed")
    r = httpx.post(
        "https://api.firecrawl.dev/v1/scrape",
        headers={"Authorization": f"Bearer {settings.firecrawl_api_key}", "Content-Type": "application/json"},
        json={"url": url, "formats": ["markdown"]},
        timeout=60,
    )
    r.raise_for_status()
    data = r.json()
    return data.get("data", {}).get("markdown", "")


def fetch_playwright(url: str) -> str:
    """JS-rendered pages. Playwright must be installed and `playwright install chromium` run once."""
    assert_public_url(url)
    try:
        from playwright.sync_api import sync_playwright
    except ImportError:
        raise RuntimeError("playwright not installed; `pip install playwright && playwright install chromium`")
    with sync_playwright() as pw:
        browser = pw.chromium.launch(headless=True)
        try:
            page = browser.new_page(user_agent=UA)

            def guard(route):
                try:
                    assert_public_url(route.request.url)
                    route.continue_()
                except UnsafeUrl:
                    route.abort()

            page.route("**/*", guard)
            page.goto(url, wait_until="networkidle", timeout=30_000)
            return page.content()
        finally:
            browser.close()


def fetch(url: str, *, mode: str = "simple") -> str:
    """mode: simple | playwright | firecrawl"""
    if mode == "playwright":
        return fetch_playwright(url)
    if mode == "firecrawl":
        return fetch_firecrawl(url)
    return fetch_simple(url)


# Platforms whose pages need a login or are JS shells for anonymous visitors. The Google title + snippet
# usually carry the post text, so we extract from that instead of burning a fetch (and risking a block).
SNIPPET_ONLY_PLATFORMS = {"twitter", "linkedin", "instagram"}
MIN_VISIBLE_CHARS = 300   # less than this from a plain fetch = probably a JS shell or a block page


def _visible_len(page_html: str) -> int:
    return len(BeautifulSoup(page_html, "html.parser").get_text(" ", strip=True))


def _snippet_page(result: SearchResult) -> str:
    return (
        f"<!doctype html><html><body><h1>{html.escape(result.title)}</h1>"
        f"<p>{html.escape(result.snippet)}</p><a href='{html.escape(result.url)}'>Apply</a></body></html>"
    )


def fetch_robust(url: str) -> tuple[str | None, str]:
    """Plain HTTP -> Playwright (JS pages / soft blocks) -> Firecrawl (hostile sites). Returns (html, mode) or
    (None, "none") if every available mode failed or returned a shell."""
    modes = ["simple"]
    if settings.playwright_fallback:
        modes.append("playwright")
    if settings.firecrawl_api_key:
        modes.append("firecrawl")
    for mode in modes:
        try:
            page = fetch(url, mode=mode)
        except Exception as e:  # HTTP 403/429, timeout, playwright missing: try the next mode
            log.info("fetch %s via %s failed: %s", url, mode, e)
            continue
        if page and _visible_len(page) >= MIN_VISIBLE_CHARS:
            return page, mode
        log.info("fetch %s via %s returned too little text", url, mode)
    return None, "none"


def fetch_page_for_result(result: SearchResult) -> tuple[str, bool]:
    """Returns (html, fetched). fetched=False means we fell back to the search snippet."""
    if settings.dev_fixtures:
        return _snippet_page(result), False
    if result.platform in SNIPPET_ONLY_PLATFORMS:
        return _snippet_page(result), False
    page, _mode = fetch_robust(result.url)
    if page is None:
        return _snippet_page(result), False
    return page, True


def fetch_for_result(result: SearchResult) -> str:
    return fetch_page_for_result(result)[0]
