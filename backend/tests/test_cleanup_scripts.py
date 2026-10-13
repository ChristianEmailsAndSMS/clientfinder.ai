"""scripts/clean_data.py and scripts/check_links.py: dry run by default, --apply hides, nothing is deleted."""
import os
import subprocess
import sys
from pathlib import Path

import httpx
import pytest
from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "scripts"))


def make_db(tmp_path):
    url = f"sqlite:///{tmp_path / 'c.db'}"
    env = dict(os.environ, DATABASE_URL=url)
    env.pop("DEV_FIXTURES", None)
    from app.db import Base
    import app.models  # noqa: F401
    from app.models import Job
    eng = create_engine(url)
    Base.metadata.create_all(eng)
    rows = [("Copywriter", "K18 Hair", "Remote", "web", "https://a.example/1"), ("Copywriter", "K18 Hair", "Remote", "web", "https://b.example/2"),
            ("Copywriter", None, "Remote", "web", "https://c.example/3"), ("Copywriter", None, "Remote", "web", "https://d.example/4"),
            ("Email Marketing Manager", "Acme", None, "greenhouse", "https://e.example/5"), ("Operations Engineer", "Acme", None, "greenhouse", "https://e.example/6"),
            ("Operations Engineer", "Reddit poster", None, "reddit", "https://e.example/7")]
    with Session(eng) as db:
        for n, (t, c, loc, sk, u) in enumerate(rows):
            db.add(Job(title=t, company_or_poster=c, location=loc, platform=sk, source_key=sk, source_url=u, dedupe_hash=f"h{n}", is_real_job=True))
        db.commit()
    return eng, env


def run(script, env, *args):
    return subprocess.run([sys.executable, str(ROOT / "scripts" / script), *args], capture_output=True, text=True, env=env, cwd=ROOT, timeout=60)


def visible(eng):
    from app.models import Job
    with Session(eng) as db:
        return sorted(db.scalars(select(Job.id).where(Job.is_real_job.is_(True))).all())


def test_clean_data_dry_run_changes_nothing_and_apply_hides_only_the_right_jobs(tmp_path):
    eng, env = make_db(tmp_path)
    r = run("clean_data.py", env)
    assert r.returncode == 0 and "1 duplicate listings in 1 groups; 1 off-topic" in r.stdout and "Dry run" in r.stdout
    assert visible(eng) == [1, 2, 3, 4, 5, 6, 7]
    assert run("clean_data.py", env, "--apply").returncode == 0
    # hidden: the second K18 Hair copy (2) and the off-topic job-board job (6). Kept: companyless "Copywriter" twins (3, 4) and the Reddit post (7).
    assert visible(eng) == [1, 3, 4, 5, 7]


def test_check_links_decides_by_status_and_by_where_a_redirect_lands(monkeypatch):
    import check_links

    def handler(request: httpx.Request) -> httpx.Response:
        u = str(request.url)
        if "gone" in u:
            return httpx.Response(404)
        if "blocked" in u:
            return httpx.Response(403)
        if "expired" in u:
            return httpx.Response(302, headers={"location": "https://www.indeed.com/q-copywriter-jobs.html"})
        if "q-copywriter-jobs" in u:
            return httpx.Response(200, text="search results")
        return httpx.Response(200, text="a job")

    client = httpx.Client(transport=httpx.MockTransport(handler))
    assert check_links.verdict(client, "https://x.example/job/1") == "ok"
    assert check_links.verdict(client, "https://x.example/gone") == "gone"
    assert check_links.verdict(client, "https://x.example/blocked") == "unknown"         # a robot block is not a dead link: leave it
    assert check_links.verdict(client, "https://x.example/expired") == "listing"          # an expired posting that bounces to a search page
