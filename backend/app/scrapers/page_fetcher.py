"""Fetch rendered HTML for a URL.

Default: httpx + a browser-ish User-Agent (fast, works for most non-JS pages).
Upgrade path: Playwright (if installed) for JS-rendered pages, Firecrawl API (if key set) for hostile sites.

In DEV_FIXTURES mode we don't actually fetch — we read the SerpAPI fixture's snippet-plus-title so the pipeline
can run offline. Real page fetch kicks in when DEV_FIXTURES=0."""
from __future__ import annotations

import httpx

from ..config import settings
from ..schemas import SearchResult

UA = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/130.0.0.0 Safari/537.36"
)


def fetch_simple(url: str, timeout: float = 20.0) -> str:
    r = httpx.get(url, headers={"User-Agent": UA, "Accept-Language": "en-US,en;q=0.9"},
                  timeout=timeout, follow_redirects=True)
    r.raise_for_status()
    return r.text


def fetch_firecrawl(url: str) -> str:
    """Fallback: Firecrawl returns clean markdown. Requires FIRECRAWL_API_KEY."""
    if not settings.firecrawl_api_key:
        raise RuntimeError("FIRECRAWL_API_KEY not set")
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
    try:
        from playwright.sync_api import sync_playwright
    except ImportError:
        raise RuntimeError("playwright not installed; `pip install playwright && playwright install chromium`")
    with sync_playwright() as pw:
        browser = pw.chromium.launch(headless=True)
        try:
            page = browser.new_page(user_agent=UA)
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


def fetch_for_result(result: SearchResult) -> str:
    """In dev-fixture mode we synthesize a minimal page from the snippet.
    This lets the whole pipeline run offline. Real mode calls fetch()."""
    if settings.dev_fixtures:
        return (
            f"<!doctype html><html><body>"
            f"<h1>{result.title}</h1>"
            f"<p>{result.snippet}</p>"
            f"<a href='{result.url}'>Apply</a>"
            f"</body></html>"
        )
    return fetch(result.url)
