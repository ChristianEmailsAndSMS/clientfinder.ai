"""user_searches and user_search_jobs

Revision ID: 0005
Revises: 0004
Create Date: 2026-10-08
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = "0005"
down_revision: Union[str, None] = "0004"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "user_searches",
        sa.Column("id", sa.Integer, primary_key=True),
        sa.Column("user_id", sa.Integer, sa.ForeignKey("users.id", ondelete="CASCADE"), nullable=False),
        sa.Column("query", sa.String(200), nullable=False),
        sa.Column("query_key", sa.String(300), nullable=False),
        sa.Column("freshness", sa.String(4), nullable=False, server_default="w"),
        sa.Column("site", sa.String(40), nullable=True),
        sa.Column("status", sa.String(10), nullable=False, server_default="queued"),
        sa.Column("cached", sa.Boolean, nullable=False, server_default=sa.false()),
        sa.Column("results_count", sa.Integer, nullable=False, server_default="0"),
        sa.Column("new_jobs", sa.Integer, nullable=False, server_default="0"),
        sa.Column("cost_micro", sa.BigInteger, nullable=False, server_default="0"),
        sa.Column("error", sa.Text, nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.Column("finished_at", sa.DateTime(timezone=True), nullable=True),
    )
    op.create_index("ix_user_searches_user_created", "user_searches", ["user_id", "created_at"])
    op.create_index("ix_user_searches_key_finished", "user_searches", ["query_key", "finished_at"])
    op.create_table(
        "user_search_jobs",
        sa.Column("search_id", sa.Integer, sa.ForeignKey("user_searches.id", ondelete="CASCADE"), primary_key=True),
        sa.Column("job_id", sa.Integer, sa.ForeignKey("jobs.id", ondelete="CASCADE"), primary_key=True),
    )


def downgrade() -> None:
    op.drop_table("user_search_jobs")
    op.drop_index("ix_user_searches_key_finished", table_name="user_searches")
    op.drop_index("ix_user_searches_user_created", table_name="user_searches")
    op.drop_table("user_searches")
