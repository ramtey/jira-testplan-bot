"""add source_provenance to runs

Revision ID: b8c9d0e1f2a3
Revises: d4e5f6a7b8c9
Create Date: 2026-09-10 00:00:00.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

# revision identifiers, used by Alembic.
revision: str = 'b8c9d0e1f2a3'
down_revision: Union[str, Sequence[str], None] = 'd4e5f6a7b8c9'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Record what each plan was derived from.

    The existing columns say how much context a run had (`pr_count`,
    `had_pr_diff`) but not *which* context, and never its state. That gap
    is why nobody could tell, reading plan 458, that three of its cases
    came from a PR closed unmerged the day before. One JSONB blob per run:
    every PR the run saw, its state at generation time, its head SHA, and
    whether that state allowed it to ground a case.

    Nullable with no backfill — runs generated before this column existed
    genuinely have no provenance, and inventing one would be the same
    class of error as the bug.
    """
    op.add_column(
        'runs',
        sa.Column('source_provenance', postgresql.JSONB(), nullable=True),
    )


def downgrade() -> None:
    """Downgrade schema."""
    op.drop_column('runs', 'source_provenance')
