"""Whop payment webhook: verify the signature, find the buyer, add the credits exactly once.

Whop signs webhooks with the Standard Webhooks scheme (headers webhook-id / webhook-timestamp / webhook-signature, HMAC-SHA256 of
"{id}.{timestamp}.{raw body}"). The payload field names below are NOT confirmed against a real delivery, so every accepted event is
stored in `payment_events`, an unrecognised shape is held for review instead of guessed at, and a payment is only credited when the
event type, currency, amount and buyer email are all clear. Make the first real payment a small test and check /admin/payments."""
from __future__ import annotations

import base64
import binascii
import hashlib
import hmac
import time
from decimal import Decimal, InvalidOperation

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from . import credits
from .config import settings
from .models import PaymentEvent, User

TOLERANCE_SECONDS = 300
MAX_BODY = 256 * 1024
CREDIT_TYPES = {"payment.succeeded"}


def _keys(secret: str) -> list[bytes]:
    """The signing key is documented inconsistently (raw string vs base64 after a prefix), so accept either reading."""
    secret = secret.strip()
    keys = [secret.encode()]
    tail = secret.split("_", 1)[1] if "_" in secret else secret
    try:
        keys.append(base64.b64decode(tail, validate=True))
    except (binascii.Error, ValueError):
        pass
    return [k for k in keys if k]


def verify(secret: str, msg_id: str, timestamp: str, signature_header: str, body: bytes, now: float | None = None) -> bool:
    if not (secret and msg_id and timestamp and signature_header):
        return False
    try:
        ts = int(timestamp)
    except ValueError:
        return False
    if abs((now if now is not None else time.time()) - ts) > TOLERANCE_SECONDS:
        return False
    signed = f"{msg_id}.{timestamp}.".encode() + body
    given = [p.split(",", 1)[1] for p in signature_header.split() if p.startswith("v1,") and len(p) > 3]
    for key in _keys(secret):
        expected = base64.b64encode(hmac.new(key, signed, hashlib.sha256).digest()).decode()
        if any(hmac.compare_digest(expected, g) for g in given):
            return True
    return False


def _dig(d, *path):
    for p in path:
        if not isinstance(d, dict):
            return None
        d = d.get(p)
    return d


def extract_email(data) -> str | None:
    for path in (("user", "email"), ("member", "user", "email"), ("member", "email"), ("customer", "email"), ("email",),
                 ("billing_address", "email"), ("metadata", "email")):
        v = _dig(data, *path)
        if isinstance(v, str) and "@" in v and len(v) <= 320:
            return v.strip().lower()
    return None


def extract_user_id(data) -> int | None:
    """The account id our own server put in the checkout's metadata (signed webhook, so a buyer cannot forge it)."""
    for path in (("metadata", "cf_user_id"), ("checkout_configuration", "metadata", "cf_user_id"), ("plan", "metadata", "cf_user_id")):
        v = _dig(data, *path)
        if isinstance(v, (str, int)) and str(v).isdigit() and len(str(v)) <= 9:
            return int(v)
    return None


def extract_amount_micro(data) -> tuple[int | None, str]:
    """The amount actually paid, in micro-USD, or (None, why). Uses the first field present, never sums or guesses between several."""
    cur = next((str(_dig(data, k)).lower() for k in ("currency",) if isinstance(_dig(data, k), str)), None)
    if cur and cur != "usd":
        return None, f"currency is {cur}, not usd"
    for field in ("usd_total", "final_amount", "total", "amount_after_fees", "amount"):
        v = _dig(data, field)
        if v is None or isinstance(v, bool):
            continue
        try:
            d = Decimal(str(v))
        except InvalidOperation:
            return None, f"{field} is not a number"
        if not d.is_finite() or d <= 0:
            return None, f"{field} is not a positive amount"
        if settings.whop_amount_unit == "cents":
            d = d / 100
        if cur is None and field not in ("usd_total",):
            return None, "no currency in the payload"
        return int((d * credits.MICRO).to_integral_value()), field
    return None, "no amount field found"


def process(db: Session, event_id: str, body: dict) -> PaymentEvent:
    """Idempotent: a retried delivery returns the stored result. Never raises for a payload we cannot use; it records why."""
    existing = db.scalar(select(PaymentEvent).where(PaymentEvent.provider == "whop", PaymentEvent.event_id == event_id))
    if existing:
        return existing
    etype = str(body.get("type") or body.get("action") or "").lower().replace("_", ".")[:64]
    data = body.get("data") if isinstance(body.get("data"), dict) else {}
    ev = PaymentEvent(provider="whop", event_id=event_id[:128], event_type=etype, status="ignored", payload=body)
    if etype in CREDIT_TYPES:
        ev.email = extract_email(data)
        amount, why = extract_amount_micro(data)
        ev.amount_micro = amount
        uid = extract_user_id(data)
        user = db.get(User, uid) if uid else None
        if user is None and ev.email:
            user = db.scalar(select(User).where(User.email == ev.email))
        ev.email = ev.email or (user.email if user else None)
        if amount is None:
            ev.status, ev.note = "review", why
        elif amount > credits.usd_to_micro(settings.whop_max_topup_usd):
            ev.status, ev.note = "review", f"over the ${settings.whop_max_topup_usd:,.0f} auto-credit limit"
        elif not user or not user.is_active:
            ev.status, ev.note = "unmatched", "no account matches this payment (no account id or email we recognise)"
        else:
            ev.status, ev.user_id = "credited", user.id
    elif any(w in etype for w in ("refund", "dispute", "chargeback")):
        ev.status = "review"                                   # money went back: the owner decides whether to take credit back
        ev.note = "refund or dispute: check this customer's balance and revoke credit if needed"
        ev.email = extract_email(data)
        amount, _ = extract_amount_micro(data)
        ev.amount_micro = amount
    elif etype:
        ev.note = "not a payment event we credit"
    else:
        ev.note = "no event type"
    db.add(ev)
    try:
        db.flush()
    except IntegrityError:                                   # two deliveries of the same event raced
        db.rollback()
        return db.scalar(select(PaymentEvent).where(PaymentEvent.provider == "whop", PaymentEvent.event_id == event_id[:128]))
    if ev.status == "credited":
        credits.apply(db, ev.user_id, ev.amount_micro, "topup", "Whop payment", ref=f"whop:{ev.event_id}",
                      meta={"payment_event_id": ev.id, "email": ev.email})
    return ev


def resolve(db: Session, ev: PaymentEvent, user_id: int, actor_id: int) -> PaymentEvent:
    """Owner decision for a held payment: credit it to this account (only once, only if it carried a usable amount)."""
    if ev.status not in ("unmatched", "review") or not ev.amount_micro or ev.amount_micro <= 0:
        raise ValueError("this payment cannot be credited")
    credits.apply(db, user_id, ev.amount_micro, "topup", "Whop payment (resolved by owner)", ref=f"whop:{ev.event_id}",
                  actor_user_id=actor_id, meta={"payment_event_id": ev.id, "email": ev.email})
    ev.status, ev.user_id, ev.note = "credited", user_id, "resolved by owner"
    return ev
