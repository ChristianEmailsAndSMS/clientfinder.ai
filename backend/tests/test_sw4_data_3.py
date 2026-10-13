import pytest

from app.dedup import dedupe_hash, normalize_url as N

SAME = [
    ("https://www.linkedin.com/jobs/view/copywriter-at-acme-3900123?position=3&pageNum=0&refId=A&trackingId=B",
     "https://www.linkedin.com/jobs/view/copywriter-at-acme-3900123?position=1&pageNum=2&eBP=x&refId=C"),
    ("https://www.indeed.com/viewjob?jk=abc123&from=serp&vjs=3", "https://www.indeed.com/viewjob?jk=abc123&from=vj&tk=xyz&q=a&l=b"),
    ("https://www.reddit.com/r/forhire/comments/abc/x/", "https://old.reddit.com/r/forhire/comments/abc/x/"),
    ("https://www.reddit.com/r/forhire/comments/abc/x/", "https://www.reddit.com/r/forhire/comments/abc/x/?utm_name=iossmf&context=3"),
    ("https://x.com/jane/status/123", "https://twitter.com/jane/status/123?s=20"),
    ("https://example.com/job/1", "https://www.example.com/job/1"),
    ("http://example.com/job/1", "https://example.com/job/1"),
    ("https://www.glassdoor.com/job-listing/x.htm?jl=123&pos=102&ao=1", "https://www.glassdoor.com/job-listing/x.htm?jl=123&pos=5&ao=2&src=GD_JOB_AD"),
]


@pytest.mark.parametrize("a,b", SAME)
def test_same_page_same_hash(a, b):
    assert N(a) == N(b)
    assert dedupe_hash(url=a, title="t") == dedupe_hash(url=b, title="t")


def test_real_ids_still_distinguish():
    assert N("https://www.indeed.com/viewjob?jk=abc") != N("https://www.indeed.com/viewjob?jk=abd")
    assert N("https://www.glassdoor.com/job-listing/x.htm?jl=1") != N("https://www.glassdoor.com/job-listing/x.htm?jl=2")
    assert N("https://boards.greenhouse.io/a?gh_jid=1") != N("https://boards.greenhouse.io/a?gh_jid=2")
    assert N("https://forum.example.com/view?t=1") != N("https://forum.example.com/view?t=2")
