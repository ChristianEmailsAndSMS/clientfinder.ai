#!/usr/bin/env python3
"""Health and yield of every source over the last N days (default 7). Read-only.

    python scripts/source_report.py [--days 7]

Use it to judge a trial week: is each source running, succeeding, and adding jobs?"""
from __future__ import annotations

import argparse
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from sqlalchemy import func, select  # noqa: E402

from app.db import session_scope  # noqa: E402
from app.models import Job, ScrapeRun, Source  # noqa: E402


def _aware(dt):
    return dt if dt is None or dt.tzinfo else dt.replace(tzinfo=timezone.utc)


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--days", type=int, default=7)
    args = ap.parse_args()
    now = datetime.now(timezone.utc)
    since = now - timedelta(days=args.days)

    with session_scope() as db:
        sources = db.execute(select(Source.id, Source.key).order_by(Source.key)).all()
        runs = db.execute(select(ScrapeRun.source_id, ScrapeRun.started_at, ScrapeRun.status, ScrapeRun.jobs_added)).all()
        totals = dict(db.execute(select(Job.source_key, func.count(Job.id)).group_by(Job.source_key)).all())
        fresh = dict(db.execute(select(Job.source_key, func.count(Job.id)).where(Job.first_seen_at >= since).group_by(Job.source_key)).all())
        mock = db.scalar(select(func.count(Job.id)).where(Job.extraction_model == "heuristic-mock")) or 0

    by_src: dict[int, list] = {}
    for sid, started, status, added in runs:
        by_src.setdefault(sid, []).append((_aware(started), status, added or 0))

    print(f"== last {args.days} days (now {now:%Y-%m-%d %H:%MZ})")
    print(f"{'source':34} {'runs':>4} {'ok':>3} {'fail':>4} {'new jobs':>8} {'total':>6}  last run / status")
    flags = []
    for sid, key in sources:
        rs = [r for r in by_src.get(sid, []) if r[0] >= since]
        ok = sum(1 for r in rs if r[1] == "ok")
        failed = sum(1 for r in rs if r[1] == "failed")
        last = max(by_src.get(sid, []), default=None)
        last_txt = f"{last[0]:%m-%d %H:%M} {last[1]}" if last else "never"
        print(f"{key[:34]:34} {len(rs):4d} {ok:3d} {failed:4d} {fresh.get(key, 0):8d} {totals.get(key, 0):6d}  {last_txt}")
        if not key.startswith("google:"):
            if not last or now - last[0] > timedelta(hours=30):
                flags.append(f"STALE: {key} has no run in the last 30h")
            elif rs and ok == 0:
                flags.append(f"BROKEN: {key} never succeeded in {args.days}d")
            elif rs and sum(r[2] for r in rs) == 0 and totals.get(key, 0) == 0:
                flags.append(f"EMPTY: {key} runs fine but has produced no jobs ever (check keywords/slugs)")
    if mock:
        flags.append(f"FAKE DATA: {mock} fixture rows still in the DB (run scripts/purge_fixture_jobs.py --apply)")
    print()
    print("\n".join(flags) if flags else "no problems flagged")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
