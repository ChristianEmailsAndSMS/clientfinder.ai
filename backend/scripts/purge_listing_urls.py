#!/usr/bin/env python3
"""Hide stored jobs whose "Open post" address is a search, list or company page instead of one job (is_real_job = False, nothing deleted).
Dry run by default: it prints the count and examples. Add --apply to change the rows.
    cd backend && .venv/bin/python scripts/purge_listing_urls.py          # look
    cd backend && .venv/bin/python scripts/purge_listing_urls.py --apply   # do it"""
import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from sqlalchemy import select  # noqa: E402

from app.db import session_scope  # noqa: E402
from app.models import Job  # noqa: E402
from app.scrapers.pipeline import is_listing_url  # noqa: E402


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--apply", action="store_true")
    args = ap.parse_args()
    with session_scope() as db:
        bad = [j for j in db.scalars(select(Job).where(Job.is_real_job.is_(True))).all() if is_listing_url(j.source_url)]
        by_site: dict[str, int] = {}
        for j in bad:
            by_site[j.platform] = by_site.get(j.platform, 0) + 1
        print(f"{len(bad)} jobs open a search or list page instead of one job:", by_site or "none")
        for j in bad[:15]:
            print("  ", j.id, j.platform, j.source_url[:110])
        if args.apply:
            for j in bad:
                j.is_real_job = False
            print(f"Hidden {len(bad)} jobs.")
        elif bad:
            print("Dry run. Add --apply to hide them.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
