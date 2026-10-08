"""Accounts, credits and the security audit trail."""
from datetime import datetime, timezone

from sqlalchemy import JSON, BigInteger, Boolean, DateTime, ForeignKey, Index, Integer, String, Text, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column

from ..db import Base


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


class User(Base):
    __tablename__ = "users"

    id: Mapped[int] = mapped_column(primary_key=True)
    email: Mapped[str] = mapped_column(String(320), unique=True)          # always stored lowercase
    password_hash: Mapped[str] = mapped_column(Text)                      # argon2id
    is_admin: Mapped[bool] = mapped_column(Boolean, default=False)
    is_active: Mapped[bool] = mapped_column(Boolean, default=True)
    totp_secret_enc: Mapped[str | None] = mapped_column(Text, nullable=True)   # Fernet-encrypted base32 secret
    totp_enabled: Mapped[bool] = mapped_column(Boolean, default=False)
    totp_last_step: Mapped[int | None] = mapped_column(BigInteger, nullable=True)   # replay protection
    failed_logins: Mapped[int] = mapped_column(Integer, default=0)
    locked_until: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    session_version: Mapped[int] = mapped_column(Integer, default=1)      # bump = log out everywhere
    balance_micro: Mapped[int] = mapped_column(BigInteger, default=0)     # credits in micro-USD (1e-6 USD)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_utcnow)
    last_login_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)


class CreditEntry(Base):
    """Append-only credit ledger. users.balance_micro is the running total; every change has a row here."""
    __tablename__ = "credit_ledger"
    __table_args__ = (Index("ix_credit_ledger_user_created", "user_id", "created_at"),
                      UniqueConstraint("ref", name="uq_credit_ledger_ref"))

    id: Mapped[int] = mapped_column(primary_key=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id", ondelete="RESTRICT"))
    kind: Mapped[str] = mapped_column(String(16))                         # topup | usage | grant | revoke | refund
    delta_micro: Mapped[int] = mapped_column(BigInteger)
    balance_after_micro: Mapped[int] = mapped_column(BigInteger)
    reason: Mapped[str] = mapped_column(String(256), default="")
    ref: Mapped[str | None] = mapped_column(String(128), nullable=True)   # external id (payment id): makes retries idempotent
    actor_user_id: Mapped[int | None] = mapped_column(ForeignKey("users.id", ondelete="SET NULL"), nullable=True)
    meta: Mapped[dict | None] = mapped_column(JSON, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_utcnow)


class AuthEvent(Base):
    """Security audit log; also the source for IP-based login throttling."""
    __tablename__ = "auth_events"
    __table_args__ = (Index("ix_auth_events_ip_event_created", "ip", "event", "created_at"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_utcnow, index=True)
    event: Mapped[str] = mapped_column(String(32))
    email: Mapped[str | None] = mapped_column(String(320), nullable=True)
    user_id: Mapped[int | None] = mapped_column(Integer, nullable=True)
    ip: Mapped[str | None] = mapped_column(String(64), nullable=True)
    user_agent: Mapped[str | None] = mapped_column(String(200), nullable=True)
    detail: Mapped[str | None] = mapped_column(String(300), nullable=True)


class SetupCode(Base):
    """One-time code, printed on the server console, that authorises creating an admin account in the browser."""
    __tablename__ = "setup_codes"

    id: Mapped[int] = mapped_column(primary_key=True)
    email: Mapped[str] = mapped_column(String(320))
    code_hash: Mapped[str] = mapped_column(String(64))                    # sha256 hex; the code is high-entropy
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    attempts: Mapped[int] = mapped_column(Integer, default=0)
    used_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_utcnow)


class UserSearch(Base):
    """A live search a customer ran (or got served from cache). Costs credits unless `cached`."""
    __tablename__ = "user_searches"
    __table_args__ = (Index("ix_user_searches_user_created", "user_id", "created_at"),
                      Index("ix_user_searches_key_finished", "query_key", "finished_at"))

    id: Mapped[int] = mapped_column(primary_key=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"))
    query: Mapped[str] = mapped_column(String(200))                     # as the user typed it (cleaned)
    query_key: Mapped[str] = mapped_column(String(300))                  # normalised: cache identity
    freshness: Mapped[str] = mapped_column(String(4), default="w")      # d | w | m
    site: Mapped[str | None] = mapped_column(String(40), nullable=True)
    status: Mapped[str] = mapped_column(String(10), default="queued")   # queued | running | done | failed
    cached: Mapped[bool] = mapped_column(Boolean, default=False)
    results_count: Mapped[int] = mapped_column(Integer, default=0)
    new_jobs: Mapped[int] = mapped_column(Integer, default=0)
    cost_micro: Mapped[int] = mapped_column(BigInteger, default=0)      # what the user was charged
    error: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_utcnow)
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)


class UserSearchJob(Base):
    __tablename__ = "user_search_jobs"

    search_id: Mapped[int] = mapped_column(ForeignKey("user_searches.id", ondelete="CASCADE"), primary_key=True)
    job_id: Mapped[int] = mapped_column(ForeignKey("jobs.id", ondelete="CASCADE"), primary_key=True)


class PaymentEvent(Base):
    """Every payment webhook we accept, so nothing is lost and the owner can resolve anything that could not be matched."""
    __tablename__ = "payment_events"
    __table_args__ = (UniqueConstraint("provider", "event_id", name="uq_payment_events_event"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    provider: Mapped[str] = mapped_column(String(16), default="whop")
    event_id: Mapped[str] = mapped_column(String(128))                    # the webhook-id header: retries carry the same one
    event_type: Mapped[str] = mapped_column(String(64), default="")
    status: Mapped[str] = mapped_column(String(12))                       # credited | ignored | unmatched | review
    note: Mapped[str] = mapped_column(String(300), default="")
    email: Mapped[str | None] = mapped_column(String(320), nullable=True)
    amount_micro: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    user_id: Mapped[int | None] = mapped_column(ForeignKey("users.id", ondelete="SET NULL"), nullable=True)
    payload: Mapped[dict | None] = mapped_column(JSON, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_utcnow)
