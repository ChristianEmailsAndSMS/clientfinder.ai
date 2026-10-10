"""Google-search layer. Returns SearchResult rows for a given query.

Real mode: hits SerpAPI (or Serper.dev if configured).
Dev mode (DEV_FIXTURES=1): reads a local JSON fixture — lets you iterate without an API key."""
import json
from pathlib import Path
from typing import Iterable
from urllib.parse import urlparse

import httpx

from ..config import settings
from ..schemas import SearchResult

FIXTURE_DIR = Path(__file__).resolve().parent.parent.parent / "fixtures"


def _fixture_results(query: str) -> list[SearchResult]:
    # Pick the most-matching fixture file; fall back to the default.
    slug = query.lower().replace(" ", "_").replace('"', "")
    candidates = [FIXTURE_DIR / f"serpapi_{slug}.json", FIXTURE_DIR / "serpapi_hiring_copywriter.json"]
    for f in candidates:
        if f.exists():
            raw = json.loads(f.read_text())
            # SerpAPI-shaped: {"organic_results": [{"link": ..., "title": ..., "snippet": ...}]}
            out: list[SearchResult] = []
            for item in raw.get("organic_results", []):
                url = item.get("link") or item.get("url")
                if not url:
                    continue
                out.append(SearchResult(
                    url=url,
                    title=item.get("title", ""),
                    snippet=item.get("snippet", "") or item.get("description", ""),
                    source_query=query,
                    platform=_platform_from_url(url),
                ))
            return out
    return []


_HOST_PLATFORMS = (
    ("twitter.com", "twitter"),
    ("x.com", "twitter"),
    ("reddit.com", "reddit"),
    ("linkedin.com", "linkedin"),
    ("upwork.com", "upwork"),
    ("indeed.com", "indeed"),
    ("problogger.com", "problogger"),
    ("mediabistro.com", "mediabistro"),
    ("greenhouse.io", "greenhouse"),
    ("lever.co", "lever"),
    ("ashbyhq.com", "ashby"),
)


def _platform_from_url(url: str) -> str:
    host = (urlparse(url).hostname or "").lower().removeprefix("www.")
    for domain, name in _HOST_PLATFORMS:
        if host == domain or host.endswith("." + domain):
            return name
    return "web"


def _serpapi_results(query: str, num: int = 20) -> list[SearchResult]:
    r = httpx.get(
        "https://serpapi.com/search.json",
        params={
            "q": query,
            "api_key": settings.serpapi_api_key,
            "num": num,
            "hl": "en",
        },
        timeout=30,
    )
    r.raise_for_status()
    data = r.json()
    out: list[SearchResult] = []
    for item in data.get("organic_results", []):
        url = item.get("link")
        if not url:
            continue
        out.append(SearchResult(
            url=url,
            title=item.get("title", ""),
            snippet=item.get("snippet", ""),
            source_query=query,
            platform=_platform_from_url(url),
        ))
    return out


def _serper_results(query: str, num: int = 20) -> list[SearchResult]:
    r = httpx.post(
        "https://google.serper.dev/search",
        headers={"X-API-KEY": settings.serper_api_key, "Content-Type": "application/json"},
        json={"q": query, "num": num, "gl": "us", "hl": "en"},
        timeout=30,
    )
    r.raise_for_status()
    data = r.json()
    out: list[SearchResult] = []
    for item in data.get("organic", []):
        url = item.get("link")
        if not url:
            continue
        out.append(SearchResult(
            url=url,
            title=item.get("title", ""),
            snippet=item.get("snippet", ""),
            source_query=query,
            platform=_platform_from_url(url),
        ))
    return out


def search(query: str, num: int = 20) -> list[SearchResult]:
    if settings.dev_fixtures:
        return _fixture_results(query)[:num]
    if settings.google_search_provider == "serper" and settings.serper_api_key:
        return _serper_results(query, num=num)
    if settings.serpapi_api_key:
        return _serpapi_results(query, num=num)
    # No key configured — fall back to fixture so dev flow still runs.
    return _fixture_results(query)[:num]


# Default queries we rotate through on scheduled runs.
DEFAULT_QUERIES: tuple[str, ...] = (
    '"hiring copywriter"',
    '"hiring email copywriter"',
    '"hiring email marketer"',
    '"hiring creative strategist"',
    '"hiring landing page builder"',
    '"hiring funnel builder"',
    '"looking for a copywriter"',
    '"need a copywriter"',
    '"copywriter wanted"',
)


def search_all(queries: Iterable[str] = DEFAULT_QUERIES, num: int = 20) -> list[SearchResult]:
    results: list[SearchResult] = []
    for q in queries:
        results.extend(search(q, num=num))
    return results
