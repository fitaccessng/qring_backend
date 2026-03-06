"""schema baseline

Revision ID: 20260306_0002
Revises: 20260212_0001
Create Date: 2026-03-06
"""

from alembic import op

from app.db.base import Base
from app.db import models as _models  # noqa: F401

# revision identifiers, used by Alembic.
revision = "20260306_0002"
down_revision = "20260212_0001"
branch_labels = None
depends_on = None


def upgrade() -> None:
    bind = op.get_bind()
    Base.metadata.create_all(bind=bind, checkfirst=True)


def downgrade() -> None:
    # Non-destructive baseline migration.
    pass

