"""Lever public postings API: https://api.lever.co/v0/postings/{company}?mode=json (free, no key).

LEVER_COMPANIES is a seed list. UNVERIFIED slugs: run `python scripts/check_sources.py lever` on the server
and prune the ones that 404. Add more slugs as you find companies hiring at jobs.lever.co/<slug>."""
from __future__ import annotations

import logging

from .common import CollectResult, NormalizedJob, employment_type, get, ingest, parse_dt, title_matches

log = logging.getLogger(__name__)

LEVER_COMPANIES: tuple[str, ...] = ("spotify", "plaid", "palantir", "kraken", "lever")
SOURCE_KEY = "lever"
_PERIOD = {"per-year-salary": "year", "per-hour-wage": "hour", "per-month-salary": "year"}


def parse(company: str, postings: list[dict]) -> CollectResult:
    out = CollectResult(fetched=len(postings))
    for p in postings:
        title = p.get("text") or ""
        url = p.get("hostedUrl")
        if not url or not title_matches(title):
            continue
        cats = p.get("categories") or {}
        sal = p.get("salaryRange") or {}
        location = cats.get("location")
        remote = (p.get("workplaceType") == "remote") or ("remote" in (location or "").lower()) or None
        out.jobs.append(NormalizedJob(
            url=url, title=title, company=company,
            description=(p.get("descriptionPlain") or "")[:4000] or None,
            type=employment_type(cats.get("commitment")),
            pay_min=sal.get("min"), pay_max=sal.get("max"),
            pay_period=_PERIOD.get(sal.get("interval")),
            pay_text=(f"{sal.get('currency', '')} {sal['min']:,}-{sal['max']:,}".strip()
                      if sal.get("min") and sal.get("max") else None),
            location=location, remote=remote,
            posted_at=parse_dt(p.get("createdAt")),
        ))
    return out


def collect(companies: tuple[str, ...] = LEVER_COMPANIES) -> CollectResult:
    total = CollectResult()
    for c in companies:
        try:
            data = get(f"https://api.lever.co/v0/postings/{c}?mode=json").json()
            part = parse(c, data if isinstance(data, list) else [])
        except Exception as e:
            total.errors.append(f"lever/{c}: {e}")
            continue
        total.fetched += part.fetched
        total.jobs.extend(part.jobs)
    return total


def run() -> dict:
    return ingest(SOURCE_KEY, "Lever (public boards)", "lever", collect())
