"""Deterministic dedupe hash for a job.
Same URL = same job. If no URL, hash title + company."""
import hashlib
import re
from urllib.parse import parse_qsl, urlencode, urlparse, urlunparse


_whitespace = re.compile(r"\s+")

# Tracking params to strip on every host. Job-identifying params (gh_jid, source, id) stay.
TRACKING_PARAMS: set[str] = {
    "utm_source", "utm_medium", "utm_campaign", "utm_term", "utm_content",
    "ref", "ref_src", "fbclid", "gclid", "msclkid", "mc_cid", "mc_eid",
}

# Short params that are tracking on Twitter/X and job identifiers elsewhere.
_TWITTER_HOSTS = {
    "twitter.com", "www.twitter.com", "mobile.twitter.com",
    "x.com", "www.x.com",
}
_TWITTER_TRACKING = {"s", "t"}


def _host(netloc: str) -> str:
    return netloc.lower().split(":")[0]


def normalize_url(url: str) -> str:
    """Lowercase scheme/host, strip fragment and tracking query params, drop trailing slash.

    Non-tracking query params stay. Many job IDs live there (e.g. ?gh_jid=12345, ?source=12345).
    """
    raw = (url or "").strip()
    if not raw:
        return ""
    try:
        p = urlparse(raw)
        scheme = (p.scheme or "https").lower()
        netloc = p.netloc.lower()
        path = p.path.rstrip("/")
        drop = set(TRACKING_PARAMS)
        if _host(netloc) in _TWITTER_HOSTS:
            drop |= _TWITTER_TRACKING
        kept = [
            (k, v)
            for (k, v) in parse_qsl(p.query, keep_blank_values=False)
            if k.lower() not in drop
        ]
        kept.sort()
        query = urlencode(kept)
        return urlunparse((scheme, netloc, path, "", query, ""))
    except Exception:
        return raw.lower()


def norm_text(s: str | None) -> str:
    if not s:
        return ""
    return _whitespace.sub(" ", s.strip().lower())


def dedupe_hash(*, url: str | None, title: str, company: str | None = None) -> str:
    """Primary key: normalized URL. Fallback: title+company."""
    if url:
        key = normalize_url(url)
    else:
        key = f"{norm_text(title)}|{norm_text(company)}"
    return hashlib.sha256(key.encode("utf-8")).hexdigest()[:32]
