"""merge incidents and chat feedback heads

Revision ID: 04f7c3a56d53
Revises: a1e5c8f34d21, f3a1c7d24e80
Create Date: 2026-07-29 09:30:18.117258

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = '04f7c3a56d53'
down_revision: Union[str, Sequence[str], None] = ('a1e5c8f34d21', 'f3a1c7d24e80')
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Upgrade schema."""
    pass


def downgrade() -> None:
    """Downgrade schema."""
    pass
