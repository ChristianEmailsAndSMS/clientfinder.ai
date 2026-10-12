"""Deterministic dedupe hash for a job.
Same URL = same job. If no URL, hash title + company."""
import hashlib
import re
from urllib.parse import urlparse, urlunparse, parse_qsl, urlencode


_whitespace = re.compile(r"\s+")

# Tracking params to strip — they add noise but don't identify the job.
TRACKING_PARAMS: set[str] = {
    "utm_source", "utm_medium", "utm_campaign", "utm_term", "utm_content",
    "ref", "ref_src", "source", "fbclid", "gclid", "msclkid", "mc_cid", "mc_eid",
    "gh_src", "lever-source", "lever-origin", "trackingid", "trk", "refid", "tk", "referrer_url_path", "igshid",
}
# "s" and "t" are Twitter/X share trackers, but on other sites they are real ids (forum ?t=<thread>, search ?s=<term>): strip them there only.
TWITTER_ONLY_PARAMS: set[str] = {"s", "t"}
_TWITTER_HOSTS = {"twitter.com", "x.com", "mobile.twitter.com", "www.twitter.com", "www.x.com"}


def normalize_url(url: str) -> str:
    """Lowercase scheme/host, strip fragment and tracking query params, drop trailing slash.
    IMPORTANT: keep non-tracking query params — many job IDs live there (e.g. ?gh_jid=12345)."""
    try:
        p = urlparse(url.strip())
        scheme = (p.scheme or "https").lower()
        netloc = p.netloc.lower()
        path = p.path.rstrip("/")
        drop = TRACKING_PARAMS | (TWITTER_ONLY_PARAMS if netloc in _TWITTER_HOSTS else set())
        kept = [(k, v) for (k, v) in parse_qsl(p.query, keep_blank_values=False) if k.lower() not in drop]
        kept.sort()  # stable order for stable hashing
        query = urlencode(kept)
        return urlunparse((scheme, netloc, path, "", query, ""))
    except Exception:
        return url.strip().lower()


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
