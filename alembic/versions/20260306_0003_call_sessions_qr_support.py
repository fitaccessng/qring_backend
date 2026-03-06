"""allow call sessions without appointment

Revision ID: 20260306_0003
Revises: 20260306_0002
Create Date: 2026-03-06
"""

from alembic import op
import sqlalchemy as sa
from sqlalchemy import inspect, text

# revision identifiers, used by Alembic.
revision = "20260306_0003"
down_revision = "20260306_0002"
branch_labels = None
depends_on = None


def upgrade() -> None:
    bind = op.get_bind()
    inspector = inspect(bind)
    table_names = set(inspector.get_table_names())
    if "call_sessions" not in table_names:
        return

    columns = {col["name"]: col for col in inspector.get_columns("call_sessions")}

    if "visitor_session_id" not in columns:
        op.add_column("call_sessions", sa.Column("visitor_session_id", sa.String(length=36), nullable=True))
        op.execute(text("CREATE INDEX IF NOT EXISTS ix_call_sessions_visitor_session_id ON call_sessions (visitor_session_id)"))

    appointment_col = columns.get("appointment_id")
    if appointment_col and not appointment_col.get("nullable", True):
        if bind.dialect.name == "postgresql":
            op.alter_column(
                "call_sessions",
                "appointment_id",
                existing_type=sa.String(length=36),
                nullable=True,
            )


def downgrade() -> None:
    # Non-destructive downgrade.
    pass

