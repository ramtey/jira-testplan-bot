"""walkthrough screenshots become a list

Revision ID: e5f6a7b8c9d0
Revises: c3d4e5f6a7b8
Create Date: 2026-07-10 00:30:00.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa

# revision identifiers, used by Alembic.
revision: str = 'e5f6a7b8c9d0'
down_revision: Union[str, Sequence[str], None] = 'c3d4e5f6a7b8'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """A ticket walkthrough can carry multiple reference screenshots.

    Replace the single ``screenshot_url`` column with a ``screenshots`` JSON
    text column that stores a list of ``{"filename", "url"}`` entries.
    Existing single-screenshot rows are backfilled into a one-element list
    so no attachment metadata is lost; the original filename wasn't tracked
    separately in the prior schema, so we fall back to a generic label.
    """
    op.add_column(
        'ticket_walkthroughs',
        sa.Column('screenshots', sa.Text(), nullable=True),
    )
    op.execute(
        """
        UPDATE ticket_walkthroughs
        SET screenshots = json_build_array(
            json_build_object('filename', 'screenshot', 'url', screenshot_url)
        )::text
        WHERE screenshot_url IS NOT NULL AND screenshot_url <> ''
        """
    )
    op.drop_column('ticket_walkthroughs', 'screenshot_url')


def downgrade() -> None:
    op.add_column(
        'ticket_walkthroughs',
        sa.Column('screenshot_url', sa.String(length=1024), nullable=True),
    )
    op.execute(
        """
        UPDATE ticket_walkthroughs
        SET screenshot_url = (screenshots::json -> 0 ->> 'url')
        WHERE screenshots IS NOT NULL AND screenshots <> ''
        """
    )
    op.drop_column('ticket_walkthroughs', 'screenshots')
