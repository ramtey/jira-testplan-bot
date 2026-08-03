"""add auto_bug_analysis_dispatched_at to jira_tickets

Revision ID: a8b9c0d1e2f3
Revises: f7a1c2d3e4b5
Create Date: 2026-08-03 12:00:00.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = 'a8b9c0d1e2f3'
down_revision: Union[str, Sequence[str], None] = 'f7a1c2d3e4b5'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column(
        'jira_tickets',
        sa.Column(
            'auto_bug_analysis_dispatched_at',
            sa.DateTime(timezone=True),
            nullable=True,
        ),
    )


def downgrade() -> None:
    op.drop_column('jira_tickets', 'auto_bug_analysis_dispatched_at')
