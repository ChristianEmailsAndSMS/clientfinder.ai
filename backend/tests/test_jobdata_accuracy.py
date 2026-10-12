"""Job data the customer sees must be true: places, platforms, tags, pay, duplicates, and the numbers beside the filters."""
import pytest

from test_customer_api import add_job, env, found, member, titles  # noqa: F401  (fixtures)
from app import geo
from app.dedup import dedupe_hash
from app.scrapers import greenhouse, lever, remoteok
from app.scrapers.common import title_matches
from app.scrapers.google_search import _platform_from_url
from app.tagging import derive_tags


@pytest.mark.parametrize("text,expected", [
    ("Join us in person", None), ("Contact us", None), ("Let us know where you are", None),
    ("Remote, in the EU", "europe_west"), ("Remote, or hybrid", None), ("Hybrid, on-site 2 days", None), ("Remote, me", None),
    ("Berlin, DE", "europe_west"), ("Bangalore, IN", "south_asia"), ("Bogota, CO", "americas_east"), ("Toronto, CA", "americas_east"),
    ("Boulder, CO", "americas_west"), ("Wilmington, DE", "americas_east"), ("Indianapolis, IN", "americas_east"),
    ("Paris, TX", "americas_west"), ("Dublin, OH", "americas_east"), ("London, ON", "americas_east"), ("Portland, ME", "americas_east"),
    ("Portland, OR", "americas_west"), ("Paris, France", "europe_west"),
    ("Anywhere in the US", "americas_west americas_east"), ("Anywhere in LATAM", "americas_west americas_east"),
    ("Remote - Anywhere in the USA", "americas_west americas_east"), ("Anywhere in the world", "worldwide"), ("Anywhere (EU preferred)", "worldwide europe_west"),
    ("Remote (EMEA)", "europe_west europe_east_mea"), ("Tbilisi, Georgia", "europe_east_mea"), ("Georgia", None), ("Atlanta, Georgia", "americas_east"),
    ("NYC", "americas_east"), ("Remote - East Coast", "americas_east"), ("Spokane, Washington", "americas_west"), ("Washington D.C.", "americas_east"),
    ("US only", "americas_west americas_east"), ("Remote, USA", "americas_west americas_east"), ("Austin, TX", "americas_west"),
])
def test_location_text_is_classified_without_false_places(text, expected):
    assert geo.classify(text) == expected


@pytest.mark.parametrize("url,platform", [
    ("https://www.wix.com/blog/hiring", "web"), ("https://www.netflix.com/jobs", "web"), ("https://www.dropbox.com/jobs", "web"),
    ("https://clever.com/careers", "web"), ("https://example.com/jobs?ref=twitter.com", "web"), ("https://www.linkedin.com.evil.com/x", "web"),
    ("https://x.com/a/status/1", "twitter"), ("https://mobile.twitter.com/a/status/1", "twitter"), ("https://jobs.lever.co/acme/1", "lever"),
    ("https://www.linkedin.com/posts/foo", "linkedin"), ("https://www.linkedin.com/jobs/view/123", "linkedin_jobs"),
    ("https://remoteok.com/remote-jobs/1", "remoteok"), ("https://www.facebook.com/groups/x/posts/1", "facebook"),
])
def test_platform_is_decided_by_the_host_not_by_text_anywhere_in_the_url(url, platform):
    assert _platform_from_url(url) == platform


def test_tags_do_not_fire_inside_other_words_or_other_jobs():
    assert "funnels-landing-pages" not in derive_tags(title="Macro Economist")
    assert "funnels-landing-pages" not in derive_tags(title="Micro Influencer Manager")
    assert "funnels-landing-pages" in derive_tags(title="Head of CRO (Remote)")
    assert "lifecycle-crm" not in derive_tags(title="Product Lifecycle Manager")
    assert "lifecycle-crm" in derive_tags(title="Lifecycle Marketing Manager")
    assert not title_matches("Software Development Lifecycle Engineer") and title_matches("Lifecycle Marketing Manager")


def test_sources_do_not_ingest_unrelated_titles():
    assert not greenhouse._matches_title("Email Security Engineer") and not greenhouse._matches_title("Staff Engineer, Email Infrastructure")
    assert greenhouse._matches_title("Email Marketing Manager") and greenhouse._matches_title("Senior Copywriter")
    assert not remoteok._matches({"position": "Senior Python Developer", "description": "sales funnel and landing page analytics", "tags": []})
    assert remoteok._matches({"position": "Email Marketer", "description": "", "tags": []})


def test_lever_monthly_and_weekly_pay_is_stored_as_the_yearly_figure_it_is_labelled_as():
    out = lever.parse("acme", [{"text": "Email Marketing Manager", "hostedUrl": "https://jobs.lever.co/acme/1",
                                "salaryRange": {"min": 6000, "max": 8000, "currency": "USD", "interval": "per-month-salary"}}]).jobs[0]
    assert (out.pay_min, out.pay_max, out.pay_period) == (72000, 96000, "year")
    wk = lever.parse("acme", [{"text": "Copywriter", "hostedUrl": "https://jobs.lever.co/acme/2",
                               "salaryRange": {"min": 1000, "max": 1500, "interval": "per-week-salary"}}]).jobs[0]
    assert (wk.pay_min, wk.pay_max, wk.pay_period) == (52000, 78000, "year")


def test_dedupe_does_not_merge_different_jobs_and_still_merges_trackers():
    h = lambda u: dedupe_hash(url=u, title="t")
    assert h("https://forum.example.com/viewtopic.php?t=111") != h("https://forum.example.com/viewtopic.php?t=222")
    assert h("https://example.com/jobs?s=1") != h("https://example.com/jobs?s=2")
    assert h("https://x.com/a/status/1?s=20&t=abc") == h("https://x.com/a/status/1")
    assert h("https://boards.greenhouse.io/acme/jobs/1?gh_src=abc") == h("https://boards.greenhouse.io/acme/jobs/1")
    assert h("https://www.linkedin.com/jobs/view/123?trackingId=a") == h("https://www.linkedin.com/jobs/view/123?trackingId=b")


def test_has_pay_means_a_real_number_in_the_filter_the_tag_and_the_facet(env):
    with env.scope() as db:
        add_job(db, title="Copywriter doe", pay_text="DOE")
        add_job(db, title="Copywriter unpaid", pay_text="Unpaid")
        add_job(db, title="Copywriter zero", pay_min=0.0, pay_max=0.0)
        add_job(db, title="Copywriter real", pay_text="$50/hr")
        add_job(db, title="Copywriter min only", pay_min=90000)
    c = member(env)
    assert found(c, "has_pay=true") == ["Copywriter min only", "Copywriter real"]
    assert {t["value"]: t["count"] for t in c.get("/jobs/facets").json()["tags"]}["has-pay"] == 2


def test_remote_filter_on_site_excludes_jobs_whose_remote_status_is_unknown(env):
    with env.scope() as db:
        add_job(db, title="Copywriter remote", remote=True)
        add_job(db, title="Copywriter onsite", remote=False)
        add_job(db, title="Copywriter unknown", remote=None)
    c = member(env)
    assert found(c, "remote=false") == ["Copywriter onsite"] and found(c, "remote=true") == ["Copywriter remote"]


def test_minimum_pay_needs_a_period_and_keeps_open_ended_pay(env):
    with env.scope() as db:
        add_job(db, title="Copywriter 90k plus", pay_min=90000, pay_period="year")
        add_job(db, title="Copywriter 40 an hour", pay_min=40, pay_max=60, pay_period="hour")
    c = member(env)
    assert c.get("/jobs?min_pay=50").status_code == 422
    assert found(c, "pay_period=year&min_pay=50000") == ["Copywriter 90k plus"]
    assert found(c, "pay_period=hour&min_pay=50") == ["Copywriter 40 an hour"]
    assert found(c, "pay_period=year&max_pay=100000") == ["Copywriter 90k plus"]


def test_region_facet_count_matches_the_list_when_worldwide_is_included(env):
    with env.scope() as db:
        add_job(db, title="Copywriter eu", location="Berlin, Germany")
        add_job(db, title="Copywriter ww", location="Remote - Worldwide")
        add_job(db, title="Copywriter us", location="Austin, TX")
    c = member(env)
    eu = {r["value"]: r for r in c.get("/jobs/facets").json()["regions"]}["europe_west"]
    assert eu["count"] == 1 and eu["count_with_worldwide"] == int(c.get("/jobs?region=europe_west").headers["x-total-count"]) == 2
