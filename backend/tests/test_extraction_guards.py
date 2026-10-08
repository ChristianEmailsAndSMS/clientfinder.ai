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


def stub(monkeypatch, text, usage=None, stop="end_turn"):
    usage = usage or {"model": "m", "input_tokens": 100, "output_tokens": 20}
    monkeypatch.setattr(llm_extractor, "_call_model", lambda prompt: (text, usage, stop))


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
    monkeypatch.setattr(llm_extractor, "_call_model", lambda prompt: (seen.update(p=prompt) or GOOD, {"model": "m", "input_tokens": 1, "output_tokens": 1}, "end_turn"))
    llm_extractor.extract(R, HTML)
    assert "Pay $50/hr" in seen["p"] and "{page_text}" not in seen["p"] and '"is_real_job": bool' in seen["p"]


# =============== Claude Haiku 5.5 request/response rules ===============
import anthropic as _anthropic_module
_REAL_ANTHROPIC = _anthropic_module.Anthropic

def _fake_client(monkeypatch, content, stop="end_turn", usage=(2600, 420)):
    """Run the real _call_model against a fake HTTP layer; returns what the SDK actually sent."""
    import json
    import httpx2
    import anthropic
    sent = {}

    def handler(request):
        sent["body"] = json.loads(request.content)
        return httpx2.Response(200, json={"id": "msg_1", "type": "message", "role": "assistant", "model": "claude-haiku-5-5",
                                          "content": content, "stop_reason": stop, "stop_sequence": None,
                                          "usage": {"input_tokens": usage[0], "output_tokens": usage[1]}})

    monkeypatch.setattr(anthropic, "Anthropic", lambda **kw: _REAL_ANTHROPIC(http_client=httpx2.Client(transport=httpx2.MockTransport(handler)), **kw))
    return sent


def test_request_matches_haiku_5_5_rules(monkeypatch):
    sent = _fake_client(monkeypatch, [{"type": "text", "text": GOOD}])
    monkeypatch.setattr(settings, "extraction_model", "claude-haiku-5-5")
    llm_extractor.extract(R, HTML)
    body = sent["body"]
    assert body["model"] == "claude-haiku-5-5"
    assert body["output_config"] == {"effort": "low"}                    # simple extraction: little thinking
    assert body["max_tokens"] >= 2048                                    # thinking counts toward max_tokens
    for banned in ("temperature", "top_p", "top_k", "thinking"):         # sampling params 400 on Haiku 5.5; budget_tokens 400s
        assert banned not in body, banned
    assert body["messages"][-1]["role"] == "user"                        # no assistant prefill (400 on Haiku 5.5)


def test_effort_is_not_sent_to_models_that_reject_it(monkeypatch):
    sent = _fake_client(monkeypatch, [{"type": "text", "text": GOOD}])
    monkeypatch.setattr(settings, "extraction_model", "claude-haiku-4-5")
    llm_extractor.extract(R, HTML)
    assert "output_config" not in sent["body"]


def test_a_leading_thinking_block_does_not_break_extraction(monkeypatch):
    _fake_client(monkeypatch, [{"type": "thinking", "thinking": "", "signature": "sig"}, {"type": "text", "text": GOOD}])
    job = llm_extractor.extract(R, HTML)
    assert job.title == "Copywriter" and job.usage["output_tokens"] == 420      # thinking tokens are part of usage.output_tokens


def test_refusal_and_cutoff_raise_but_still_report_the_tokens_we_paid_for(monkeypatch):
    _fake_client(monkeypatch, [{"type": "text", "text": ""}], stop="refusal", usage=(2600, 7))
    with pytest.raises(ExtractionError) as e:
        llm_extractor.extract(R, HTML)
    assert "declined" in str(e.value) and e.value.usage["input_tokens"] == 2600
    _fake_client(monkeypatch, [{"type": "thinking", "thinking": "", "signature": "s"}, {"type": "text", "text": '{"is_real_job": tr'}], stop="max_tokens", usage=(2600, 2048))
    with pytest.raises(ExtractionError) as e:
        llm_extractor.extract(R, HTML)
    assert "cut off" in str(e.value) and e.value.usage["output_tokens"] == 2048


def test_unparseable_output_still_reports_usage(monkeypatch):
    stub(monkeypatch, "I think this is a job!", usage={"model": "m", "input_tokens": 500, "output_tokens": 30})
    with pytest.raises(ExtractionError) as e:
        llm_extractor.extract(R, HTML)
    assert e.value.usage == {"model": "m", "input_tokens": 500, "output_tokens": 30}


def test_the_pipeline_counts_tokens_from_failed_extractions(monkeypatch):
    """A page the model read but we could not use is still billed to whoever asked for the search."""
    from app.scrapers import page_fetcher
    seen = pipeline.__dict__
    stats = pipeline._new_stats("q")
    monkeypatch.setattr(page_fetcher, "fetch_page_for_result", lambda r: (HTML, True))

    def extract(r, h):
        raise ExtractionError("model declined this page", {"model": "m", "input_tokens": 900, "output_tokens": 11})
    monkeypatch.setattr(llm_extractor, "extract", extract)

    class FakeDB:
        def begin_nested(self):
            import contextlib
            return contextlib.nullcontext()

        def scalar(self, *a, **k):
            return None

        def add(self, *a):
            pass

        def flush(self):
            pass

    pipeline._run_one(FakeDB(), type("S", (), {"key": "k"})(), R, set(), stats)
    assert (stats["tokens_in"], stats["tokens_out"], stats["skipped"]) == (900, 11, 1)
