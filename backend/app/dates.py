"""One rule for 'is this a believable posting date?'. A hiring post cannot be older than the earliest job boards we read or dated in the future;
anything else (epoch 0, 1969, a mis-read 2-digit year, a model that guessed) is a parse error and must never reach a customer as '691 months ago'."""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

MIN_POSTED = datetime(2015, 1, 1, tzinfo=timezone.utc)
FUTURE_SLACK = timedelta(days=1)                      # clocks and time zones differ a little


def latest_posted(now: datetime | None = None) -> datetime:
    return (now or datetime.now(timezone.utc)) + FUTURE_SLACK


def sane_posted_at(dt: datetime | None, now: datetime | None = None) -> datetime | None:
    """The date if it is believable, else None (callers then fall back to when we first saw the post)."""
    if dt is None:
        return None
    aware = dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)
    return dt if MIN_POSTED <= aware <= latest_posted(now) else None
