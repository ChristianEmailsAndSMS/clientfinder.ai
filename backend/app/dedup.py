"""Deterministic dedupe hash for a job.
Same URL = same job. If no URL, hash title + company."""
import hashlib
import re
from urllib.parse import urlparse, urlunparse, parse_qsl, urlencode


_whitespace = re.compile(r"\s+")

# Tracking params to strip — they add noise but don't identify the job.
# `s` and `t` are Twitter click ids. On other hosts they can be the job id.
TRACKING_PARAMS: set[str] = {
    "utm_source", "utm_medium", "utm_campaign", "utm_term", "utm_content",
    "ref", "ref_src", "source", "fbclid", "gclid", "msclkid", "mc_cid", "mc_eid",
}
_TWITTER_TRACKING = {"s", "t"}


def _host(netloc: str) -> str:
    host = netloc.split("@")[-1].split(":")[0].lower()
    if host.startswith("www."):
        host = host[4:]
    if host in {"x.com", "mobile.twitter.com"} or host.endswith(".twitter.com"):
        return "twitter.com"
    return host


def normalize_url(url: str) -> str:
    """Lowercase scheme/host, strip fragment and tracking query params, drop trailing slash.
    IMPORTANT: keep non-tracking query params — many job IDs live there (e.g. ?gh_jid=12345)."""
    try:
        p = urlparse(url.strip())
        scheme = (p.scheme or "https").lower()
        host = _host(p.netloc)
        if not host and not p.path:
            return url.strip().lower()
        path = p.path.rstrip("/")
        tracking = set(TRACKING_PARAMS)
        if host == "twitter.com":
            tracking |= _TWITTER_TRACKING
        kept = [(k, v) for (k, v) in parse_qsl(p.query, keep_blank_values=False) if k.lower() not in tracking]
        kept.sort()  # stable order for stable hashing
        query = urlencode(kept)
        return urlunparse((scheme, host, path, "", query, ""))
    except Exception:
        return url.strip().lower()


def norm_text(s: str | None) -> str:
    if not s:
        return ""
    return _whitespace.sub(" ", s.strip().lower())


def dedupe_hash(*, url: str | None, title: str, company: str | None = None) -> str:
    """Primary key: normalized URL. Fallback: title+company."""
    key = normalize_url(url) if url else ""
    if not key or key in {"https://", "http://"}:
        key = f"{norm_text(title)}|{norm_text(company)}"
    return hashlib.sha256(key.encode("utf-8")).hexdigest()[:32]
