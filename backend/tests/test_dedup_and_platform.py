"""Platform host matching and URL dedupe normalization.

These tests do not touch the network. They import pure helpers only.
"""
import inspect

import pytest

from app.dedup import dedupe_hash, norm_text, normalize_url
from app.scrapers.google_search import DEFAULT_QUERIES, _platform_from_url, search, search_all


@pytest.mark.parametrize(
    "url,expected",
    [
        ("https://twitter.com/user/status/1", "twitter"),
        ("https://mobile.twitter.com/user", "twitter"),
        ("https://www.twitter.com/foo", "twitter"),
        ("https://x.com/user", "twitter"),
        ("https://www.x.com/user", "twitter"),
        ("https://WWW.X.COM/User", "twitter"),
        ("https://mobile.x.com/i", "twitter"),
        ("https://reddit.com/r/forhire", "reddit"),
        ("https://www.reddit.com/r/forhire", "reddit"),
        ("https://old.reddit.com/r/forhire", "reddit"),
        ("https://linkedin.com/jobs", "linkedin"),
        ("https://www.linkedin.com/jobs/view/123", "linkedin"),
        ("https://upwork.com/jobs", "upwork"),
        ("https://www.upwork.com/freelance", "upwork"),
        ("https://indeed.com/viewjob", "indeed"),
        ("https://www.indeed.com/viewjob", "indeed"),
        ("https://problogger.com/jobs", "problogger"),
        ("https://www.problogger.com/jobs", "problogger"),
        ("https://mediabistro.com/jobs", "mediabistro"),
        ("https://www.mediabistro.com/jobs", "mediabistro"),
        ("https://greenhouse.io/something", "greenhouse"),
        ("https://boards.greenhouse.io/company/jobs/123", "greenhouse"),
        ("https://lever.co/jobs", "lever"),
        ("https://jobs.lever.co/company/uuid", "lever"),
        ("https://ashbyhq.com/jobs", "ashby"),
        ("https://jobs.ashbyhq.com/company/uuid", "ashby"),
        # Hosts that merely contain another platform's domain as a substring.
        ("https://netflix.com/jobs", "web"),
        ("https://www.netflix.com/jobs", "web"),
        ("https://box.com/careers", "web"),
        ("https://www.box.com/jobs", "web"),
        ("https://clever.co/jobs", "web"),
        ("https://notlinkedin.com/in/someone", "web"),
        ("https://myindeed.com/viewjob", "web"),
        ("https://notproblogger.com/jobs", "web"),
        ("https://fakemediabistro.com/jobs", "web"),
        ("https://notgreenhouse.io/x", "web"),
        ("https://notashbyhq.com/x", "web"),
        # Lookalike hosts and platform strings that live outside the hostname.
        ("https://jobs.ashbyhq.com.evil.example/x", "web"),
        ("https://example.com/path?redirect=https://x.com", "web"),
        ("https://example.com/twitter.com", "web"),
        ("https://evil.com/jobs.lever.co", "web"),
        ("https://example.com/jobs", "web"),
    ],
)
def test_platform_from_url_matches_host_or_subdomain(url: str, expected: str) -> None:
    assert _platform_from_url(url) == expected


def test_www_and_bare_host_normalize_to_the_same_url() -> None:
    bare = "https://reddit.com/r/forhire/abc"
    assert normalize_url("https://www.reddit.com/r/forhire/abc") == bare
    assert normalize_url("https://WWW.Reddit.com/r/forhire/abc/") == bare
    # Only a single leading www. label is removed.
    assert normalize_url("https://www.www.reddit.com/r/forhire/abc") == "https://www.reddit.com/r/forhire/abc"


def test_www_strip_keeps_port_and_userinfo() -> None:
    assert normalize_url("https://www.example.com:8443/jobs/") == "https://example.com:8443/jobs"
    assert normalize_url("http://user:pass@www.reddit.com/r/foo/") == "http://user:pass@reddit.com/r/foo"


def test_normalize_url_drops_fragment_tracking_params_and_trailing_slash() -> None:
    assert normalize_url("https://example.com/jobs/1#apply") == "https://example.com/jobs/1"
    assert normalize_url("https://example.com/jobs/1/") == "https://example.com/jobs/1"
    assert (
        normalize_url("https://example.com/jobs/1?utm_source=twitter&gh_jid=123&UTM_MEDIUM=email&fbclid=abc")
        == "https://example.com/jobs/1?gh_jid=123"
    )
    assert normalize_url("https://example.com/a?b=1&a=2") == "https://example.com/a?a=2&b=1"
    assert normalize_url("https://boards.greenhouse.io/acme/jobs/1?gh_jid=111&utm_campaign=x") == (
        "https://boards.greenhouse.io/acme/jobs/1?gh_jid=111"
    )


def test_www_variants_share_a_dedupe_hash_but_job_ids_do_not() -> None:
    with_www = dedupe_hash(
        url="https://www.reddit.com/r/forhire/abc?utm_source=twitter&s=1#frag",
        title="ignored",
        company="also ignored",
    )
    bare = dedupe_hash(url="https://reddit.com/r/forhire/abc/", title="different title")
    assert with_www == bare
    assert len(with_www) == 32
    assert with_www == "".join(ch for ch in with_www if ch in "0123456789abcdef")

    job_a = dedupe_hash(url="https://www.boards.greenhouse.io/acme/jobs/1?gh_jid=111&utm_source=x", title="A")
    job_b = dedupe_hash(url="https://boards.greenhouse.io/acme/jobs/1?gh_jid=222", title="B")
    assert job_a != job_b
    assert len(job_b) == 32


def test_dedupe_hash_falls_back_to_title_and_company_without_url() -> None:
    left = dedupe_hash(url=None, title="  Hiring Copywriter ", company="Acme Inc")
    right = dedupe_hash(url="", title="hiring   copywriter", company="acme inc")
    assert left == right
    assert len(left) == 32
    assert norm_text("  Hiring   Copywriter ") == "hiring copywriter"


def test_public_search_signatures_stay_stable() -> None:
    assert isinstance(DEFAULT_QUERIES, tuple)
    assert all(isinstance(q, str) and q for q in DEFAULT_QUERIES)
    assert list(inspect.signature(search).parameters) == ["query", "num"]
    assert list(inspect.signature(search_all).parameters) == ["queries", "num"]
    assert list(inspect.signature(normalize_url).parameters) == ["url"]
    assert list(inspect.signature(norm_text).parameters) == ["s"]
    assert list(inspect.signature(dedupe_hash).parameters) == ["url", "title", "company"]
