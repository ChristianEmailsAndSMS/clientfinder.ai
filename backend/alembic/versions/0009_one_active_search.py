"""one queued/running search per user, enforced by a partial unique index

Revision ID: 0009
Revises: 0008
Create Date: 2026-10-12
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = "0009"
down_revision: Union[str, None] = "0008"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # Any user who already has several active searches keeps only the newest; the rest are closed so the index can be built.
    op.execute(sa.text(
        "UPDATE user_searches SET status = 'failed', error = 'Superseded. You were not charged.' "
        "WHERE status IN ('queued', 'running') AND id NOT IN "
        "(SELECT MAX(id) FROM user_searches WHERE status IN ('queued', 'running') GROUP BY user_id)"))
    op.create_index("uq_user_searches_one_active", "user_searches", ["user_id"], unique=True,
                    postgresql_where=sa.text("status IN ('queued', 'running')"),
                    sqlite_where=sa.text("status IN ('queued', 'running')"))


def downgrade() -> None:
    op.drop_index("uq_user_searches_one_active", table_name="user_searches")
