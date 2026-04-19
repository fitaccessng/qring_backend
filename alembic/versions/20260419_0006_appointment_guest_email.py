"""add appointment guest email

Revision ID: 20260419_0006
Revises: 20260325_0005
Create Date: 2026-04-19 15:35:00.000000
"""

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision = "20260419_0006"
down_revision = "20260325_0005"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("appointments", sa.Column("visitor_email", sa.String(length=255), nullable=True))
    op.create_index("ix_appointments_visitor_email", "appointments", ["visitor_email"], unique=False)


def downgrade() -> None:
    op.drop_index("ix_appointments_visitor_email", table_name="appointments")
    op.drop_column("appointments", "visitor_email")
