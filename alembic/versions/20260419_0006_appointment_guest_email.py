"""add appointment guest email

Revision ID: 20260419_0006
Revises: 20260325_0005
Create Date: 2026-04-19 15:35:00.000000
"""

from alembic import op
import sqlalchemy as sa
from sqlalchemy import inspect, text


# revision identifiers, used by Alembic.
revision = "20260419_0006"
down_revision = "20260325_0005"
branch_labels = None
depends_on = None


def upgrade() -> None:
    bind = op.get_bind()
    inspector = inspect(bind)
    table_names = set(inspector.get_table_names())
    if "appointments" not in table_names:
        return

    columns = {col["name"]: col for col in inspector.get_columns("appointments")}
    indexes = {index["name"] for index in inspector.get_indexes("appointments")}

    if "visitor_email" not in columns:
        op.add_column("appointments", sa.Column("visitor_email", sa.String(length=255), nullable=True))

    if "ix_appointments_visitor_email" not in indexes:
        op.execute(text("CREATE INDEX IF NOT EXISTS ix_appointments_visitor_email ON appointments (visitor_email)"))


def downgrade() -> None:
    bind = op.get_bind()
    inspector = inspect(bind)
    table_names = set(inspector.get_table_names())
    if "appointments" not in table_names:
        return

    columns = {col["name"] for col in inspector.get_columns("appointments")}
    indexes = {index["name"] for index in inspector.get_indexes("appointments")}

    if "ix_appointments_visitor_email" in indexes:
        op.drop_index("ix_appointments_visitor_email", table_name="appointments")

    if "visitor_email" in columns:
        op.drop_column("appointments", "visitor_email")
