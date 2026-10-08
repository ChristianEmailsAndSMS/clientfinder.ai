#!/usr/bin/env python3
"""CLI: run the Google-search → fetch → LLM-extract pipeline.
Usage:
    python scripts/run_pipeline.py                      # all default queries
    python scripts/run_pipeline.py --query "hiring copywriter"
    python scripts/run_pipeline.py --query "hiring copywriter" --num 10 --freshness w
    python scripts/run_pipeline.py --due               # one scheduler tick (logged, budget-aware)
"""
from __future__ import annotations

import argparse
import json
import logging
import sys
from pathlib import Path

# Add backend/ to path when run as a script
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.logging_setup import setup_logging  # noqa: E402
from app.scrapers.pipeline import run_pipeline_for_query, run_pipeline_for_queries  # noqa: E402

setup_logging()


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--query", help="single query to run", default=None)
    ap.add_argument("--num", type=int, default=10)
    ap.add_argument("--freshness", choices=["d", "w", "m"], default=None, help="d=24h, w=week, m=month")
    ap.add_argument("--due", action="store_true", help="run one scheduler tick (due queries, budget-aware, logged)")
    args = ap.parse_args()

    if args.due:
        from app.scrapers.query_plan import run_due_queries
        print(json.dumps(run_due_queries(), indent=2))
        return 0
    if args.query:
        stats = run_pipeline_for_query(args.query, num=args.num, freshness=args.freshness)
        print(json.dumps(stats, indent=2))
    else:
        stats = run_pipeline_for_queries(num=args.num)
        print(json.dumps(stats, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
