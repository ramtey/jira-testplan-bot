"""add ticket_walkthroughs table

Revision ID: b2c3d4e5f6a7
Revises: a1b2c3d4e5f6
Create Date: 2026-06-24 00:00:00.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
import sqlmodel

# revision identifiers, used by Alembic.
revision: str = 'b2c3d4e5f6a7'
down_revision: Union[str, Sequence[str], None] = 'a1b2c3d4e5f6'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Human-authored "how to test this" content per ticket: a Loom link, a
    screenshot link, and free-text setup/repro notes.

    Keyed by ticket_key and stored apart from the generated plan body so that
    regenerating a plan never wipes the planner's walkthrough.
    """
    op.create_table(
        'ticket_walkthroughs',
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
        sa.Column('ticket_key', sa.String(length=64), nullable=False),
        sa.Column('loom_url', sqlmodel.sql.sqltypes.AutoString(length=1024), nullable=True),
        sa.Column('screenshot_url', sqlmodel.sql.sqltypes.AutoString(length=1024), nullable=True),
        sa.Column('notes', sa.Text(), nullable=True),
        sa.PrimaryKeyConstraint('id'),
    )
    op.create_index(
        'ix_ticket_walkthroughs_ticket_key',
        'ticket_walkthroughs',
        ['ticket_key'],
        unique=True,
    )


def downgrade() -> None:
    """Downgrade schema."""
    op.drop_index('ix_ticket_walkthroughs_ticket_key', table_name='ticket_walkthroughs')
    op.drop_table('ticket_walkthroughs')
