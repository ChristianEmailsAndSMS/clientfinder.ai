"""initial schema: sources, scrape_runs, jobs

Revision ID: 0001
Revises:
Create Date: 2026-10-07
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = "0001"
down_revision: Union[str, None] = None
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "sources",
        sa.Column("id", sa.Integer, primary_key=True),
        sa.Column("key", sa.String(64), unique=True, nullable=False),
        sa.Column("kind", sa.String(32), nullable=False),
        sa.Column("display_name", sa.String(128), nullable=False),
        sa.Column("enabled", sa.Boolean, nullable=False, server_default=sa.true()),
        sa.Column("config", sa.JSON, nullable=False, server_default=sa.text("'{}'::json")),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
    )

    op.create_table(
        "scrape_runs",
        sa.Column("id", sa.Integer, primary_key=True),
        sa.Column("source_id", sa.Integer, sa.ForeignKey("sources.id", ondelete="CASCADE"), nullable=False, index=True),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.Column("finished_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("status", sa.String(16), nullable=False, server_default="running"),
        sa.Column("jobs_added", sa.Integer, nullable=False, server_default="0"),
        sa.Column("jobs_updated", sa.Integer, nullable=False, server_default="0"),
        sa.Column("error", sa.Text, nullable=True),
    )

    op.create_table(
        "jobs",
        sa.Column("id", sa.Integer, primary_key=True),
        sa.Column("dedupe_hash", sa.String(64), nullable=False, index=True),
        sa.Column("source_url", sa.Text, nullable=False),
        sa.Column("platform", sa.String(64), nullable=False, index=True),
        sa.Column("title", sa.String(512), nullable=False),
        sa.Column("company_or_poster", sa.String(256), nullable=True),
        sa.Column("raw_snippet", sa.Text, nullable=True),
        sa.Column("description", sa.Text, nullable=True),
        sa.Column("type", sa.String(32), nullable=True, index=True),
        sa.Column("pay_text", sa.String(128), nullable=True),
        sa.Column("pay_min", sa.Float, nullable=True),
        sa.Column("pay_max", sa.Float, nullable=True),
        sa.Column("pay_period", sa.String(16), nullable=True),
        sa.Column("experience_level", sa.String(32), nullable=True),
        sa.Column("location", sa.String(128), nullable=True),
        sa.Column("remote", sa.Boolean, nullable=True),
        sa.Column("skills", sa.JSON, nullable=True),
        sa.Column("posted_at", sa.DateTime(timezone=True), nullable=True, index=True),
        sa.Column("first_seen_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.Column("last_seen_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.Column("source_key", sa.String(64), nullable=False, index=True),
        sa.Column("is_real_job", sa.Boolean, nullable=False, server_default=sa.true()),
        sa.Column("extraction_model", sa.String(64), nullable=True),
        sa.Column("extra", sa.JSON, nullable=True),
        sa.UniqueConstraint("dedupe_hash", name="uq_jobs_dedupe_hash"),
    )


def downgrade() -> None:
    op.drop_table("jobs")
    op.drop_table("scrape_runs")
    op.drop_table("sources")
