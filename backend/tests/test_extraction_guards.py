"""Guards that keep fake or guessed data out of the live DB."""
import pytest

from app.config import settings
from app.scrapers import google_search, llm_extractor, pipeline
from app.scrapers.llm_extractor import ExtractionError
from app.schemas import SearchResult

R = SearchResult(url="https://example.org/j/1", title="Hiring copywriter", snippet="s", source_query="q", platform="web")
HTML = "<html><body><h1>Copywriter</h1><p>Pay $50/hr, remote.</p></body></html>"
GOOD = '{"is_real_job": true, "title": "Copywriter", "company_or_poster": "Acme", "type": "contract"}'


@pytest.fixture(autouse=True)
def live_mode(monkeypatch):
    monkeypatch.setattr(settings, "dev_fixtures", False)
    monkeypatch.setattr(settings, "anthropic_api_key", "test-key")
    monkeypatch.setattr(settings, "serpapi_api_key", "")
    monkeypatch.setattr(settings, "serper_api_key", "")


def stub(monkeypatch, text, usage=None):
    usage = usage or {"model": "m", "input_tokens": 100, "output_tokens": 20}
    monkeypatch.setattr(llm_extractor, "_call_model", lambda prompt: (text, usage))


def test_dev_default_is_off():
    assert type(settings).model_fields["dev_fixtures"].default is False


def test_live_extract_returns_job_with_token_usage(monkeypatch):
    stub(monkeypatch, GOOD)
    job = llm_extractor.extract(R, HTML)
    assert job.title == "Copywriter"
    assert job.usage == {"model": "m", "input_tokens": 100, "output_tokens": 20}


def test_code_fenced_json_is_accepted(monkeypatch):
    stub(monkeypatch, "```json\n" + GOOD + "\n```")
    assert llm_extractor.extract(R, HTML).title == "Copywriter"


@pytest.mark.parametrize("bad", ["not json", "[]", '{"is_real_job": true, "title": ""}', '{"skills": "x"}'])
def test_bad_model_output_raises_instead_of_guessing(monkeypatch, bad):
    stub(monkeypatch, bad)
    with pytest.raises(ExtractionError):
        llm_extractor.extract(R, HTML)


def test_model_failure_raises(monkeypatch):
    def boom(prompt):
        raise RuntimeError("429")
    monkeypatch.setattr(llm_extractor, "_call_model", boom)
    with pytest.raises(ExtractionError):
        llm_extractor.extract(R, HTML)


def test_missing_anthropic_key_raises(monkeypatch):
    monkeypatch.setattr(settings, "anthropic_api_key", "")
    with pytest.raises(ExtractionError):
        llm_extractor.extract(R, HTML)


def test_not_a_job_is_not_a_failure(monkeypatch):
    stub(monkeypatch, '{"is_real_job": false, "title": ""}')
    assert llm_extractor.extract(R, HTML).is_real_job is False


def test_empty_page_raises(monkeypatch):
    stub(monkeypatch, GOOD)
    with pytest.raises(ExtractionError):
        llm_extractor.extract(R, "<html><script>x</script></html>")


def test_dev_mode_still_uses_mock(monkeypatch):
    monkeypatch.setattr(settings, "dev_fixtures", True)
    assert llm_extractor.extract(R, HTML).usage is None


def test_search_without_key_returns_nothing_not_fixtures():
    assert google_search.search('"hiring copywriter"') == []


def test_pipeline_without_anthropic_key_does_nothing(monkeypatch):
    monkeypatch.setattr(settings, "anthropic_api_key", "")
    called = []
    monkeypatch.setattr(google_search, "search", lambda *a, **k: called.append(1) or [])
    stats = pipeline.run_pipeline_for_query("q")
    assert stats["error"] and not called


def test_prompt_is_built_without_format_errors(monkeypatch):
    seen = {}
    monkeypatch.setattr(llm_extractor, "_call_model", lambda prompt: (seen.update(p=prompt) or GOOD, {"model": "m", "input_tokens": 1, "output_tokens": 1}))
    llm_extractor.extract(R, HTML)
    assert "Pay $50/hr" in seen["p"] and "{page_text}" not in seen["p"] and '"is_real_job": bool' in seen["p"]
