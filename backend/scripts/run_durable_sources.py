#!/usr/bin/env python3
"""CLI: pull from the free durable job sources (RemoteOK, ProBlogger, Greenhouse).
Each runs independently and reports its own stats."""
from __future__ import annotations

import json
import logging
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.scrapers import remoteok, problogger, greenhouse  # noqa: E402

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")


def main() -> int:
    all_stats = []
    for mod in (remoteok, problogger, greenhouse):
        try:
            all_stats.append(mod.run())
        except Exception as e:
            all_stats.append({"source": mod.__name__, "error": str(e)})
    print(json.dumps(all_stats, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
