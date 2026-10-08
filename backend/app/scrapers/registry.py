"""The durable (free, no-LLM) sources, by their `sources.key`. Shared by the scheduler and the admin API."""
from __future__ import annotations

from . import ashby, greenhouse, lever, problogger, remoteok, remotive, weworkremotely

DURABLE = {
    "remoteok": remoteok,
    "problogger": problogger,
    "greenhouse": greenhouse,
    "remotive": remotive,   # Remotive asks for <= ~4 requests/day: the scheduler floors its interval at 6h
    "weworkremotely": weworkremotely,
    "lever": lever,
    "ashby": ashby,
}
