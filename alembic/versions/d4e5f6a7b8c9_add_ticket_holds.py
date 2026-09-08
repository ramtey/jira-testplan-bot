"""add ticket_holds table

Revision ID: d4e5f6a7b8c9
Revises: a8b9c0d1e2f3
Create Date: 2026-09-08 00:00:00.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa

# revision identifiers, used by Alembic.
revision: str = 'd4e5f6a7b8c9'
down_revision: Union[str, Sequence[str], None] = 'a8b9c0d1e2f3'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Shared, per-ticket QA hold marker: "parked, and here's why".

    One row per held ticket, keyed by ticket_key and not by user, so the whole
    QA team sees the same hold. Resuming deletes the row, so the table only
    ever holds currently-parked tickets and created_at reads as "held since".
    """
    op.create_table(
        'ticket_holds',
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
        sa.Column('reason', sa.String(length=64), nullable=False),
        sa.Column('note', sa.Text(), nullable=True),
        sa.PrimaryKeyConstraint('id'),
    )
    op.create_index(
        'ix_ticket_holds_ticket_key',
        'ticket_holds',
        ['ticket_key'],
        unique=True,
    )


def downgrade() -> None:
    """Downgrade schema."""
    op.drop_index('ix_ticket_holds_ticket_key', table_name='ticket_holds')
    op.drop_table('ticket_holds')
