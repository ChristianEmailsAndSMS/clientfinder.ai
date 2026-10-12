#!/usr/bin/env python3
"""Re-derive every Google-found job's platform from its link, after the platform rules were fixed (e.g. wix.com used to count as
Twitter/X because 'x.com' appeared inside the address). Jobs from direct sources (RemoteOK, Greenhouse...) are left alone.

    .venv/bin/python scripts/recompute_platforms.py            show what would change
    .venv/bin/python scripts/recompute_platforms.py --apply    change it"""
import sys
from collections import Counter
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from sqlalchemy import select  # noqa: E402

from app.db import session_scope  # noqa: E402
from app.models import Job  # noqa: E402
from app.scrapers.google_search import _platform_from_url  # noqa: E402

apply = "--apply" in sys.argv
changes: Counter = Counter()
with session_scope() as db:
    for job in db.scalars(select(Job).where(Job.source_key.like("google:%") | (Job.source_key == "usersearch"))).all():
        new = _platform_from_url(job.source_url)
        if new != job.platform:
            changes[(job.platform, new)] += 1
            if apply:
                job.platform = new
for (old, new), n in changes.most_common():
    print(f"{n:5}  {old} -> {new}")
print(("Changed " if apply else "Would change ") + f"{sum(changes.values())} jobs." + ("" if apply else "  Add --apply to do it."))
