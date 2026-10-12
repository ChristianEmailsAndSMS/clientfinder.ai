"""Money numbers must match what is really charged (audit repros)."""
from sqlalchemy import select

from fastapi.testclient import TestClient
from test_customer_api import env, live, member, run, pitch, ask  # noqa: F401  (fixtures)
from authkit import login, make_user
from app.main import app
from app import pricing
from app import live_search
from app.config import settings
from app.models import CreditEntry


def test_long_prompt_rate_is_decided_per_call_not_per_search(env, live):
    """5 pages of 30,000 input tokens: every call is under the 100,000 long-prompt line, so each is priced at the cheap rate."""
    live.usage, live.n = (30_000, 500), 5
    c = member(env, balance_usd=5)
    sid = run(c, "per call tier", site=live_search.CUSTOMER_SITES[0]).json()["id"]      # one Google search, so one search fee
    with env.scope() as db:
        e = db.scalar(select(CreditEntry).where(CreditEntry.ref == f"usersearch:{sid}"))
        charged, our = -e.delta_micro, e.meta["our_cost_micro"]
    real = settings.serpapi_cost_per_search_usd * 1e6 + 5 * (30_000 * 0.10 + 500 * 0.50)
    assert abs(our - real) < 5 and 1.5 * real <= charged <= 1.5 * real + 5


def test_one_call_over_the_long_prompt_line_is_still_priced_at_the_long_rate(env, live):
    live.usage, live.n = (120_000, 500), 2
    c = member(env, balance_usd=5)
    sid = run(c, "long call", site=live_search.CUSTOMER_SITES[0]).json()["id"]
    with env.scope() as db:
        our = db.scalar(select(CreditEntry).where(CreditEntry.ref == f"usersearch:{sid}")).meta["our_cost_micro"]
    assert our == 15_000 + 2 * (120_000 * 0.50 + 500 * 2.50)


def test_a_search_that_cost_nothing_still_finishes_as_done(env, live, monkeypatch):
    monkeypatch.setattr(settings, "serpapi_cost_per_search_usd", 0.0)
    live.usage = (0, 0)
    c = member(env, balance_usd=5)
    s = c.get(f"/searches/{run(c, 'free plan').json()['id']}").json()
    assert s["status"] == "done" and s["cost_usd"] == 0




def _admin(env):
    with env.scope() as db:
        _, secret = make_user(db, "christian@emailsandsms.com", admin=True)
    a = TestClient(app)
    assert login(env, a, "christian@emailsandsms.com", secret=secret).status_code == 200
    return a


def test_pitch_drafts_are_not_counted_as_customer_searches_and_are_in_spent(env, live, pitch):
    c = member(env, balance_usd=5)
    assert ask(c, job_id=pitch.job).status_code == 200            # one pitch draft, ZERO searches
    a = _admin(env)
    o = a.get("/admin/overview").json()["customer_searches_30d"]
    print(o)
    assert o["count"] == 0
    row = [u for u in a.get("/admin/users").json() if u["email"] == "member@example.com"][0]
    print(row["searches"], row["spent_usd"])
    assert row["spent_usd"] > 0                                    # they paid for a draft
