import html
from datetime import datetime, timedelta, timezone
from fastapi import APIRouter, Depends, Query
from fastapi.responses import HTMLResponse
from sqlalchemy import and_, select, func, or_
from sqlalchemy.orm import Session

from ..db import get_db
from ..models import Job
from ..schemas import JobOut

router = APIRouter(prefix="/jobs", tags=["jobs"])


@router.get("", response_model=list[JobOut])
def list_jobs(
    db: Session = Depends(get_db),
    q: str | None = Query(None, description="keyword search on title/company/snippet"),
    platform: str | None = None,
    type: str | None = None,
    min_pay: float | None = None,
    max_pay: float | None = None,
    posted_within_days: int | None = Query(None, ge=1, le=365),
    limit: int = Query(50, ge=1, le=200),
    offset: int = Query(0, ge=0),
):
    stmt = select(Job).order_by(Job.first_seen_at.desc())
    if q:
        like = f"%{q}%"
        stmt = stmt.where(or_(Job.title.ilike(like), Job.company_or_poster.ilike(like), Job.raw_snippet.ilike(like)))
    if platform:
        stmt = stmt.where(Job.platform == platform)
    if type:
        stmt = stmt.where(Job.type == type)
    if min_pay is not None:
        # A job that only stored pay_min (hourly "$100/hr") still qualifies.
        stmt = stmt.where(func.coalesce(Job.pay_max, Job.pay_min) >= min_pay)
    if max_pay is not None:
        stmt = stmt.where(func.coalesce(Job.pay_min, Job.pay_max) <= max_pay)
    if posted_within_days:
        cutoff = datetime.now(timezone.utc) - timedelta(days=posted_within_days)
        stmt = stmt.where(or_(
            Job.posted_at >= cutoff,
            and_(Job.posted_at.is_(None), Job.first_seen_at >= cutoff),
        ))
    stmt = stmt.limit(limit).offset(offset)
    rows = db.scalars(stmt).all()
    return rows


@router.get("/stats")
def job_stats(db: Session = Depends(get_db)) -> dict:
    total = db.scalar(select(func.count(Job.id))) or 0
    by_platform = {
        k: v for k, v in db.execute(select(Job.platform, func.count(Job.id)).group_by(Job.platform)).all()
        if k is not None
    }
    by_type = {
        k: v for k, v in db.execute(select(Job.type, func.count(Job.id)).group_by(Job.type)).all()
        if k is not None
    }
    return {"total": total, "by_platform": by_platform, "by_type": by_type}


_FEED_PAGE = """<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Jobs — Clientfinder.ai</title>
<style>
:root { --bg:#0b0d12; --fg:#e9edf3; --muted:#8891a0; --accent:#4ea1ff; --card:#141821; --border:#1f2430; }
* { box-sizing:border-box; margin:0; padding:0; }
body { background:var(--bg); color:var(--fg); font-family:-apple-system,BlinkMacSystemFont,"Segoe UI",Roboto,Helvetica,Arial,sans-serif; }
a { color:var(--accent); text-decoration:none; }
a:hover { text-decoration:underline; }
.wrap { max-width:1040px; margin:0 auto; padding:48px 24px; }
header { display:flex; justify-content:space-between; align-items:center; margin-bottom:32px; }
.brand { font-weight:700; font-size:20px; }
.brand .dot { color:var(--accent); }
nav a { margin-left:24px; color:var(--muted); font-size:14px; }
h1 { font-size:32px; letter-spacing:-0.02em; margin-bottom:8px; }
.lede { color:var(--muted); margin-bottom:28px; }
ul { list-style:none; display:flex; flex-direction:column; gap:12px; }
li { background:var(--card); border:1px solid var(--border); border-radius:12px; padding:16px 18px; }
.meta { color:var(--muted); font-size:13px; margin-top:6px; }
</style>
</head>
<body>
<div class="wrap">
<header>
  <div class="brand"><a href="/">clientfinder<span class="dot">.ai</span></a></div>
  <nav>
    <a href="/jobs/feed">jobs</a>
    <a href="/docs">api</a>
    <a href="/jobs/stats">stats</a>
  </nav>
</header>
<h1>Today's jobs</h1>
<p class="lede">Latest listings in the database. Open a row to see the original post.</p>
<ul>
{{JOBS}}
</ul>
</div>
</body>
</html>"""


@router.get("/feed", response_class=HTMLResponse, include_in_schema=False)
def jobs_feed(db: Session = Depends(get_db)):
    rows = db.scalars(select(Job).order_by(Job.first_seen_at.desc()).limit(50)).all()
    if not rows:
        items = "<li>No jobs yet. Run the pipeline and this list fills in.</li>"
    else:
        parts = []
        for job in rows:
            title = html.escape(job.title or "Untitled")
            url = html.escape(job.source_url or "", quote=True)
            platform = html.escape(job.platform or "")
            company = html.escape(job.company_or_poster or "")
            pay = html.escape(job.pay_text or "")
            bits = " · ".join(bit for bit in (platform, company, pay) if bit)
            parts.append(
                f'<li><a href="{url}">{title}</a><div class="meta">{bits}</div></li>'
            )
        items = "\n".join(parts)
    return _FEED_PAGE.replace("{{JOBS}}", items)
