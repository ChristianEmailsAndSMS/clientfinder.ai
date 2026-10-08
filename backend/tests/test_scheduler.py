from app import scheduler
from app.config import settings


def ids(s):
    return {j.id for j in s.get_jobs()}


def test_durable_jobs_always_scheduled_google_not_without_keys(monkeypatch):
    monkeypatch.setattr(settings, "dev_fixtures", False)
    monkeypatch.setattr(settings, "serpapi_api_key", "")
    monkeypatch.setattr(settings, "serper_api_key", "")
    assert ids(scheduler.build_scheduler()) == {f"durable:{n}" for n in ("remoteok", "problogger", "greenhouse", "remotive", "weworkremotely", "lever", "ashby")}


def test_google_scheduled_only_with_keys_AND_the_explicit_opt_in(monkeypatch):
    monkeypatch.setattr(settings, "dev_fixtures", False)
    monkeypatch.setattr(settings, "serpapi_api_key", "k")
    monkeypatch.setattr(settings, "anthropic_api_key", "k")
    monkeypatch.setattr(settings, "google_schedule_enabled", False)           # keys alone must NOT start spending money on a timer
    assert "google" not in ids(scheduler.build_scheduler())
    ok, why = scheduler.google_layer_ready()
    assert ok is False and "customer-paid" in why
    monkeypatch.setattr(settings, "google_schedule_enabled", True)
    assert "google" in ids(scheduler.build_scheduler())


def test_google_scheduled_with_both_keys(monkeypatch):
    monkeypatch.setattr(settings, "google_schedule_enabled", True)
    monkeypatch.setattr(settings, "dev_fixtures", False)
    monkeypatch.setattr(settings, "serpapi_api_key", "k")
    monkeypatch.setattr(settings, "anthropic_api_key", "k")
    assert "google" in ids(scheduler.build_scheduler())


def test_google_never_scheduled_in_fixture_mode(monkeypatch):
    monkeypatch.setattr(settings, "google_schedule_enabled", True)
    monkeypatch.setattr(settings, "dev_fixtures", True)
    monkeypatch.setattr(settings, "serpapi_api_key", "k")
    monkeypatch.setattr(settings, "anthropic_api_key", "k")
    assert "google" not in ids(scheduler.build_scheduler())
