"""Google-search layer. Returns SearchResult rows for a given query.

Real mode: hits SerpAPI (or Serper.dev if configured).
Dev mode (DEV_FIXTURES=1): reads a local JSON fixture — lets you iterate without an API key."""
import json
from pathlib import Path
from typing import Iterable

import httpx

from ..config import settings
from ..schemas import SearchResult

FIXTURE_DIR = Path(__file__).resolve().parent.parent.parent / "fixtures"


def _result_from_item(item: dict, query: str) -> SearchResult | None:
    url = item.get("link") or item.get("url")
    if not url:
        return None
    return SearchResult(
        url=url,
        title=item.get("title") or "",
        snippet=item.get("snippet") or item.get("description") or "",
        source_query=query,
        platform=_platform_from_url(url),
    )


def _fixture_results(query: str, num: int = 20) -> list[SearchResult]:
    # Pick the most-matching fixture file; fall back to the default.
    # An empty per-query file must not hide the shared fixture.
    slug = query.lower().replace(" ", "_").replace('"', "")
    candidates = [FIXTURE_DIR / f"serpapi_{slug}.json", FIXTURE_DIR / "serpapi_hiring_copywriter.json"]
    seen: set[Path] = set()
    for f in candidates:
        if f in seen or not f.exists():
            continue
        seen.add(f)
        raw = json.loads(f.read_text())
        out: list[SearchResult] = []
        for item in raw.get("organic_results", [])[:num]:
            row = _result_from_item(item, query)
            if row:
                out.append(row)
        if out:
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
        row = _result_from_item(item, query)
        if row:
            out.append(row)
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
        row = _result_from_item(item, query)
        if row:
            out.append(row)
    return out


def search(query: str, num: int = 20) -> list[SearchResult]:
    if settings.dev_fixtures:
        return _fixture_results(query, num=num)
    if settings.google_search_provider == "serper" and settings.serper_api_key:
        return _serper_results(query, num=num)
    if settings.serpapi_api_key:
        return _serpapi_results(query, num=num)
    # No key configured — fall back to fixture so dev flow still runs.
    return _fixture_results(query, num=num)


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
