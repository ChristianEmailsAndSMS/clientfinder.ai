#!/usr/bin/env python3
"""Live smoke test for the direct sources. Fetches and parses; writes NOTHING to the database.

    python scripts/check_sources.py            # all new sources
    python scripts/check_sources.py lever      # one source

Per source: items fetched, items matching our keywords, per-feed errors, and sample titles.
Run it on the server (open internet) before trusting a source or pruning its slug list."""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.scrapers import ashby, lever, remotive, weworkremotely  # noqa: E402

SOURCES = {"remotive": remotive, "weworkremotely": weworkremotely, "lever": lever, "ashby": ashby}


def main(names: list[str]) -> int:
    bad = 0
    for name in names or list(SOURCES):
        if name not in SOURCES:
            print(f"unknown source {name!r}; choose from {', '.join(SOURCES)}")
            return 2
        r = SOURCES[name].collect()
        print(f"\n== {name}: fetched={r.fetched} matched={len(r.jobs)} errors={len(r.errors)}")
        for e in r.errors:
            print(f"   ERROR {e}")
        for j in r.jobs[:5]:
            print(f"   - {j.title[:70]} | {j.company} | {j.type} | {j.pay_text} | {j.url[:70]}")
        if r.fetched == 0:
            bad += 1
            print("   !! nothing fetched: endpoint or parser needs attention")
    return 1 if bad else 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
