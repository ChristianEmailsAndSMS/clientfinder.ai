"""Accuracy fixes found by the API audit: pay filters/sort, /jobs/stats population, oversized ids, overview split."""
import pytest

import authkit
from authkit import CSRF
from app import credits
from app.models import CreditEntry
from test_customer_api import env, member, add_job
from test_route_matrix import people  # noqa


def titles(r):
    return [j["title"] for j in r.json()]


@pytest.fixture
def pay(env):
    with env.scope() as db:
        add_job(db, title="A $50+/hr", pay_min=50, pay_max=None, pay_period="hour")
        add_job(db, title="B $20-30/hr", pay_min=20, pay_max=30, pay_period="hour")
        add_job(db, title="C $90-120k/yr", pay_min=90000, pay_max=120000, pay_period="year")
        add_job(db, title="D up to $40/hr", pay_min=None, pay_max=40, pay_period="hour")
        add_job(db, title="E $5000 project", pay_min=5000, pay_max=5000, pay_period="project")
    return member(env)


def test_min_and_max_pay_need_a_period_because_hourly_and_yearly_numbers_are_not_comparable(pay):
    assert pay.get("/jobs?min_pay=40").status_code == 422
    assert pay.get("/jobs?max_pay=40").status_code == 422


def test_a_minimum_only_job_matches_min_pay_and_a_maximum_only_job_matches_max_pay(pay):
    assert set(titles(pay.get("/jobs?pay_period=hour&min_pay=40"))) == {"A $50+/hr", "D up to $40/hr"}      # "$50+/hr" was dropped before
    assert set(titles(pay.get("/jobs?pay_period=hour&max_pay=45"))) == {"B $20-30/hr", "D up to $40/hr"}    # "up to $40/hr" was dropped before


def test_pay_sort_compares_hourly_with_yearly_and_puts_projects_last(pay):
    hi = titles(pay.get("/jobs?sort=pay_high"))
    assert hi[:2] == ["C $90-120k/yr", "A $50+/hr"] or hi[:2] == ["A $50+/hr", "C $90-120k/yr"]
    assert hi.index("A $50+/hr") < hi.index("D up to $40/hr") < hi.index("B $20-30/hr") and hi[-1] == "E $5000 project"
    lo = titles(pay.get("/jobs?sort=pay_low"))
    assert lo.index("B $20-30/hr") < lo.index("D up to $40/hr") < lo.index("A $50+/hr") and lo[-1] == "E $5000 project"


def test_jobs_stats_counts_only_real_jobs_like_facets_and_public_stats(env):
    with env.scope() as db:
        add_job(db, title="real", platform="web", type="contract")
        add_job(db, title="real 2", platform="web", type=None)
        add_job(db, title="spam", platform="spamsite", type="contract", is_real_job=False)
    c = member(env)
    st = c.get("/jobs/stats").json()
    assert st["total"] == 2 == c.get("/jobs/facets").json()["total"]
    assert st["by_platform"] == {"web": 2} and st["by_type"] == {"contract": 1}


@pytest.mark.parametrize("who,method,path,body", [
    ("cust", "get", "/searches/99999999999999999999", None), ("cust", "get", "/searches/3000000000/jobs", None),
    ("adm", "get", "/admin/users/99999999999999999999/ledger", None), ("adm", "post", "/admin/users/3000000000/unlock", {}),
    ("adm", "post", "/admin/payments/3000000000/credit", {"user_id": 1}), ("adm", "post", "/admin/payments/1/credit", {"user_id": 3000000000}),
    ("cust", "post", "/assist", {"mode": "cover_letter", "job_id": 99999999999999999999, "job_text": "x"}),
    ("adm", "post", "/admin/failed-urls/requeue", {"ids": [3000000000]}),
])
def test_ids_too_big_for_the_database_are_a_clean_4xx_not_a_crash(people, who, method, path, body):
    env, anon, cust, adm = people
    c = cust if who == "cust" else adm
    r = getattr(c, method)(path, headers=CSRF, **({"json": body} if body is not None else {}))
    assert 400 <= r.status_code < 500, (r.status_code, r.text[:100])


def test_overview_does_not_count_pitch_helper_drafts_as_customer_searches(people):
    env, anon, cust, adm = people
    with env.scope() as db:
        credits.apply(db, cust.uid, 5_000_000, "grant", "seed")
        credits.apply(db, cust.uid, -150_000, "usage", "Search: x", allow_negative=True,
                      meta={"search_id": 1, "our_cost_micro": 100_000, "charged_micro": 150_000})
        credits.apply(db, cust.uid, -30_000, "usage", "Pitch helper: cover letter", allow_negative=True,
                      meta={"mode": "cover_letter", "our_cost_micro": 20_000, "charged_micro": 30_000})
    o = adm.get("/admin/overview").json()
    assert o["customer_searches_30d"]["count"] == 1 and o["customer_searches_30d"]["our_cost_usd"] == 0.1
    assert o["pitch_drafts_30d"]["count"] == 1 and o["pitch_drafts_30d"]["our_cost_usd"] == 0.02
