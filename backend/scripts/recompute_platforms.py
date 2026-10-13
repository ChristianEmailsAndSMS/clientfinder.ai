#!/usr/bin/env python3
"""Re-derive every Google-found job's platform from its link, after the platform rules were fixed (e.g. wix.com used to count as
Twitter/X because 'x.com' appeared inside the address). Jobs from direct sources (RemoteOK, Greenhouse...) are left alone.

    .venv/bin/python scripts/recompute_platforms.py            show what would change
    .venv/bin/python scripts/recompute_platforms.py --apply    change it
    --allow-web   also let old rows (no stored found_url) become "web"; read the list first"""
import argparse
import sys
from collections import Counter
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from sqlalchemy import select  # noqa: E402

from app.db import session_scope  # noqa: E402
from app.models import Job  # noqa: E402
from app.scrapers.google_search import _platform_from_url  # noqa: E402

_ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
_ap.add_argument("--apply", action="store_true", help="change the data (default: only show what would change)")
_ap.add_argument("--allow-web", action="store_true", help="also let old rows (no stored found_url) become 'web'")
_args = _ap.parse_args()                      # --help and unknown options exit here, before the database is touched
apply = _args.apply
downgrade = _args.allow_web                   # see below
changes: Counter = Counter()
skipped: Counter = Counter()
with session_scope() as db:
    for job in db.scalars(select(Job).where(Job.source_key.like("google:%") | (Job.source_key == "usersearch"))).all():
        found = (job.extra or {}).get("found_url")        # the page the job was found on (stored since the swarm-2 fix)
        new = _platform_from_url(found or job.source_url)
        if new != job.platform:
            # Older rows only have source_url, which is the APPLY link when the model gave one (a Reddit or LinkedIn post that says
            # "apply at forms.gle/...") - not the page we found it on. Never turn such a row into "web" unless asked to.
            if not found and new == "web" and not downgrade:
                skipped[(job.platform, new)] += 1
                continue
            changes[(job.platform, new)] += 1
            if apply:
                job.platform = new
for (old, new), n in changes.most_common():
    print(f"{n:5}  {old} -> {new}")
for (old, new), n in skipped.most_common():
    print(f"{n:5}  {old} -> {new}  NOT changed (old row, link may be an apply link); add --allow-web to change these too")
print(("Changed " if apply else "Would change ") + f"{sum(changes.values())} jobs." + ("" if apply else "  Add --apply to do it."))
