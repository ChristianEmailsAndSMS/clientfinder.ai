"""Tags, admin API, failed-URL requeue. Uses sqlite + FastAPI TestClient (background tasks run inline)."""
import json
import os
import time
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, select
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from app.api import admin
from app.config import settings
from app.db import Base, get_db
from app.main import app
from app.models import FailedUrl, Job, JobTag, ScrapeRun, Source
from app.schemas import ExtractedJob, SearchResult
from app.scrapers import common, google_search, llm_extractor, page_fetcher, pipeline, query_plan
from app.scrapers.llm_extractor import ExtractionError
from app.tagging import derive_tags, retag, set_tags, tag_counts

import authkit
from authkit import CSRF

ADMIN_EMAIL = "christian@emailsandsms.com"
H = CSRF


@pytest.fixture
def env(monkeypatch):
    e = authkit.make_env(monkeypatch)
    with e.scope() as db:
        _, secret = authkit.make_user(db, ADMIN_EMAIL, admin=True)
    assert authkit.login(e, e.client, ADMIN_EMAIL, secret=secret).status_code == 200
    admin._running.clear()
    e.client.admin_secret = secret
    yield e.client, e.scope
    app.dependency_overrides.clear()


def add_job(db, **kw):
    kw.setdefault("source_url", "u"); kw.setdefault("platform", "web"); kw.setdefault("source_key", "s")
    kw.setdefault("dedupe_hash", f"h{db.query(Job).count()}"); kw.setdefault("title", "t")
    j = Job(**kw); db.add(j); db.flush(); set_tags(db, j); return j


# ---------- tagging rules ----------
def test_derive_tags_from_title_tools_and_attributes():
    t = derive_tags(title="Senior Email Marketing Manager", skills=["Klaviyo"], description="Run flows on Shopify",
                    type="full_time", remote=True, pay_text="$90k")
    assert t == {"email-marketing", "klaviyo", "shopify", "full-time", "remote", "has-pay"}


def test_category_tags_use_title_only_not_description():
    assert derive_tags(title="Software Engineer", description="we need a copywriter and funnel builder") == set()
    assert derive_tags(title="Direct Response Copywriter") == {"copywriting", "direct-response"}
    assert "creative-strategy" in derive_tags(title="Creative Strategist, Paid Social")
    assert derive_tags(title="Lifecycle Marketing Lead") == {"lifecycle-crm"}


def test_every_emitted_tag_is_in_the_vocabulary():
    from app.tagging import ALL_TAGS, CATEGORY_RULES, TOOL_RULES
    assert set(CATEGORY_RULES) | set(TOOL_RULES) <= ALL_TAGS


def test_retag_rebuilds_and_counts(env):
    _, scope = env
    with scope() as db:
        add_job(db, title="Email Marketer", remote=True)
        add_job(db, title="Copywriter")
        db.execute(JobTag.__table__.delete())                          # simulate old rows with no tags
        db.flush()
        assert retag(db) == 2
        assert dict(tag_counts(db)) == {"email-marketing": 1, "copywriting": 1, "remote": 1}


# ---------- tags in the public API ----------
def test_jobs_tag_filter_tags_field_and_counts(env):
    client, scope = env
    with scope() as db:
        add_job(db, title="Email Marketing Manager", remote=True, type="contract")
        add_job(db, title="Email Copywriter", remote=False)
        add_job(db, title="Accountant")
    all_jobs = client.get("/jobs").json()
    assert {j["title"]: j["tags"] for j in all_jobs}["Email Copywriter"] == ["copywriting", "email-marketing"]
    assert {j["title"] for j in client.get("/jobs?tag=email-marketing").json()} == {"Email Marketing Manager", "Email Copywriter"}
    assert [j["title"] for j in client.get("/jobs?tag=email-marketing&tag=remote").json()] == ["Email Marketing Manager"]
    assert client.get("/jobs?tag=EMAIL-MARKETING").json() != []        # case-insensitive
    assert client.get("/jobs?tag=nope").json() == []
    counts = {c["tag"]: c["count"] for c in client.get("/jobs/tags").json()}
    assert counts["email-marketing"] == 2 and "nope" not in counts


# ---------- admin auth ----------
def test_admin_endpoints_need_a_signed_in_admin(env):
    client, scope = env
    from fastapi.testclient import TestClient
    anon = TestClient(app)
    for method, path in (("get", "/admin/sources"), ("get", "/admin/overview"), ("get", "/admin/users"), ("get", "/admin/backups"),
                         ("post", "/admin/retag"), ("post", "/admin/failed-urls/requeue"), ("post", "/admin/sources/lever/run")):
        r = getattr(anon, method)(path, headers=H)
        assert r.status_code == 401, (path, r.status_code)


# ---------- sources ----------
def test_sources_listing_shows_last_run_and_running_flag(env):
    client, scope = env
    now = datetime.now(timezone.utc)
    with scope() as db:
        src = Source(key="remoteok", kind="direct_api", display_name="r"); db.add(src); db.flush()
        db.add_all([ScrapeRun(source_id=src.id, started_at=now - timedelta(hours=30), status="ok", jobs_added=1),
                    ScrapeRun(source_id=src.id, started_at=now - timedelta(hours=1), status="failed", error="boom"),
                    ScrapeRun(source_id=src.id, started_at=now - timedelta(hours=2), status="ok", jobs_added=4)])
        add_job(db, source_key="remoteok")
    admin._running.add("remoteok")
    row = next(s for s in client.get("/admin/sources", headers=H).json() if s["key"] == "remoteok")
    assert row["running"] is True and row["total_jobs"] == 1
    assert (row["runs_24h"], row["failed_24h"]) == (2, 1)
    assert row["last_run"]["status"] == "failed" and row["last_run"]["error"] == "boom"


def test_run_durable_source_now(env, monkeypatch):
    client, _ = env
    calls = []
    monkeypatch.setitem(admin.DURABLE, "remoteok", type("M", (), {"run": staticmethod(lambda: calls.append(1) or {})}))
    r = client.post("/admin/sources/remoteok/run", headers=H)
    assert r.status_code == 202 and calls == [1] and "remoteok" not in admin._running   # lock released


def test_run_unknown_source_404_and_double_start_409(env):
    client, _ = env
    assert client.post("/admin/sources/nope/run", headers=H).status_code == 404
    admin._running.add("lever")
    assert client.post("/admin/sources/lever/run", headers=H).status_code == 409


def test_lock_is_released_even_if_the_run_crashes(env, monkeypatch):
    client, _ = env
    def boom():
        raise RuntimeError("x")
    monkeypatch.setitem(admin.DURABLE, "ashby", type("M", (), {"run": staticmethod(boom)}))
    with pytest.raises(RuntimeError):
        client.post("/admin/sources/ashby/run", headers=H)
    assert "ashby" not in admin._running


def test_run_google_source_now_runs_that_exact_query(env, monkeypatch):
    client, _ = env
    spec = query_plan.build_plan()[0]
    seen = []
    monkeypatch.setattr(query_plan, "run_query_now", lambda text, **k: seen.append(text) or {})
    r = client.post(f"/admin/sources/{pipeline._source_key(spec.text)}/run", headers=H)
    assert r.status_code == 202 and seen == [spec.text] and "search credit" in r.json()["note"]


def test_run_query_now_respects_the_budget(env, monkeypatch):
    _, _ = env
    monkeypatch.setattr(settings, "serpapi_monthly_budget", 0)
    monkeypatch.setattr(pipeline, "run_pipeline_for_query", lambda *a, **k: pytest.fail("must not search"))
    out = query_plan.run_query_now(query_plan.build_plan()[0].text)
    assert out["searched"] is False and "budget" in out["error"]
    with pytest.raises(KeyError):
        query_plan.run_query_now("not in the plan")


# ---------- failed urls + requeue ----------
R = SearchResult(url="https://example.org/job/9", title="Copywriter", snippet="s", source_query="q", platform="web")
PAGE = "<html><body>" + "job text " * 80 + "</body></html>"


def run_with(results, monkeypatch, extract):
    monkeypatch.setattr(google_search, "search", lambda *a, **k: results)
    monkeypatch.setattr(page_fetcher, "fetch_page_for_result", lambda r: (PAGE, True))
    monkeypatch.setattr(llm_extractor, "extract", extract)
    return pipeline.run_pipeline_for_query("q")


def fail_extract(r, h):
    raise ExtractionError("model returned junk")


def ok_extract(r, h):
    return ExtractedJob(is_real_job=True, title="Copywriter", usage={"input_tokens": 1, "output_tokens": 1})


def test_failures_are_persisted_and_listed(env, monkeypatch):
    client, scope = env
    stats = run_with([R], monkeypatch, fail_extract)
    assert stats["skipped"] == 1 and stats["added"] == 0
    body = client.get("/admin/failed-urls", headers=H).json()
    assert body["counts"] == {"pending": 1}
    item = body["items"][0]
    assert item["url"] == R.url and "junk" in item["error"] and item["attempts"] == 1


def test_one_failure_does_not_poison_the_rest_of_the_batch(env, monkeypatch):
    _, scope = env
    good = R.model_copy(update={"url": "https://example.org/job/good"})
    def extract(r, h):
        if r.url == R.url:
            raise RuntimeError("db exploded")        # non-Extraction error: also isolated
        return ok_extract(r, h)
    stats = run_with([R, good], monkeypatch, extract)
    assert (stats["errors"], stats["added"]) == (1, 1)
    with scope() as db:
        assert db.scalar(select(FailedUrl).where(FailedUrl.url == R.url)).status == "pending"
        assert db.scalar(select(Job).where(Job.source_url == good.url)) is not None


def test_requeue_resolves_and_stores_the_job_without_a_search(env, monkeypatch):
    client, scope = env
    run_with([R], monkeypatch, fail_extract)
    monkeypatch.setattr(google_search, "search", lambda *a, **k: pytest.fail("requeue must not search"))
    monkeypatch.setattr(llm_extractor, "extract", ok_extract)
    r = client.post("/admin/failed-urls/requeue", json={}, headers=H)
    assert r.status_code == 202 and "requeue" not in admin._running
    with scope() as db:
        assert db.scalar(select(FailedUrl)).status == "resolved"
        assert db.scalar(select(Job).where(Job.source_url == R.url)) is not None
    assert client.get("/admin/failed-urls", headers=H).json()["items"] == []        # pending list now empty


def test_url_goes_dead_after_max_attempts_and_ids_force_one_more_try(env, monkeypatch):
    client, scope = env
    for _ in range(pipeline.MAX_FAILURE_ATTEMPTS):
        run_with([R], monkeypatch, fail_extract)
    with scope() as db:
        fu = db.scalar(select(FailedUrl)); fid = fu.id
        assert (fu.status, fu.attempts) == ("dead", pipeline.MAX_FAILURE_ATTEMPTS)
    assert client.get("/admin/failed-urls?status=dead", headers=H).json()["counts"] == {"dead": 1}
    monkeypatch.setattr(llm_extractor, "extract", fail_extract)
    pipeline.requeue_failed()                                                      # default skips dead rows
    with scope() as db:
        assert db.scalar(select(FailedUrl)).attempts == pipeline.MAX_FAILURE_ATTEMPTS
    monkeypatch.setattr(llm_extractor, "extract", ok_extract)
    client.post("/admin/failed-urls/requeue", json={"ids": [fid]}, headers=H)
    with scope() as db:
        assert db.scalar(select(FailedUrl)).status == "resolved"


def test_requeue_without_anthropic_key_does_nothing(env, monkeypatch):
    _, scope = env
    run_with([R], monkeypatch, fail_extract)
    monkeypatch.setattr(settings, "anthropic_api_key", "")
    stats = pipeline.requeue_failed()
    assert stats["error"] and stats["requeued"] == 0


def test_requeue_double_start_409(env):
    client, _ = env
    admin._running.add("requeue")
    assert client.post("/admin/failed-urls/requeue", json={}, headers=H).status_code == 409


# ---------- retag + backups ----------
def test_admin_retag(env):
    client, scope = env
    with scope() as db:
        add_job(db, title="Email Marketer")
        db.execute(JobTag.__table__.delete())
    assert client.post("/admin/retag", headers=H).json() == {"retagged": 1}
    assert client.get("/jobs?tag=email-marketing", headers=H).json() != []


def test_backups_status(env, monkeypatch, tmp_path):
    client, _ = env
    monkeypatch.setattr(settings, "backup_dir", str(tmp_path / "missing"))
    assert client.get("/admin/backups", headers=H).json()["status"] == "none"
    monkeypatch.setattr(settings, "backup_dir", str(tmp_path))
    fresh = tmp_path / "clientfinder-2026-10-08_033000.dump"; fresh.write_bytes(b"x" * 10)
    (tmp_path / "last_run.json").write_text(json.dumps({"verified": True, "jobs_rows": 3}))
    body = client.get("/admin/backups", headers=H).json()
    assert body["status"] == "ok" and body["count"] == 1 and body["latest"][0]["bytes"] == 10 and body["last_run"]["jobs_rows"] == 3
    old = time.time() - 40 * 3600
    os.utime(fresh, (old, old))
    assert client.get("/admin/backups", headers=H).json()["status"] == "stale"


# ---------- postgres column limits (sqlite does not enforce them, Postgres drops the row) ----------
def test_overlong_extracted_fields_are_clipped_not_dropped(env, monkeypatch):
    _, scope = env
    r = R.model_copy(update={"url": "https://example.org/long"})
    def extract(res, h):
        return ExtractedJob(is_real_job=True, title="T" * 600, company_or_poster="C" * 400, location="L" * 300,
                            pay_text="P" * 200, type="x" * 50, usage=None)
    stats = run_with([r], monkeypatch, extract)
    assert stats["added"] == 1 and stats["errors"] == 0
    with scope() as db:
        j = db.scalar(select(Job).where(Job.source_url == r.url))
        assert (len(j.title), len(j.company_or_poster), len(j.location), len(j.pay_text), len(j.type)) == (512, 256, 128, 128, 32)


def test_clip_limits_match_the_real_columns():
    from app.scrapers.common import LIMITS
    for field, n in LIMITS.items():
        assert getattr(Job, field).property.columns[0].type.length == n, field
