"""Deterministic dedupe hash for a job.
Same URL = same job. If no URL, hash title + company."""
import hashlib
import re
from urllib.parse import parse_qsl, urlencode, urlparse, urlunparse


_whitespace = re.compile(r"\s+")

# Campaign tags only. Short keys like t/s/ref/source are job ids on some boards.
TRACKING_PARAMS: set[str] = {
    "utm_source", "utm_medium", "utm_campaign", "utm_term", "utm_content", "utm_id",
    "fbclid", "gclid", "msclkid", "mc_cid", "mc_eid",
}


def normalize_url(url: str) -> str:
    """Lowercase host, force https, drop a leading www and tracking params.

    Fragments and non-tracking query params stay. Job ids often live in either
    (`?gh_jid=`, `#/job/1`). A URL with no host is not an identity.
    """
    raw = (url or "").strip()
    if not raw:
        return ""
    try:
        p = urlparse(raw)
    except Exception:
        return ""
    netloc = (p.netloc or "").lower()
    if netloc.startswith("www."):
        netloc = netloc[4:]
    if netloc.endswith(":443") or netloc.endswith(":80"):
        netloc = netloc.rsplit(":", 1)[0]
    if not netloc:
        return ""
    path = p.path.rstrip("/")
    kept = [
        (k, v)
        for (k, v) in parse_qsl(p.query, keep_blank_values=False)
        if k.lower() not in TRACKING_PARAMS
    ]
    kept.sort()
    return urlunparse(("https", netloc, path, "", urlencode(kept), p.fragment))


def norm_text(s: str | None) -> str:
    if not s:
        return ""
    return _whitespace.sub(" ", s.strip().lower())


def dedupe_hash(*, url: str | None, title: str, company: str | None = None) -> str:
    """Primary key: normalized URL. Fallback: title+company."""
    key = normalize_url(url) if url and str(url).strip() else ""
    if not key:
        key = f"{norm_text(title)}|{norm_text(company)}"
    return hashlib.sha256(key.encode("utf-8")).hexdigest()[:32]
