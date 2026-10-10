#!/usr/bin/env python3
"""CLI: run the Google-search → fetch → LLM-extract pipeline.
Usage:
    python scripts/run_pipeline.py                      # all default queries
    python scripts/run_pipeline.py --query "hiring copywriter"
    python scripts/run_pipeline.py --query "hiring copywriter" --num 10
"""
from __future__ import annotations

import argparse
import json
import logging
import sys
from pathlib import Path

# Add backend/ to path when run as a script
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.scrapers.pipeline import run_pipeline_for_query, run_pipeline_for_queries  # noqa: E402

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--query", help="single query to run", default=None)
    ap.add_argument("--num", type=int, default=20)
    args = ap.parse_args()

    if args.query:
        stats = [run_pipeline_for_query(args.query, num=args.num)]
    else:
        stats = run_pipeline_for_queries(num=args.num)
    print(json.dumps(stats[0] if args.query else stats, indent=2))
    failed = any(row.get("error") or row.get("errors") for row in stats)
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
