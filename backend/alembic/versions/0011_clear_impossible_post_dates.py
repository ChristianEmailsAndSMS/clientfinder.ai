"""clear impossible posted_at values (epoch 0, 1969, future dates) so cards never show "691 months ago"

Revision ID: 0011
Revises: 0010
Create Date: 2026-10-13
"""
from datetime import datetime, timedelta, timezone
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = "0011"
down_revision: Union[str, None] = "0010"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # Same bounds as app/dates.py: nothing before 2015-01-01 and nothing more than a day ahead is a believable posting date.
    # The card then falls back to when we first saw the post.
    op.execute(sa.text("UPDATE jobs SET posted_at = NULL WHERE posted_at < :lo OR posted_at > :hi").bindparams(
        lo=datetime(2015, 1, 1, tzinfo=timezone.utc), hi=datetime.now(timezone.utc) + timedelta(days=1)))


def downgrade() -> None:
    pass                                              # the cleared values were wrong; there is nothing to restore
