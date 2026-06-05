"""add posted_to_jira fields to generated_plans

Revision ID: a1b2c3d4e5f6
Revises: 0ccd3713f19e
Create Date: 2026-06-04 00:00:00.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
import sqlmodel

# revision identifiers, used by Alembic.
revision: str = 'a1b2c3d4e5f6'
down_revision: Union[str, Sequence[str], None] = '0ccd3713f19e'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Track which plan version is currently live on the Jira ticket.

    Set when /jira/post-comment succeeds; cleared on prior versions of the
    same ticket because Jira posting is update-in-place (only one comment
    per ticket survives at a time).
    """
    op.add_column(
        'generated_plans',
        sa.Column(
            'jira_comment_id',
            sqlmodel.sql.sqltypes.AutoString(length=64),
            nullable=True,
        ),
    )
    op.add_column(
        'generated_plans',
        sa.Column(
            'posted_at',
            sa.DateTime(timezone=True),
            nullable=True,
        ),
    )


def downgrade() -> None:
    """Downgrade schema."""
    op.drop_column('generated_plans', 'posted_at')
    op.drop_column('generated_plans', 'jira_comment_id')
