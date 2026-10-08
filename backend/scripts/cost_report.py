#!/usr/bin/env python3
"""What the scraping is costing, and what each query earns.

    python scripts/cost_report.py [--plan-usd 75 --plan-searches 5000]

Search cost = plan price / plan searches, per successful search. Extraction cost comes from the token usage
stored on each job (jobs.extra.usage) and app/pricing.py (unverified prices: check them)."""
from __future__ import annotations

import argparse
import sys
from collections import defaultdict
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from sqlalchemy import select  # noqa: E402

from app.db import session_scope  # noqa: E402
from app.models import Job, SearchQuery  # noqa: E402
from app.pricing import cost_usd  # noqa: E402
from app.scrapers.query_plan import month_start  # noqa: E402


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--plan-usd", type=float, default=75.0)
    ap.add_argument("--plan-searches", type=int, default=5000)
    args = ap.parse_args()
    per_search = args.plan_usd / args.plan_searches
    since = month_start(datetime.now(timezone.utc))

    with session_scope() as db:
        searches = db.execute(
            select(SearchQuery.query_key, SearchQuery.new_jobs, SearchQuery.error).where(SearchQuery.ran_at >= since)
        ).all()
        jobs = db.execute(select(Job.extra, Job.source_key, Job.extraction_model).where(Job.first_seen_at >= since)).all()
        total_jobs = len(jobs)

    ok = [q for q in searches if q.error is None]  # rows: (query_key, new_jobs, error)
    search_usd = len(ok) * per_search
    tin = tout = 0
    ext_usd, unpriced = 0.0, 0
    for extra, _key, _model in jobs:
        u = (extra or {}).get("usage")
        if not u:
            continue
        tin += u.get("input_tokens", 0)
        tout += u.get("output_tokens", 0)
        c = cost_usd(u.get("model"), u.get("input_tokens", 0), u.get("output_tokens", 0))
        if c is None:
            unpriced += 1
        else:
            ext_usd += c
    llm_jobs = sum(1 for e, *_ in jobs if (e or {}).get("usage"))

    print(f"== since {since:%Y-%m-%d}")
    print(f"searches: {len(ok)} ok, {len(searches) - len(ok)} failed  -> ${search_usd:.2f} at ${per_search:.4f}/search")
    print(f"new jobs: {total_jobs} total, {llm_jobs} via Claude extraction")
    print(f"extraction: {tin:,} in / {tout:,} out tokens -> ${ext_usd:.2f}" + (f"  ({unpriced} jobs on unpriced models)" if unpriced else ""))
    note = " (Claude-extracted rows only; stored rows that cost nothing are not counted)"
    print(f"approx extraction cost per Claude-extracted job: ${ext_usd / llm_jobs:.4f}{note}" if llm_jobs else "no Claude-extracted jobs yet")
    spend = search_usd + ext_usd
    print(f"total scraping spend: ${spend:.2f}  ->  ${spend / total_jobs:.3f} per new job" if total_jobs else "no jobs yet")

    by_q: dict[str, list[int]] = defaultdict(lambda: [0, 0])
    for key, new_jobs, _err in ok:
        by_q[key][0] += 1
        by_q[key][1] += new_jobs
    if by_q:
        ranked = sorted(by_q.items(), key=lambda kv: kv[1][1] / kv[1][0], reverse=True)
        print("\nbest queries (new jobs per search):")
        for k, (n, new) in ranked[:8]:
            print(f"  {new / n:5.2f}  {n:3d} runs  {k}")
        worst = [r for r in ranked[-8:] if r not in ranked[:8]]
        print("\nworst queries (candidates to drop):" if worst else "\n(too few queries for a worst list yet)")
        for k, (n, new) in worst:
            print(f"  {new / n:5.2f}  {n:3d} runs  {k}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
