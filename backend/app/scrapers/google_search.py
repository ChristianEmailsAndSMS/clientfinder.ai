"""Google-search layer. Returns SearchResult rows for a given query.

Real mode: hits SerpAPI, and Serper if that account is out of searches (or Serper first when configured).
Dev mode (DEV_FIXTURES=1): reads a local JSON fixture — lets you iterate without an API key.

A query that says site:linkedin.com never keeps a result from another host. Google sometimes
ignores site: and fills the page with job-board category URLs; those are not the search we ran."""
import json
import logging
import re
from pathlib import Path
from typing import Iterable
from urllib.parse import urlparse

import httpx

from ..config import settings
from ..schemas import SearchResult

log = logging.getLogger(__name__)

# x.com and twitter.com are the same site. A site:x.com search may still link to twitter.com.
_HOST_ALIASES = {"x.com": "twitter.com", "twitter.com": "x.com"}
_SITE = re.compile(r"\bsite:(\S+)", re.I)


class SearchError(RuntimeError):
    """A search call failed. The message never contains the request URL (it carries the API key)."""

    def __init__(self, message: str, *, quota: bool = False):
        super().__init__(message)
        self.quota = quota


def _quota_text(text: str) -> bool:
    low = (text or "").lower()
    return "run out of searches" in low or "not enough credits" in low or "quota exceeded" in low


def _checked(provider: str, r: httpx.Response) -> httpx.Response:
    """Turn a failed response into SearchError. Never include the request URL (it carries the key)."""
    status = getattr(r, "status_code", 200) or 200
    body = getattr(r, "text", "") or ""
    if status == 429 or _quota_text(body):
        raise SearchError(f"{provider} is out of searches", quota=True)
    if status >= 400:
        raise SearchError(f"{provider} HTTP {status}")
    return r


def _http_get(provider: str, url: str, **kw) -> httpx.Response:
    try:
        return _checked(provider, httpx.get(url, **kw))
    except SearchError:
        raise
    except httpx.HTTPError as e:
        raise SearchError(f"{provider} request failed: {type(e).__name__}") from None


def _http_post(provider: str, url: str, **kw) -> httpx.Response:
    try:
        return _checked(provider, httpx.post(url, **kw))
    except SearchError:
        raise
    except httpx.HTTPError as e:
        raise SearchError(f"{provider} request failed: {type(e).__name__}") from None


def site_constraint(query: str) -> str | None:
    m = _SITE.search(query or "")
    return m.group(1).lower() if m else None


def on_site(url: str, site: str) -> bool:
    """True when url is on the host (and path, if site: included one) the query asked for."""
    try:
        parsed = urlparse(url)
    except ValueError:
        return False
    host = (parsed.netloc or "").lower().removeprefix("www.")
    site_host, _, site_path = site.lower().partition("/")
    site_host = site_host.removeprefix("www.")
    hosts = {site_host}
    alt = _HOST_ALIASES.get(site_host)
    if alt:
        hosts.add(alt)
    if not any(host == h or host.endswith("." + h) for h in hosts):
        return False
    if site_path and site_path not in (parsed.path or "").lower().lstrip("/"):
        return False
    return True


def _filter_site(query: str, results: list[SearchResult]) -> list[SearchResult]:
    site = site_constraint(query)
    if not site:
        return results
    kept = [r for r in results if on_site(r.url, site)]
    dropped = len(results) - len(kept)
    if dropped:
        log.info("dropped %d off-site results for site:%s", dropped, site)
    return kept


def _provider_order() -> list[str]:
    """Configured provider first. The other one is only a fallback when the first is out of searches."""
    primary = "serper" if (settings.google_search_provider == "serper" and settings.serper_api_key) else "serpapi"
    secondary = "serpapi" if primary == "serper" else "serper"
    order = []
    for name in (primary, secondary):
        key = settings.serper_api_key if name == "serper" else settings.serpapi_api_key
        if key and name not in order:
            order.append(name)
    return order

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


# Longest-suffix wins is unnecessary: each suffix is a registrable host. remote.co does not match
# euremotejobs.com because the check is the host, not a substring of the whole URL.
_PLATFORM_HOSTS: tuple[tuple[str, str], ...] = (
    ("dynamitejobs.com", "dynamite"),
    ("himalayas.app", "himalayas"),
    ("tryremotely.com", "remotely"),
    ("nodesk.co", "nodesk"),
    ("workingnomads.com", "workingnomads"),
    ("weworkremotely.com", "weworkremotely"),
    ("4dayweek.io", "fourdayweek"),
    ("workable.com", "workable"),
    ("contentjobs.co", "contentjobs"),
    ("jobspresso.co", "jobspresso"),
    ("creatorjobboard.com", "creatorjobboard"),
    ("euremotejobs.com", "euremotejobs"),
    ("remoterocketship.com", "remoterocketship"),
    ("jobicy.com", "jobicy"),
    ("remoteok.com", "remoteok"),
    ("remotive.com", "remotive"),
    ("remote.co", "remoteco"),
    ("twitter.com", "twitter"),
    ("x.com", "twitter"),
    ("facebook.com", "facebook"),
    ("fb.com", "facebook"),
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
    host = (urlparse(url).netloc or "").lower().removeprefix("www.")
    for suffix, name in _PLATFORM_HOSTS:
        if host == suffix or host.endswith("." + suffix):
            return name
    return "web"


def _serpapi_results(query: str, num: int = 10, freshness: str | None = None) -> list[SearchResult]:
    params = {"q": query, "api_key": settings.serpapi_api_key, "num": num, "hl": "en", "gl": "us"}
    if freshness:
        params["tbs"] = f"qdr:{freshness}"   # d = past 24h, w = past week, m = past month, m3/m6/m9 = past N months, y = past year
    r = _http_get("serpapi", "https://serpapi.com/search.json", params=params, timeout=30)
    data = r.json()
    err = data.get("error") if isinstance(data, dict) else None
    if isinstance(err, str) and _quota_text(err):
        raise SearchError("serpapi is out of searches", quota=True)
    if isinstance(err, str):
        log.info("serpapi returned no results: %s", err[:200])
        return []
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


def _call_provider(provider: str, query: str, num: int, freshness: str | None) -> list[SearchResult]:
    if provider == "serper":
        return _serper_results(query, num=num, freshness=freshness)
    return _serpapi_results(query, num=num, freshness=freshness)


def search(query: str, num: int = 10, freshness: str | None = None) -> list[SearchResult]:
    if settings.dev_fixtures:
        return _filter_site(query, _fixture_results(query))
    order = _provider_order()
    if not order:
        # No key configured. Never fall back to fixtures here: they are fake and would end up in the live DB.
        log.warning("no search API key configured; skipping %r (set DEV_FIXTURES=1 for fake local data)", query)
        return []
    last: SearchError | None = None
    for provider in order:
        try:
            return _filter_site(query, _call_provider(provider, query, num, freshness))
        except SearchError as e:
            last = e
            if e.quota and provider != order[-1]:
                log.warning("%s is out of searches; trying the next provider", provider)
                continue
            raise
    if last:
        raise last
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
