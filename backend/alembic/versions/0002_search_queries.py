"""search_queries: log of Google-layer searches (scheduling, budget, yield)

Revision ID: 0002
Revises: 0001
Create Date: 2026-10-08
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = "0002"
down_revision: Union[str, None] = "0001"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "search_queries",
        sa.Column("id", sa.Integer, primary_key=True),
        sa.Column("query_key", sa.String(256), nullable=False),
        sa.Column("provider", sa.String(16), nullable=False),
        sa.Column("freshness", sa.String(4), nullable=True),
        sa.Column("ran_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.Column("results", sa.Integer, nullable=False, server_default="0"),
        sa.Column("new_jobs", sa.Integer, nullable=False, server_default="0"),
        sa.Column("error", sa.Text, nullable=True),
    )
    op.create_index("ix_search_queries_key_ran", "search_queries", ["query_key", "ran_at"])


def downgrade() -> None:
    op.drop_index("ix_search_queries_key_ran", table_name="search_queries")
    op.drop_table("search_queries")
