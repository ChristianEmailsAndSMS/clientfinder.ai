from app import scheduler
from app.config import settings


def ids(s):
    return {j.id for j in s.get_jobs()}


def test_durable_jobs_always_scheduled_google_not_without_keys(monkeypatch):
    monkeypatch.setattr(settings, "dev_fixtures", False)
    monkeypatch.setattr(settings, "serpapi_api_key", "")
    monkeypatch.setattr(settings, "serper_api_key", "")
    assert ids(scheduler.build_scheduler()) == {"durable:remoteok", "durable:problogger", "durable:greenhouse"}


def test_google_scheduled_with_both_keys(monkeypatch):
    monkeypatch.setattr(settings, "dev_fixtures", False)
    monkeypatch.setattr(settings, "serpapi_api_key", "k")
    monkeypatch.setattr(settings, "anthropic_api_key", "k")
    assert "google" in ids(scheduler.build_scheduler())


def test_google_never_scheduled_in_fixture_mode(monkeypatch):
    monkeypatch.setattr(settings, "dev_fixtures", True)
    monkeypatch.setattr(settings, "serpapi_api_key", "k")
    monkeypatch.setattr(settings, "anthropic_api_key", "k")
    assert "google" not in ids(scheduler.build_scheduler())
