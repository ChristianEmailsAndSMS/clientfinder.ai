"""Money paths pinned at the edges (found by mutation testing: each of these survived a one-line change before):
fractional-micro rounding, the 100K-token price boundary, the lost-race branch of credits.apply, the live-search budget and balance
checks, refusal billing, and the Whop account-id limit."""
import math
from decimal import Decimal

import pytest
from sqlalchemy import create_engine, func, select
from sqlalchemy.orm import sessionmaker

from app import credits, live_search, pricing, whop
from app.config import settings
from app.db import Base
from app.models import CreditEntry, User
from app.scrapers import query_plan
from test_customer_api import CSRF, ask, balance, env, live, member, pitch  # noqa: F401  (fixtures)

HAIKU5 = "claude-haiku-5-5"        # 0.10 / 0.50 USD per 1M tokens, so 1 input token = 0.1 micro-USD


# ---------- pricing ----------
def test_charge_rounds_up_never_down_on_fractional_micro_costs():
    assert pricing.charge_micro(HAIKU5, 1, 0) == 1                         # 0.1 x 1.5 = 0.15 must charge 1, not 0
    assert pricing.charge_micro(HAIKU5, 23, 0) == 4                        # 2.3 x 1.5 = 3.45 -> 4 (banker's rounding would give 3)
    for i in range(0, 60):
        for o in (0, 1, 3):
            exact = pricing.cost_micro_exact(HAIKU5, i, o)
            assert pricing.charge_micro(HAIKU5, i, o) == math.ceil(exact * Decimal("1.5"))


def test_our_cost_is_rounded_up_so_it_never_understates_what_we_spent():
    assert pricing.cost_micro(HAIKU5, 1, 0) == 1                           # 0.1 micro-USD spent, recorded as 1, not 0
    assert pricing.cost_micro(HAIKU5, 23, 0) == 3
    assert pricing.cost_micro(HAIKU5, 0, 1) == 1


def test_long_prompt_rate_starts_strictly_above_100k_input_tokens():
    assert pricing.rates(HAIKU5, 100_000) == (Decimal("0.1"), Decimal("0.5"))
    assert pricing.rates(HAIKU5, 100_001) == (Decimal("0.5"), Decimal("2.5"))


def test_search_fee_rounds_up(monkeypatch):
    monkeypatch.setattr(settings, "serpapi_cost_per_search_usd", 0.0000003)         # 0.3 micro x 1.5 = 0.45 -> 1
    assert live_search.search_fee_micro() == 1


# ---------- credits ----------
@pytest.fixture
def db():
    engine = create_engine("sqlite://")
    Base.metadata.create_all(engine)
    s = sessionmaker(bind=engine)()
    s.add(User(email="u@example.com", password_hash="x"))
    s.commit()
    yield s
    s.close()


def test_a_request_that_loses_the_ref_race_does_not_keep_its_credit(db, monkeypatch):
    uid = db.scalar(select(User.id))
    winner = credits.apply(db, uid, 1_000_000, "topup", "paid", ref="pay_race")
    real = db.scalar
    calls = []

    def blind_first(*a, **kw):                          # the idempotency pre-check does not see the other session's row yet
        calls.append(1)
        return None if len(calls) == 1 else real(*a, **kw)

    monkeypatch.setattr(db, "scalar", blind_first)
    again = credits.apply(db, uid, 1_000_000, "topup", "paid", ref="pay_race")
    monkeypatch.undo()
    assert again.id == winner.id
    assert credits.balance(db, uid) == 1_000_000
    assert db.scalar(select(func.count(CreditEntry.id))) == 1


def test_zero_value_ledger_rows_are_refused_for_every_kind(db):
    uid = db.scalar(select(User.id))
    for kind in ("usage", "revoke", "grant", "topup", "refund"):
        with pytest.raises(ValueError):
            credits.apply(db, uid, 0, kind, "nothing")
    assert db.scalar(select(func.count(CreditEntry.id))) == 0


def test_amount_parsing_rejects_a_seventh_decimal_and_huge_values_of_either_sign():
    assert credits.usd_to_micro("0.000001") == 1
    for bad in ("0.0000005", "0.0000014", "-0.0000005", "1000000.01", "-1000000.01", "nan", "inf"):
        with pytest.raises(ValueError):
            credits.usd_to_micro(bad)
    assert credits.usd_to_micro("1000000") == 1_000_000 * credits.MICRO


# ---------- live search ----------
def test_live_search_budget_cannot_be_overshot_and_may_be_used_exactly(env, live, monkeypatch):  # noqa: F811
    n = len(live_search.search_targets("hiring copywriter", None))
    monkeypatch.setattr(settings, "serpapi_monthly_budget", 1000)
    for used, ok in ((1000 - n, True), (1000 - n + 1, False)):
        monkeypatch.setattr(query_plan, "searches_used_this_month", lambda db, now=None, u=used: u)
        c = member(env, f"b{used}@example.com", balance_usd=50)                 # a fresh customer each time: no search already running
        with env.scope() as db:
            user = db.get(User, c.uid)
            if ok:
                assert live_search.start_search(db, user, "hiring copywriter", "w", None).status == "queued"
            else:
                with pytest.raises(live_search.SearchRefused) as ei:
                    live_search.start_search(db, user, "hiring copywriter", "w", None)
                assert ei.value.status == 503


def test_live_search_needs_the_full_quoted_price_in_the_balance(env, live):  # noqa: F811
    need = live_search.price_for(None)
    short = member(env, "short@example.com", balance_usd=0)
    exact = member(env, "exact@example.com", balance_usd=0)
    with env.scope() as db:
        db.get(User, short.uid).balance_micro = need - 1
        db.get(User, exact.uid).balance_micro = need
    with env.scope() as db:
        with pytest.raises(live_search.SearchRefused) as ei:
            live_search.start_search(db, db.get(User, short.uid), "hiring copywriter", "w", None)
        assert ei.value.status == 402
    with env.scope() as db:
        assert live_search.start_search(db, db.get(User, exact.uid), "hiring copywriter", "w", None).status == "queued"


# ---------- Pitch help ----------
def test_a_refusal_with_partial_text_is_not_billed(env, pitch):  # noqa: F811
    c = member(env, balance_usd=1)
    start = balance(env, c.uid)
    pitch.stop, pitch.text = "refusal", "I can help with part of that, but"
    r = ask(c, job_id=pitch.job)
    assert r.status_code == 422 and balance(env, c.uid) == start
    with env.scope() as db:
        assert db.scalar(select(func.count(CreditEntry.id)).where(CreditEntry.kind == "usage")) == 0


# ---------- Whop ----------
def test_whop_account_id_must_be_a_plain_number_that_fits_our_column():
    ok = lambda v: whop.extract_user_id({"metadata": {"cf_user_id": v}})
    assert ok("123456789") == 123456789 and ok(42) == 42
    assert ok("1234567890") is None and ok(10 ** 20) is None             # would be an integer-out-of-range error on PostgreSQL
    assert ok(True) is None and ok("-5") is None and ok("1.5") is None
