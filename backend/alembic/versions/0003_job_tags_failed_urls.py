"""job_tags and failed_urls

Revision ID: 0003
Revises: 0002
Create Date: 2026-10-08
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = "0003"
down_revision: Union[str, None] = "0002"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "job_tags",
        sa.Column("job_id", sa.Integer, sa.ForeignKey("jobs.id", ondelete="CASCADE"), primary_key=True),
        sa.Column("tag", sa.String(48), primary_key=True),
    )
    op.create_index("ix_job_tags_tag", "job_tags", ["tag"])

    op.create_table(
        "failed_urls",
        sa.Column("id", sa.Integer, primary_key=True),
        sa.Column("url_hash", sa.String(64), nullable=False, unique=True),
        sa.Column("url", sa.Text, nullable=False),
        sa.Column("title", sa.String(512), nullable=False, server_default=""),
        sa.Column("snippet", sa.Text, nullable=False, server_default=""),
        sa.Column("platform", sa.String(64), nullable=False, server_default="web"),
        sa.Column("source_key", sa.String(64), nullable=False),
        sa.Column("source_query", sa.String(256), nullable=False, server_default=""),
        sa.Column("error", sa.Text, nullable=True),
        sa.Column("attempts", sa.Integer, nullable=False, server_default="1"),
        sa.Column("status", sa.String(12), nullable=False, server_default="pending"),
        sa.Column("first_failed_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.Column("last_failed_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
    )
    op.create_index("ix_failed_urls_status", "failed_urls", ["status", "last_failed_at"])


def downgrade() -> None:
    op.drop_index("ix_failed_urls_status", table_name="failed_urls")
    op.drop_table("failed_urls")
    op.drop_index("ix_job_tags_tag", table_name="job_tags")
    op.drop_table("job_tags")
