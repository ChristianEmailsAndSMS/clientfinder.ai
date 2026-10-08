"""jobs.region, user onboarding/prefs, per-user unlimited credits + daily limit

Revision ID: 0007
Revises: 0006
Create Date: 2026-10-08
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = "0007"
down_revision: Union[str, None] = "0006"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column("jobs", sa.Column("region", sa.String(100), nullable=True))
    op.create_index("ix_jobs_region", "jobs", ["region"])
    op.add_column("users", sa.Column("onboarded_at", sa.DateTime(timezone=True), nullable=True))
    op.add_column("users", sa.Column("prefs", sa.JSON, nullable=True))
    op.add_column("users", sa.Column("unlimited_credits", sa.Boolean, nullable=False, server_default=sa.false()))
    op.add_column("users", sa.Column("daily_search_limit", sa.Integer, nullable=True))

    # Fill in the region for jobs we already have.
    from app import geo
    bind = op.get_bind()
    jobs = sa.table("jobs", sa.column("id", sa.Integer), sa.column("location", sa.String), sa.column("region", sa.String))
    for jid, loc in bind.execute(sa.select(jobs.c.id, jobs.c.location).where(jobs.c.location.is_not(None))).all():
        r = geo.classify(loc)
        if r:
            bind.execute(jobs.update().where(jobs.c.id == jid).values(region=r))


def downgrade() -> None:
    for c in ("daily_search_limit", "unlimited_credits", "prefs", "onboarded_at"):
        op.drop_column("users", c)
    op.drop_index("ix_jobs_region", table_name="jobs")
    op.drop_column("jobs", "region")
