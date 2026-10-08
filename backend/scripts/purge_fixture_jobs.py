#!/usr/bin/env python3
"""Delete fake rows written by DEV_FIXTURES mode (extraction_model = 'heuristic-mock').
Dry run by default; pass --apply to delete. Also removes google_search sources left with no jobs."""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from sqlalchemy import delete, func, select  # noqa: E402

from app.db import session_scope  # noqa: E402
from app.models import Job, Source  # noqa: E402

MOCK = "heuristic-mock"


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--apply", action="store_true", help="actually delete (default: dry run)")
    args = ap.parse_args()

    with session_scope() as db:
        rows = db.execute(select(Job.platform, func.count()).where(Job.extraction_model == MOCK).group_by(Job.platform)).all()
        total = sum(n for _, n in rows)
        print(f"fixture rows: {total}  {dict(rows)}")
        print(f"real rows kept: {db.scalar(select(func.count(Job.id)).where(Job.extraction_model.is_distinct_from(MOCK)))}")
        if not args.apply:
            print("dry run; re-run with --apply to delete")
            return 0
        db.execute(delete(Job).where(Job.extraction_model == MOCK))
        empty = db.scalars(
            select(Source).where(Source.kind == "google_search", ~Source.key.in_(select(Job.source_key).distinct()))
        ).all()
        for s in empty:
            db.delete(s)
        print(f"deleted {total} jobs and {len(empty)} empty google sources")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
