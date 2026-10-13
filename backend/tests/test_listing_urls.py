"""'Open post' must open one job, not a search, location list or company page."""
import pytest

from app.scrapers.pipeline import is_listing_url, not_a_hiring_post

LISTINGS = [
    "https://www.indeed.com/jobs?q=copywriter&l=remote", "https://www.indeed.com/q-copywriter-jobs.html", "https://uk.indeed.com/l-london-jobs.html",
    "https://www.indeed.com/cmp/Acme", "https://jobgether.com/remote-jobs/copywriter", "https://jobgether.com/location/united-states",
    "https://www.linkedin.com/jobs/search/?keywords=copywriter", "https://www.linkedin.com/jobs/copywriter-jobs-new-york",
    "https://www.linkedin.com/jobs/collections/recommended", "https://www.glassdoor.com/Job/copywriter-jobs-SRCH_KO0,10.htm",
]
SINGLE = [
    "https://www.indeed.com/viewjob?jk=abc123def456", "https://www.indeed.com/rc/clk?jk=abc123&from=serp", "https://www.indeed.com/cmp/Acme/jobs/Copywriter-1a2b3c",
    "https://jobgether.com/offer/65f0c1-senior-copywriter", "https://www.linkedin.com/jobs/view/3876543210",
    "https://www.glassdoor.com/job-listing/copywriter-acme-JV_IC1132348_KO0,10_KE11,15.htm?jl=1",
    "https://remoteok.com/remote-jobs/remote-copywriter-acme-12345", "https://www.reddit.com/r/forhire/comments/abc/hiring_copywriter/",
]


@pytest.mark.parametrize("url", LISTINGS)
def test_search_and_list_pages_are_not_jobs(url):
    assert is_listing_url(url) and not_a_hiring_post("Copywriter jobs", "", "hiring copywriter", url)


@pytest.mark.parametrize("url", SINGLE)
def test_single_job_addresses_are_kept(url):
    assert not is_listing_url(url)


def test_freelancer_com_rule_does_not_match_other_sites_with_the_same_ending():
    assert is_listing_url("https://www.freelancer.com/jobs/copywriting/")
    assert is_listing_url("https://freelancer.com/job-search/copywriter")
    assert not is_listing_url("https://getfreelancer.com/jobs/engaging-website-copywriter-wanted-cf167b76")


TEMPLATES = [
    "https://www.linkedin.com/jobs/view/[job-id]", "https://apply.workable.com/acme/j/...", "https://jobs.example.com/%7Bjob_id%7D",
    "https://example.com/jobs/{{id}}", "https://boards.greenhouse.io/acme/jobs/<id>", "https://example.com/careers/copywriter-…",
    "https://example.com/jobs/job-id",
]


@pytest.mark.parametrize("url", TEMPLATES)
def test_placeholder_and_truncated_addresses_are_never_stored_and_are_purged(url):
    from app.security_utils import looks_templated, safe_job_url
    assert looks_templated(url) and safe_job_url(url) is None and is_listing_url(url)


@pytest.mark.parametrize("url", ["https://boards.greenhouse.io/acme/jobs/4012345", "https://apply.workable.com/acme/j/ABC123DEF4/", "https://www.linkedin.com/jobs/view/3876543210",
                                 "https://example.com/careers/copywriter-lead?utm_source=x&gh_jid=77"])
def test_real_addresses_are_untouched_by_the_placeholder_rule(url):
    from app.security_utils import looks_templated, safe_job_url
    assert not looks_templated(url) and safe_job_url(url) == url
