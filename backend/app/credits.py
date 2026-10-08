"""Credit ledger. Amounts are integers in micro-USD (1 credit-dollar = 1_000_000) so sub-cent LLM costs are exact.

Rules:
  * Every balance change is ONE atomic statement (UPDATE ... WHERE balance + delta >= 0 RETURNING) plus a ledger row
    in the same transaction, so concurrent requests can never overdraw a balance or lose an update.
  * `ref` (e.g. a payment id) makes a credit idempotent: replaying a webhook returns the original entry.
  * Usage is charged AFTER the model call (we have already paid Anthropic), so it may dip slightly below zero;
    `require_balance()` before the call is what stops a user with no credit from running anything.
  * Price charged = Claude API cost x CREDIT_MARKUP (app/pricing.py), rounded up."""
from __future__ import annotations

import math
from decimal import ROUND_HALF_UP, Decimal, InvalidOperation

from sqlalchemy import select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from .models import CreditEntry, User
from .pricing import charge_usd

MICRO = 1_000_000
MAX_USD = Decimal("1000000")       # hard ceiling for any single amount; stays far inside BIGINT
CREDIT_KINDS = {"topup", "grant", "refund"}
DEBIT_KINDS = {"usage", "revoke"}


class InsufficientCredits(Exception):
    def __init__(self, balance_micro: int, needed_micro: int):
        super().__init__(f"balance {balance_micro / MICRO:.4f} < needed {needed_micro / MICRO:.4f}")
        self.balance_micro, self.needed_micro = balance_micro, needed_micro


class UnknownUser(Exception):
    pass


def usd_to_micro(amount) -> int:
    """Exact conversion from a dollar amount (str/int/float/Decimal). Rejects NaN, infinities, more than 6 decimals."""
    try:
        d = Decimal(str(amount))
    except InvalidOperation:
        raise ValueError("not a number") from None
    if not d.is_finite():
        raise ValueError("not a finite number")
    if abs(d) > MAX_USD:
        raise ValueError("amount too large")
    micro = (d * MICRO)
    if micro != micro.to_integral_value():
        raise ValueError("at most 6 decimal places")
    return int(micro.quantize(Decimal(1), rounding=ROUND_HALF_UP))


def micro_to_usd(micro: int) -> float:
    return micro / MICRO


def balance(db: Session, user_id: int) -> int:
    b = db.scalar(select(User.balance_micro).where(User.id == user_id))
    if b is None:
        raise UnknownUser(user_id)
    return b


def apply(db: Session, user_id: int, delta_micro: int, kind: str, reason: str = "", *, ref: str | None = None,
          actor_user_id: int | None = None, allow_negative: bool = False, meta: dict | None = None) -> CreditEntry:
    """Apply one signed change atomically. Credits need delta > 0 and a credit kind; debits delta < 0 and a debit kind."""
    if delta_micro == 0 or (delta_micro > 0) != (kind in CREDIT_KINDS) or kind not in CREDIT_KINDS | DEBIT_KINDS:
        raise ValueError(f"invalid change: kind={kind} delta={delta_micro}")
    if ref:
        existing = db.scalar(select(CreditEntry).where(CreditEntry.ref == ref))
        if existing:
            return existing                                            # idempotent replay
    stmt = update(User).where(User.id == user_id).values(balance_micro=User.balance_micro + delta_micro)
    if delta_micro < 0 and not allow_negative:
        stmt = stmt.where(User.balance_micro + delta_micro >= 0)
    new_balance = db.execute(stmt.returning(User.balance_micro)).scalar()
    if new_balance is None:                                            # no row updated: unknown user or not enough credit
        current = db.scalar(select(User.balance_micro).where(User.id == user_id))
        if current is None:
            raise UnknownUser(user_id)
        raise InsufficientCredits(current, -delta_micro)
    entry = CreditEntry(user_id=user_id, kind=kind, delta_micro=delta_micro, balance_after_micro=new_balance,
                        reason=reason[:256], ref=ref, actor_user_id=actor_user_id, meta=meta)
    sp = db.begin_nested()
    try:
        db.add(entry)
        db.flush()
        sp.commit()
    except IntegrityError:                                             # lost a race on `ref`: undo and return the winner
        sp.rollback()
        db.execute(update(User).where(User.id == user_id).values(balance_micro=User.balance_micro - delta_micro))
        winner = db.scalar(select(CreditEntry).where(CreditEntry.ref == ref)) if ref else None
        if winner is None:
            raise
        return winner
    return entry


def require_balance(db: Session, user_id: int, needed_micro: int) -> None:
    b = balance(db, user_id)
    if b < needed_micro:
        raise InsufficientCredits(b, needed_micro)


def usage_cost_micro(model: str, input_tokens: int, output_tokens: int) -> int:
    """What a call costs the user, in micro-USD. Refuses unpriced models rather than guessing."""
    usd = charge_usd(model, input_tokens, output_tokens)
    if usd is None:
        raise ValueError(f"no price for model {model!r}; refusing to charge a guess")
    return math.ceil(usd * MICRO)


def charge_usage(db: Session, user_id: int, model: str, input_tokens: int, output_tokens: int, reason: str,
                 meta: dict | None = None) -> CreditEntry | None:
    cost = usage_cost_micro(model, input_tokens, output_tokens)
    if cost == 0:
        return None
    return apply(db, user_id, -cost, "usage", reason, allow_negative=True,
                 meta={"model": model, "input_tokens": input_tokens, "output_tokens": output_tokens, **(meta or {})})
