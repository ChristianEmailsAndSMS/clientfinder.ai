#!/usr/bin/env python3
"""Rebuild derived tags for every job. Safe to re-run; run after changing app/tagging.py rules."""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.db import session_scope  # noqa: E402
from app.tagging import retag, tag_counts  # noqa: E402


def main() -> int:
    with session_scope() as db:
        n = retag(db)
        counts = tag_counts(db)
    print(f"retagged {n} jobs")
    for tag, c in counts:
        print(f"  {c:5d}  {tag}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
