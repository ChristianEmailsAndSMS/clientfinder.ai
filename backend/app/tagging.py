"""Derived tags for jobs: a small fixed vocabulary so tags work as stable UI filters.

Category tags come from the TITLE (precise). Tool tags come from title + skills + the start of the
description. Attribute tags come from structured fields. Tags are derived data: `retag()` can rebuild
them at any time (scripts/backfill_tags.py), so changing the rules is safe."""
from __future__ import annotations

from sqlalchemy import delete, func, select
from sqlalchemy.orm import Session

from .models import Job, JobTag

CATEGORY_RULES: dict[str, tuple[str, ...]] = {
    "copywriting": ("copywriter", "copywriting", "copy writer", "copy chief", "copy editor"),
    "email-marketing": ("email marketing", "email marketer", "email copy", "email specialist", "email manager",
                        "email strategist", "email designer", "email developer"),
    "lifecycle-crm": ("lifecycle", "crm", "retention marketing", "retention manager"),
    "funnels-landing-pages": ("funnel", "landing page", "conversion rate", "cro "),
    "creative-strategy": ("creative strategist", "creative director", "creative lead"),
    "growth-marketing": ("growth marketing", "growth marketer", "growth manager"),
    "direct-response": ("direct response", "direct-response"),
    "sms-marketing": ("sms marketing", "sms marketer", "text message marketing"),
}
TOOL_RULES: dict[str, tuple[str, ...]] = {
    "klaviyo": ("klaviyo",),
    "shopify": ("shopify",),
    "activecampaign": ("activecampaign", "active campaign"),
    "mailchimp": ("mailchimp",),
    "hubspot": ("hubspot",),
    "gohighlevel": ("gohighlevel", "go high level", "highlevel"),
}
ALL_TAGS: frozenset[str] = frozenset(
    list(CATEGORY_RULES) + list(TOOL_RULES) + ["remote", "contract", "full-time", "has-pay"]
)


def derive_tags(*, title: str | None, skills: list[str] | None = None, description: str | None = None,
                type: str | None = None, remote: bool | None = None,
                pay_min: float | None = None, pay_max: float | None = None, pay_text: str | None = None) -> set[str]:
    t = f" {(title or '').lower()} "
    tools_text = " ".join([t, " ".join(skills or []).lower(), (description or "")[:2000].lower()])
    tags: set[str] = set()
    for tag, kws in CATEGORY_RULES.items():
        if any(k in t for k in kws):
            tags.add(tag)
    for tag, kws in TOOL_RULES.items():
        if any(k in tools_text for k in kws):
            tags.add(tag)
    if remote:
        tags.add("remote")
    if type == "contract":
        tags.add("contract")
    elif type == "full_time":
        tags.add("full-time")
    if pay_min or pay_max or pay_text:
        tags.add("has-pay")
    return tags


def tags_for_job(job: Job) -> set[str]:
    return derive_tags(title=job.title, skills=job.skills, description=job.description, type=job.type,
                       remote=job.remote, pay_min=job.pay_min, pay_max=job.pay_max, pay_text=job.pay_text)


def set_tags(db: Session, job: Job) -> None:
    """Replace a job's derived tags. job.id must exist (flush first)."""
    db.execute(delete(JobTag).where(JobTag.job_id == job.id))
    for tag in sorted(tags_for_job(job)):
        db.add(JobTag(job_id=job.id, tag=tag))


def retag(db: Session, batch: int = 500) -> int:
    """Rebuild tags for every job. Returns the number of jobs processed."""
    n, last_id = 0, 0
    while True:
        jobs = db.scalars(select(Job).where(Job.id > last_id).order_by(Job.id).limit(batch)).all()
        if not jobs:
            return n
        for j in jobs:
            set_tags(db, j)
        db.flush()
        n += len(jobs)
        last_id = jobs[-1].id


def tag_counts(db: Session) -> list[tuple[str, int]]:
    rows = db.execute(
        select(JobTag.tag, func.count(JobTag.job_id)).join(Job, Job.id == JobTag.job_id)
        .where(Job.is_real_job.is_(True)).group_by(JobTag.tag).order_by(func.count(JobTag.job_id).desc(), JobTag.tag)
    ).all()
    return [(t, c) for t, c in rows]
