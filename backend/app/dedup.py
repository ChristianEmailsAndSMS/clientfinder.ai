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
# Same site under another name: fold to one host so one page gets one hash.
_HOST_ALIASES = {"twitter.com": "x.com", "mobile.twitter.com": "x.com", "m.twitter.com": "x.com",
                 "old.reddit.com": "reddit.com", "np.reddit.com": "reddit.com", "new.reddit.com": "reddit.com",
                 "i.reddit.com": "reddit.com", "m.reddit.com": "reddit.com"}
# Extra tracking params that are only noise on one site (the job id is elsewhere in the URL).
_SITE_TRACKING = {"indeed.com": {"from", "vjs", "q", "l"}, "reddit.com": {"utm_name", "context", "share_id"},
                  "glassdoor.com": {"pos", "ao", "src", "vt", "cs", "guid"}}


def _site_key(host: str) -> str:
    for d in _SITE_TRACKING:
        if host == d or host.endswith("." + d):
            return d
    return ""


def normalize_url(url: str) -> str:
    """Lowercase scheme/host, strip fragment and tracking query params, drop trailing slash.
    IMPORTANT: keep non-tracking query params — many job IDs live there (e.g. ?gh_jid=12345)."""
    try:
        p = urlparse(url.strip())
        scheme = "https" if (p.scheme or "https").lower() == "http" else (p.scheme or "https").lower()
        netloc = p.netloc.lower()
        is_twitter = netloc in _TWITTER_HOSTS
        if netloc.startswith("www.") and "@" not in netloc:
            netloc = netloc[4:]
        netloc = _HOST_ALIASES.get(netloc, netloc)
        path = p.path.rstrip("/")
        drop = TRACKING_PARAMS | (TWITTER_ONLY_PARAMS if is_twitter else set()) | _SITE_TRACKING.get(_site_key(netloc), set())
        kept = [(k, v) for (k, v) in parse_qsl(p.query, keep_blank_values=False) if k.lower() not in drop]
        if (netloc == "linkedin.com" or netloc.endswith(".linkedin.com")) and path.startswith("/jobs/view/"):
            kept = []  # the job id is in the path; every query param there is tracking
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


_GENERIC_COMPANIES = {"", "confidential", "anonymous", "unknown", "n/a", "na", "company", "hiring", "stealth", "undisclosed"}


def find_same_job(db, title: str | None, company: str | None, location: str | None, *, within_days: int = 90):
    """An existing real job with the same company, title and place (the same posting reached through another address, e.g. the company
    site and a board). Needs a real company name: without one, two posts with the same title are not known to be the same job."""
    from datetime import datetime, timedelta, timezone

    from sqlalchemy import func, select

    from .models import Job
    c, t, loc = norm_text(company), norm_text(title), norm_text(location)
    if c in _GENERIC_COMPANIES or not t:
        return None
    since = datetime.now(timezone.utc) - timedelta(days=within_days)
    q = select(Job).where(Job.is_real_job.is_(True), func.lower(func.trim(Job.company_or_poster)) == c,
                          func.lower(func.trim(Job.title)) == t, Job.first_seen_at >= since)
    q = q.where(func.lower(func.trim(func.coalesce(Job.location, ""))) == loc)
    return db.scalar(q.order_by(Job.id).limit(1))
