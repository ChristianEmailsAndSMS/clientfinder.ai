"""Locks for the dev/fallback extractor in llm_extractor._heuristic_mock.

No test calls Anthropic or the network. Claude is stubbed when the fallback
path is exercised.
"""
import inspect

import pytest

from app.scrapers import llm_extractor
from app.schemas import SearchResult


def listing(title: str = "Copywriter", snippet: str = "", page: str = ""):
    result = SearchResult(
        url="https://example.com/job",
        title=title,
        snippet=snippet,
        source_query="freelance copywriter",
        platform="web",
    )
    return llm_extractor._heuristic_mock(result, page)


def test_call_signatures_stay_the_same():
    assert list(inspect.signature(llm_extractor.extract).parameters) == ["result", "page_html"]
    assert list(inspect.signature(llm_extractor._heuristic_mock).parameters) == ["result", "page_text"]
    assert list(inspect.signature(llm_extractor._html_to_text).parameters) == ["html", "max_chars"]
    assert list(inspect.signature(llm_extractor._claude_extract).parameters) == ["result", "page_html"]


@pytest.mark.parametrize("snippet", ["Budget $5000 keywords", "Budget $5,000 keywords"])
def test_thousands_suffix_does_not_consume_the_next_word(snippet):
    job = listing(snippet=snippet)
    assert job.pay_min == 5000
    assert job.pay_max is None
    assert job.pay_period is None


@pytest.mark.parametrize("snippet", ["Salary $80k", "Salary $80K", "Salary $80k per year"])
def test_glued_k_suffix_is_yearly(snippet):
    job = listing(snippet=snippet)
    assert job.pay_min == 80000
    assert job.pay_max is None
    assert job.pay_period == "year"


def test_uppercase_k_in_the_title_is_yearly():
    job = listing(title="Senior copywriter $80K", snippet="Remote role")
    assert job.pay_min == 80000
    assert job.pay_period == "year"


@pytest.mark.parametrize("snippet", ["$120k-$150k", "$120K-$150K", "$120k - $150k", "$120k\u2013$150k"])
def test_k_on_both_sides_of_a_range_keeps_both_bounds(snippet):
    job = listing(snippet=f"Pay {snippet}")
    assert job.pay_min == 120000
    assert job.pay_max == 150000
    assert job.pay_period == "year"


@pytest.mark.parametrize("snippet", ["$120-150k", "$120 - 150k", "$120-150K"])
def test_trailing_k_scales_both_range_bounds(snippet):
    job = listing(snippet=f"Pay {snippet}")
    assert job.pay_min == 120000
    assert job.pay_max == 150000
    assert job.pay_period == "year"


def test_comma_separated_range():
    job = listing(snippet="Pay $95,000-$130,000")
    assert job.pay_min == 95000
    assert job.pay_max == 130000


@pytest.mark.parametrize("snippet,pay_min", [("$2k project", 2000), ("$3K/project", 3000), ("$500 per project", 500)])
def test_project_fee_is_not_a_yearly_salary(snippet, pay_min):
    job = listing(snippet=f"Fee {snippet}")
    assert job.pay_min == pay_min
    assert job.pay_max is None
    assert job.pay_period == "project"


def test_per_page_range_keeps_numbers_without_a_new_period():
    job = listing(snippet="Rate $800-1,500 per page")
    assert job.pay_min == 800
    assert job.pay_max == 1500
    assert job.pay_period is None


@pytest.mark.parametrize(
    "snippet,pay_min,pay_max",
    [
        ("$75/hr", 75, None),
        ("$50-70/hr", 50, 70),
        ("$50 - $70/hr", 50, 70),
        ("$75 per hour", 75, None),
        ("$50\u201370/hr", 50, 70),
    ],
)
def test_hourly_bounds(snippet, pay_min, pay_max):
    job = listing(snippet=f"Rate {snippet}")
    assert job.pay_min == pay_min
    assert job.pay_max == pay_max
    assert job.pay_period == "hour"


@pytest.mark.parametrize("snippet", ["$3,000/mo", "$3,000/month", "$3,000 per month"])
def test_monthly_rate(snippet):
    job = listing(snippet=f"Retainer {snippet}")
    assert job.pay_min == 3000
    assert job.pay_max is None
    assert job.pay_period == "month"


def test_explicit_year_amount_is_not_rescaled():
    job = listing(snippet="Salary $5000 per year keywords")
    assert job.pay_min == 5000
    assert job.pay_period == "year"


def test_plain_page_text_supplies_pay_when_listing_has_none():
    job = listing(title="Brand copywriter", snippet="We are hiring", page="Fee is $2k project")
    assert job.pay_min == 2000
    assert job.pay_max is None
    assert job.pay_period == "project"


def test_html_page_text_supplies_pay_when_listing_has_none():
    page = (
        "<html><head><script>var track='$1';</script></head>"
        "<body><p>Compensation $120k-$150k</p></body></html>"
    )
    job = listing(title="Brand copywriter", snippet="We are hiring", page=page)
    assert job.pay_min == 120000
    assert job.pay_max == 150000
    assert job.pay_period == "year"


def test_clear_listing_pay_is_not_replaced_by_a_worse_page_match():
    page = "<script>var budget='$5000 keywords';</script><p>Also mentioned $40/hr</p>"
    job = listing(snippet="Rate $75/hr", page=page)
    assert job.pay_min == 75
    assert job.pay_max is None
    assert job.pay_period == "hour"


def test_listing_range_wins_over_a_different_page_rate():
    job = listing(snippet="Salary $120k-$150k", page="<p>Old posting said $75/hr</p>")
    assert job.pay_min == 120000
    assert job.pay_max == 150000
    assert job.pay_period == "year"


def test_pay_is_not_summed_across_snippet_and_page():
    job = listing(snippet="Rate $40/hr", page="Rate $40/hr")
    assert job.pay_min == 40
    assert job.pay_max is None
    assert job.pay_period == "hour"


def test_page_text_is_used_for_remote_and_skills():
    job = listing(
        title="Writer",
        snippet="Apply today",
        page="<p>Remote Shopify email campaigns</p>",
    )
    assert job.remote is True
    assert "shopify" in job.skills
    assert "email" in job.skills


def test_skill_is_not_duplicated_when_snippet_and_page_both_mention_it():
    job = listing(snippet="shopify expert", page="<p>shopify store</p>")
    assert job.skills.count("shopify") == 1


def test_is_real_job_stays_true():
    job = listing(title="How to hire a copywriter", snippet="A blog post about hiring")
    assert job.is_real_job is True


def test_extract_in_dev_mode_reads_page_text_without_network(monkeypatch):
    def _boom(*_args, **_kwargs):
        raise AssertionError("Claude must not be called")

    monkeypatch.setattr(llm_extractor.settings, "dev_fixtures", True)
    monkeypatch.setattr(llm_extractor.settings, "anthropic_api_key", "")
    monkeypatch.setattr(llm_extractor, "_claude_extract", _boom)
    result = SearchResult(
        url="https://example.com/job",
        title="Editor",
        snippet="Part-time role",
        source_query="editor",
        platform="web",
    )
    job = llm_extractor.extract(result, "<p>Retainer $3,000/month</p>")
    assert job.pay_min == 3000
    assert job.pay_period == "month"


def test_claude_failure_falls_back_to_page_text_without_network(monkeypatch):
    def _boom(*_args, **_kwargs):
        raise RuntimeError("anthropic down")

    monkeypatch.setattr(llm_extractor.settings, "dev_fixtures", False)
    monkeypatch.setattr(llm_extractor.settings, "anthropic_api_key", "test-key")
    monkeypatch.setattr(llm_extractor, "_claude_extract", _boom)
    result = SearchResult(
        url="https://example.com/job",
        title="Designer",
        snippet="Contract role",
        source_query="designer",
        platform="web",
    )
    job = llm_extractor.extract(result, "<div>Fee $2k project</div>")
    assert job.pay_min == 2000
    assert job.pay_period == "project"
    assert job.is_real_job is True
