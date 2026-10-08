"""Long-running scheduler process: `python -m app.scheduler`.

- Durable sources (RemoteOK, ProBlogger, Greenhouse, Remotive, WWR, Lever, Ashby): every DURABLE_SOURCES_INTERVAL_HOURS (default 6 = 4x/day),
  first run shortly after start.
- Google-search layer: every GOOGLE_SEARCH_INTERVAL_MINUTES (default 60). Only scheduled when a search key AND an
  Anthropic key are configured and DEV_FIXTURES is off, so fake fixture data can never be scheduled.

Run it as its own systemd unit (deploy/clientfinder-scheduler.service), separate from the API."""
from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone

from apscheduler.schedulers.blocking import BlockingScheduler

from .config import settings
from .scrapers import ashby, greenhouse, lever, problogger, remoteok, remotive, weworkremotely
from .scrapers.pipeline import run_pipeline_for_queries

log = logging.getLogger("scheduler")

DURABLE = (
    ("remoteok", remoteok), ("problogger", problogger), ("greenhouse", greenhouse),
    ("remotive", remotive),  # keep <= 4 runs/day (Remotive's request): durable interval of 6h or more
    ("weworkremotely", weworkremotely), ("lever", lever), ("ashby", ashby),
)


def run_durable_source(name: str, module) -> None:
    try:
        log.info("durable %s: %s", name, module.run())
    except Exception:
        log.exception("durable %s failed", name)


def run_google_layer() -> None:
    try:
        for s in run_pipeline_for_queries():
            log.info("google: %s", s)
    except Exception:
        log.exception("google layer failed")


def google_layer_ready() -> tuple[bool, str]:
    if settings.dev_fixtures:
        return False, "DEV_FIXTURES is on (fake data)"
    if not (settings.serpapi_api_key or settings.serper_api_key):
        return False, "no SERPAPI_API_KEY / SERPER_API_KEY"
    if not settings.anthropic_api_key:
        return False, "no ANTHROPIC_API_KEY"
    return True, ""


def build_scheduler() -> BlockingScheduler:
    sched = BlockingScheduler(
        timezone="UTC",
        job_defaults={"coalesce": True, "max_instances": 1, "misfire_grace_time": 600},
    )
    now = datetime.now(timezone.utc)
    for i, (name, module) in enumerate(DURABLE):
        sched.add_job(
            run_durable_source, "interval", args=[name, module], id=f"durable:{name}",
            hours=max(settings.durable_sources_interval_hours, 6) if name == "remotive"
            else settings.durable_sources_interval_hours,
            next_run_time=now + timedelta(seconds=15 + 30 * i),  # staggered first run
        )
    ok, why = google_layer_ready()
    if ok:
        sched.add_job(
            run_google_layer, "interval", id="google", minutes=settings.google_search_interval_minutes,
            next_run_time=now + timedelta(minutes=2),
        )
    else:
        log.warning("google layer NOT scheduled: %s", why)
    return sched


def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    sched = build_scheduler()
    for job in sched.get_jobs():
        log.info("scheduled %s every %s", job.id, job.trigger)
    try:
        sched.start()
    except (KeyboardInterrupt, SystemExit):
        log.info("scheduler stopped")


if __name__ == "__main__":
    main()
