"""users, credit_ledger, auth_events, setup_codes

Revision ID: 0004
Revises: 0003
Create Date: 2026-10-08
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = "0004"
down_revision: Union[str, None] = "0003"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "users",
        sa.Column("id", sa.Integer, primary_key=True),
        sa.Column("email", sa.String(320), nullable=False, unique=True),
        sa.Column("password_hash", sa.Text, nullable=False),
        sa.Column("is_admin", sa.Boolean, nullable=False, server_default=sa.false()),
        sa.Column("is_active", sa.Boolean, nullable=False, server_default=sa.true()),
        sa.Column("totp_secret_enc", sa.Text, nullable=True),
        sa.Column("totp_enabled", sa.Boolean, nullable=False, server_default=sa.false()),
        sa.Column("totp_last_step", sa.BigInteger, nullable=True),
        sa.Column("failed_logins", sa.Integer, nullable=False, server_default="0"),
        sa.Column("locked_until", sa.DateTime(timezone=True), nullable=True),
        sa.Column("session_version", sa.Integer, nullable=False, server_default="1"),
        sa.Column("balance_micro", sa.BigInteger, nullable=False, server_default="0"),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.Column("last_login_at", sa.DateTime(timezone=True), nullable=True),
    )
    op.create_table(
        "credit_ledger",
        sa.Column("id", sa.Integer, primary_key=True),
        sa.Column("user_id", sa.Integer, sa.ForeignKey("users.id", ondelete="RESTRICT"), nullable=False),
        sa.Column("kind", sa.String(16), nullable=False),
        sa.Column("delta_micro", sa.BigInteger, nullable=False),
        sa.Column("balance_after_micro", sa.BigInteger, nullable=False),
        sa.Column("reason", sa.String(256), nullable=False, server_default=""),
        sa.Column("ref", sa.String(128), nullable=True),
        sa.Column("actor_user_id", sa.Integer, sa.ForeignKey("users.id", ondelete="SET NULL"), nullable=True),
        sa.Column("meta", sa.JSON, nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.UniqueConstraint("ref", name="uq_credit_ledger_ref"),
    )
    op.create_index("ix_credit_ledger_user_created", "credit_ledger", ["user_id", "created_at"])
    op.create_table(
        "auth_events",
        sa.Column("id", sa.Integer, primary_key=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.Column("event", sa.String(32), nullable=False),
        sa.Column("email", sa.String(320), nullable=True),
        sa.Column("user_id", sa.Integer, nullable=True),
        sa.Column("ip", sa.String(64), nullable=True),
        sa.Column("user_agent", sa.String(200), nullable=True),
        sa.Column("detail", sa.String(300), nullable=True),
    )
    op.create_index("ix_auth_events_created_at", "auth_events", ["created_at"])
    op.create_index("ix_auth_events_ip_event_created", "auth_events", ["ip", "event", "created_at"])
    op.create_table(
        "setup_codes",
        sa.Column("id", sa.Integer, primary_key=True),
        sa.Column("email", sa.String(320), nullable=False),
        sa.Column("code_hash", sa.String(64), nullable=False),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("attempts", sa.Integer, nullable=False, server_default="0"),
        sa.Column("used_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
    )


def downgrade() -> None:
    op.drop_table("setup_codes")
    op.drop_index("ix_auth_events_ip_event_created", table_name="auth_events")
    op.drop_index("ix_auth_events_created_at", table_name="auth_events")
    op.drop_table("auth_events")
    op.drop_index("ix_credit_ledger_user_created", table_name="credit_ledger")
    op.drop_table("credit_ledger")
    op.drop_table("users")
