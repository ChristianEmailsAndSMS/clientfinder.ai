import pytest

from app import live_search
from app.config import settings
from app.scrapers import google_search


@pytest.mark.parametrize("q", ["hiring copywriter -site:linkedin.com", "hiring site:foo.com", "SITE: foo.com hiring"])
def test_typed_site_operator_refused(q):
    with pytest.raises(live_search.SearchRefused) as e:
        live_search.clean_query(q)
    assert e.value.status == 422


def test_negated_site_is_not_a_constraint():
    assert google_search.site_constraint("hiring copywriter -site:linkedin.com") is None
    assert google_search.site_constraint("site:linkedin.com/posts hiring") == "linkedin.com/posts"


def test_filter_site_keeps_other_results_for_negation():
    res = [google_search.SearchResult(url=u, title="t", snippet="s", source_query="q") for u in ("https://linkedin.com/posts/a", "https://example.com/job1")]
    kept = google_search._filter_site("hiring copywriter -site:linkedin.com", res)
    assert len(kept) == 2
