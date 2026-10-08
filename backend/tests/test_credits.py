"""Credit ledger: atomic changes, idempotency, invariants, and pricing refusal."""
import pytest
from sqlalchemy import create_engine, func, select
from sqlalchemy.orm import sessionmaker

from app import credits
from app.credits import InsufficientCredits, UnknownUser, apply, charge_usage, require_balance, usage_cost_micro, usd_to_micro
from app.db import Base
from app.models import CreditEntry, User

MODEL = "claude-haiku-4-5-20251001"


@pytest.fixture
def db():
    engine = create_engine("sqlite://")
    Base.metadata.create_all(engine)
    s = sessionmaker(bind=engine)()
    s.add(User(email="u@example.com", password_hash="x"))
    s.commit()
    yield s
    s.close()


def uid(db):
    return db.scalar(select(User.id))


def invariant(db):
    """balance == sum of ledger deltas, and each row's balance_after follows from the previous one."""
    total = db.scalar(select(func.coalesce(func.sum(CreditEntry.delta_micro), 0)))
    assert db.scalar(select(User.balance_micro)) == total
    running = 0
    for e in db.scalars(select(CreditEntry).order_by(CreditEntry.id)):
        running += e.delta_micro
        assert e.balance_after_micro == running


def test_grant_then_debit_keeps_ledger_consistent(db):
    u = uid(db)
    apply(db, u, 5_000_000, "grant", "bonus")
    apply(db, u, -1_250_000, "revoke", "correction")
    assert credits.balance(db, u) == 3_750_000
    invariant(db)


def test_overdraw_is_refused_and_changes_nothing(db):
    u = uid(db)
    apply(db, u, 1_000_000, "topup", "paid", ref="pay_1")
    with pytest.raises(InsufficientCredits) as ei:
        apply(db, u, -1_000_001, "revoke", "too much")
    assert ei.value.balance_micro == 1_000_000
    assert credits.balance(db, u) == 1_000_000 and db.scalar(select(func.count(CreditEntry.id))) == 1
    apply(db, u, -1_000_000, "revoke", "exact")                      # exactly the balance is fine
    assert credits.balance(db, u) == 0
    invariant(db)


def test_same_ref_applies_once(db):
    u = uid(db)
    first = apply(db, u, 10_000_000, "topup", "whop payment", ref="pay_abc")
    again = apply(db, u, 10_000_000, "topup", "whop payment (webhook retry)", ref="pay_abc")
    assert again.id == first.id
    assert credits.balance(db, u) == 10_000_000 and db.scalar(select(func.count(CreditEntry.id))) == 1


def test_refs_are_global_so_a_replay_for_another_user_cannot_double_pay(db):
    other = User(email="v@example.com", password_hash="x")
    db.add(other)
    db.flush()
    apply(db, uid(db), 1_000_000, "topup", "p", ref="pay_x")
    again = apply(db, other.id, 1_000_000, "topup", "p", ref="pay_x")
    assert again.user_id == uid(db) and credits.balance(db, other.id) == 0


@pytest.mark.parametrize("kind,delta", [("grant", -5), ("topup", 0), ("usage", 5), ("revoke", 5), ("bogus", 5), ("refund", -1)])
def test_kind_and_sign_must_agree(db, kind, delta):
    with pytest.raises(ValueError):
        apply(db, uid(db), delta, kind, "x")
    assert db.scalar(select(func.count(CreditEntry.id))) == 0


def test_unknown_user(db):
    with pytest.raises(UnknownUser):
        apply(db, 9999, 1_000, "grant", "x")
    with pytest.raises(UnknownUser):
        credits.balance(db, 9999)


def test_usage_is_charged_at_cost_times_markup_rounded_up(db):
    u = uid(db)
    apply(db, u, 1_000_000, "topup", "paid", ref="p1")
    e = charge_usage(db, u, MODEL, 2000, 400, "outreach draft")
    assert e.delta_micro == -6000                                     # $0.004 cost * 1.5 = $0.006
    assert e.meta == {"model": MODEL, "input_tokens": 2000, "output_tokens": 400}
    assert credits.balance(db, u) == 994_000
    invariant(db)


def test_usage_may_go_slightly_negative_because_we_already_paid_anthropic(db):
    u = uid(db)
    apply(db, u, 1_000, "topup", "tiny", ref="p2")
    charge_usage(db, u, MODEL, 200_000, 20_000, "big call")
    assert credits.balance(db, u) < 0
    with pytest.raises(InsufficientCredits):                          # but the next call is stopped at the door
        require_balance(db, u, 1)
    invariant(db)


def test_unpriced_model_is_refused_not_guessed(db):
    with pytest.raises(ValueError):
        usage_cost_micro("mystery-model", 1000, 1000)
    with pytest.raises(ValueError):
        charge_usage(db, uid(db), "mystery-model", 1000, 1000, "x")
    assert db.scalar(select(func.count(CreditEntry.id))) == 0


def test_require_balance_passes_when_enough(db):
    apply(db, uid(db), 5_000, "grant", "x")
    require_balance(db, uid(db), 5_000)
    with pytest.raises(InsufficientCredits):
        require_balance(db, uid(db), 5_001)


def test_many_small_operations_never_drift(db):
    u = uid(db)
    apply(db, u, 3_000_000, "grant", "seed")
    for i in range(50):
        charge_usage(db, u, MODEL, 1000 + i, 100, f"call {i}")
    invariant(db)


@pytest.mark.parametrize("raw,micro", [("1", 1_000_000), ("0.000001", 1), (" 2.5 ", 2_500_000), (3, 3_000_000), (0.1, 100_000)])
def test_usd_to_micro_exact(raw, micro):
    assert usd_to_micro(raw) == micro
