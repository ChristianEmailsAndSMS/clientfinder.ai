#!/usr/bin/env python3
"""Open every stored job link (politely, one at a time) and hide the ones that cannot open a job: pages that are gone (404/410), placeholder or
truncated addresses, and search or list pages. Nothing is deleted: a hidden job has is_real_job = False and can be restored.
Dry run by default. Run it on the server (it needs internet).
    cd backend && .venv/bin/python scripts/check_links.py                  # look: prints what it would hide
    cd backend && .venv/bin/python scripts/check_links.py --apply          # hide them
    cd backend && .venv/bin/python scripts/check_links.py --limit 100 --delay 2   # a small, slow sample
Links that time out or return 403/429/5xx are NOT hidden (the site may just be blocking a robot); they are only counted."""
import argparse
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import httpx  # noqa: E402
from sqlalchemy import select  # noqa: E402

from app.db import session_scope  # noqa: E402
from app.models import Job  # noqa: E402
from app.scrapers.common import UA  # noqa: E402
from app.scrapers.pipeline import is_listing_url  # noqa: E402
from app.security_utils import safe_job_url  # noqa: E402

GONE = {404, 410}


def verdict(client: httpx.Client, url: str) -> str:
    """'ok', 'gone', 'listing' (opens a search/list page after redirects) or 'unknown' (blocked, slow or erroring: leave it alone)."""
    try:
        r = client.get(url, follow_redirects=True)
    except httpx.HTTPError:
        return "unknown"
    if r.status_code in GONE:
        return "gone"
    if r.status_code >= 400:
        return "unknown"
    final = str(r.url)
    if final != url and is_listing_url(final) and not is_listing_url(url):
        return "listing"                        # an expired posting that redirects to a search page
    return "ok"


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--apply", action="store_true", help="hide the dead links (default: only report)")
    ap.add_argument("--limit", type=int, default=0, help="check at most this many jobs (0 = all)")
    ap.add_argument("--delay", type=float, default=1.0, help="seconds between requests (default 1)")
    args = ap.parse_args()
    counts = {"ok": 0, "gone": 0, "listing": 0, "template": 0, "unknown": 0}
    bad: list[tuple[int, str, str]] = []
    with session_scope() as db, httpx.Client(headers={"User-Agent": UA}, timeout=15, verify=True) as client:
        jobs = db.scalars(select(Job).where(Job.is_real_job.is_(True)).order_by(Job.id)).all()
        if args.limit:
            jobs = jobs[: args.limit]
        for j in jobs:
            url = j.source_url
            if safe_job_url(url) is None or is_listing_url(url):
                v = "template" if safe_job_url(url) is None else "listing"
            else:
                v = verdict(client, url)
                time.sleep(args.delay)
            counts[v] += 1
            if v != "ok" and v != "unknown":
                bad.append((j.id, v, url))
                if args.apply:
                    j.is_real_job = False
        print(f"checked {len(jobs)} jobs:", counts)
        for i, v, u in bad[:40]:
            print(f"  {i:6} {v:8} {u[:110]}")
        print(f"{'Hidden' if args.apply else 'Would hide'} {len(bad)} jobs." + ("" if args.apply or not bad else " Add --apply to do it."))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
