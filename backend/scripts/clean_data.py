#!/usr/bin/env python3
"""Tidy stored jobs. Dry run by default; add --apply to change rows. Nothing is deleted (hidden jobs have is_real_job = False).
    cd backend && .venv/bin/python scripts/clean_data.py            # look
    cd backend && .venv/bin/python scripts/clean_data.py --apply    # do it
It does two things:
  1. DUPLICATES: jobs with the same company, title and place (see app/dedup.py find_same_job); the oldest stays, the others are hidden.
     Jobs with no company name are never merged: two "Copywriter" posts with no company are not known to be the same job.
  2. OFF-TOPIC: jobs from the direct job-board feeds whose TITLE does not name any of our roles (e.g. "Operations Engineer"); the same title
     filter new jobs already pass through (app/scrapers/common.py title_matches). Posts found through search are not touched."""
import argparse
import sys
from collections import defaultdict
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from sqlalchemy import select  # noqa: E402

from app.db import session_scope  # noqa: E402
from app.dedup import norm_text  # noqa: E402
from app.models import Job  # noqa: E402
from app.scrapers.common import title_matches  # noqa: E402

DIRECT_FEEDS = ("remoteok", "weworkremotely", "remotive", "greenhouse", "lever", "ashby", "problogger")
GENERIC = {"", "confidential", "anonymous", "unknown", "n/a", "na", "company", "hiring", "stealth", "undisclosed"}


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--apply", action="store_true")
    args = ap.parse_args()
    with session_scope() as db:
        jobs = db.scalars(select(Job).where(Job.is_real_job.is_(True)).order_by(Job.id)).all()
        groups: dict[tuple, list[Job]] = defaultdict(list)
        for j in jobs:
            c = norm_text(j.company_or_poster)
            if c not in GENERIC and norm_text(j.title):
                groups[(c, norm_text(j.title), norm_text(j.location))].append(j)
        dupes = [j for g in groups.values() if len(g) > 1 for j in g[1:]]
        off = [j for j in jobs if j.source_key in DIRECT_FEEDS and not title_matches(j.title)]
        print(f"{len(dupes)} duplicate listings in {sum(1 for g in groups.values() if len(g) > 1)} groups; {len(off)} off-topic jobs from job-board feeds")
        for j in dupes[:12]:
            print("  dup     ", j.id, j.company_or_poster, "|", j.title[:70])
        for j in off[:12]:
            print("  off-topic", j.id, j.platform, "|", j.title[:70])
        if args.apply:
            for j in {*dupes, *off}:
                j.is_real_job = False
            print(f"Hidden {len({*dupes, *off})} jobs.")
        elif dupes or off:
            print("Dry run. Add --apply to hide them.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
