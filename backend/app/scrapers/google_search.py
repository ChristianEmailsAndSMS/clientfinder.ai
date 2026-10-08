"""Google-search layer. Returns SearchResult rows for a given query.

Real mode: hits SerpAPI (or Serper.dev if configured).
Dev mode (DEV_FIXTURES=1): reads a local JSON fixture — lets you iterate without an API key."""
import json
import logging
from pathlib import Path
from typing import Iterable

import httpx

from ..config import settings
from ..schemas import SearchResult

log = logging.getLogger(__name__)


class SearchError(RuntimeError):
    """A search call failed. The message never contains the request URL (it carries the API key)."""


def _http_get(provider: str, url: str, **kw) -> httpx.Response:
    try:
        r = httpx.get(url, **kw)
        r.raise_for_status()
        return r
    except httpx.HTTPStatusError as e:
        raise SearchError(f"{provider} HTTP {e.response.status_code}") from None
    except httpx.HTTPError as e:
        raise SearchError(f"{provider} request failed: {type(e).__name__}") from None


def _http_post(provider: str, url: str, **kw) -> httpx.Response:
    try:
        r = httpx.post(url, **kw)
        r.raise_for_status()
        return r
    except httpx.HTTPStatusError as e:
        raise SearchError(f"{provider} HTTP {e.response.status_code}") from None
    except httpx.HTTPError as e:
        raise SearchError(f"{provider} request failed: {type(e).__name__}") from None

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


def _platform_from_url(url: str) -> str:
    u = url.lower()
    if "twitter.com" in u or "x.com" in u: return "twitter"
    if "reddit.com" in u: return "reddit"
    if "linkedin.com" in u: return "linkedin"
    if "upwork.com" in u: return "upwork"
    if "indeed.com" in u: return "indeed"
    if "problogger.com" in u: return "problogger"
    if "mediabistro.com" in u: return "mediabistro"
    if "greenhouse.io" in u: return "greenhouse"
    if "lever.co" in u: return "lever"
    if "ashbyhq.com" in u: return "ashby"
    return "web"


def _serpapi_results(query: str, num: int = 10, freshness: str | None = None) -> list[SearchResult]:
    params = {"q": query, "api_key": settings.serpapi_api_key, "num": num, "hl": "en", "gl": "us"}
    if freshness:
        params["tbs"] = f"qdr:{freshness}"   # d = past 24h, w = past week, m = past month, m3/m6/m9 = past N months, y = past year
    r = _http_get("serpapi", "https://serpapi.com/search.json", params=params, timeout=30)
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


def _serper_results(query: str, num: int = 10, freshness: str | None = None) -> list[SearchResult]:
    r = _http_post(
        "serper", "https://google.serper.dev/search",
        headers={"X-API-KEY": settings.serper_api_key, "Content-Type": "application/json"},
        json={"q": query, "num": num, "gl": "us", "hl": "en", **({"tbs": f"qdr:{freshness}"} if freshness else {})},
        timeout=30,
    )
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


def search(query: str, num: int = 10, freshness: str | None = None) -> list[SearchResult]:
    if settings.dev_fixtures:
        return _fixture_results(query)
    if settings.google_search_provider == "serper" and settings.serper_api_key:
        return _serper_results(query, num=num, freshness=freshness)
    if settings.serpapi_api_key:
        return _serpapi_results(query, num=num, freshness=freshness)
    # No key configured. Never fall back to fixtures here: they are fake and would end up in the live DB.
    log.warning("no search API key configured; skipping %r (set DEV_FIXTURES=1 for fake local data)", query)
    return []


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
