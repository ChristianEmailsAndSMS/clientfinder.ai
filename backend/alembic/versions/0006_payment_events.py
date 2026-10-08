"""payment_events

Revision ID: 0006
Revises: 0005
Create Date: 2026-10-08
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = "0006"
down_revision: Union[str, None] = "0005"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "payment_events",
        sa.Column("id", sa.Integer, primary_key=True),
        sa.Column("provider", sa.String(16), nullable=False, server_default="whop"),
        sa.Column("event_id", sa.String(128), nullable=False),
        sa.Column("event_type", sa.String(64), nullable=False, server_default=""),
        sa.Column("status", sa.String(12), nullable=False),
        sa.Column("note", sa.String(300), nullable=False, server_default=""),
        sa.Column("email", sa.String(320), nullable=True),
        sa.Column("amount_micro", sa.BigInteger, nullable=True),
        sa.Column("user_id", sa.Integer, sa.ForeignKey("users.id", ondelete="SET NULL"), nullable=True),
        sa.Column("payload", sa.JSON, nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.UniqueConstraint("provider", "event_id", name="uq_payment_events_event"),
    )


def downgrade() -> None:
    op.drop_table("payment_events")
