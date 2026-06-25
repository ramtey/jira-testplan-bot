"""add test_plan_progress table

Revision ID: c3d4e5f6a7b8
Revises: b2c3d4e5f6a7
Create Date: 2026-06-25 00:00:00.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa

# revision identifiers, used by Alembic.
revision: str = 'c3d4e5f6a7b8'
down_revision: Union[str, Sequence[str], None] = 'b2c3d4e5f6a7'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Shared, per-ticket record of which test cases a QA team has checked off.

    Keyed by progress_key (ticket key(s) + a fingerprint of the plan's section
    sizes), so progress is shared across everyone testing the ticket and resets
    cleanly when a plan is regenerated into a different shape.
    """
    op.create_table(
        'test_plan_progress',
        sa.Column('id', sa.Integer(), nullable=False),
        sa.Column(
            'created_at',
            sa.DateTime(timezone=True),
            server_default=sa.text('now()'),
            nullable=False,
        ),
        sa.Column(
            'updated_at',
            sa.DateTime(timezone=True),
            server_default=sa.text('now()'),
            nullable=False,
        ),
        sa.Column('progress_key', sa.String(length=512), nullable=False),
        sa.Column('checked_ids', sa.Text(), server_default='[]', nullable=False),
        sa.PrimaryKeyConstraint('id'),
    )
    op.create_index(
        'ix_test_plan_progress_progress_key',
        'test_plan_progress',
        ['progress_key'],
        unique=True,
    )


def downgrade() -> None:
    """Downgrade schema."""
    op.drop_index('ix_test_plan_progress_progress_key', table_name='test_plan_progress')
    op.drop_table('test_plan_progress')
