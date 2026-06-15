"""add scaling indexes for sessions and notifications

Revision ID: 20260513_0007
Revises: 20260419_0006
Create Date: 2026-05-13 11:00:00.000000
"""

from alembic import op
from sqlalchemy import inspect


# revision identifiers, used by Alembic.
revision = "20260513_0007"
down_revision = "20260419_0006"
branch_labels = None
depends_on = None


def _ensure_index(table_name: str, index_name: str, columns: list[str]) -> None:
    bind = op.get_bind()
    inspector = inspect(bind)
    if table_name not in set(inspector.get_table_names()):
        return

    existing_indexes = {index["name"] for index in inspector.get_indexes(table_name)}
    if index_name not in existing_indexes:
        op.create_index(index_name, table_name, columns, unique=False)


def upgrade() -> None:
    _ensure_index("visitor_sessions", "ix_visitor_sessions_homeowner_started_at", ["homeowner_id", "started_at"])
    _ensure_index("visitor_sessions", "ix_visitor_sessions_estate_started_at", ["estate_id", "started_at"])
    _ensure_index("visitor_sessions", "ix_visitor_sessions_status_started_at", ["status", "started_at"])
    _ensure_index("messages", "ix_messages_session_created_at", ["session_id", "created_at"])
    _ensure_index("notifications", "ix_notifications_user_created_at", ["user_id", "created_at"])
    _ensure_index("call_sessions", "ix_call_sessions_homeowner_created_at", ["homeowner_id", "created_at"])
    _ensure_index("call_sessions", "ix_call_sessions_status_created_at", ["status", "created_at"])


def downgrade() -> None:
    op.drop_index("ix_call_sessions_status_created_at", table_name="call_sessions")
    op.drop_index("ix_call_sessions_homeowner_created_at", table_name="call_sessions")
    op.drop_index("ix_notifications_user_created_at", table_name="notifications")
    op.drop_index("ix_messages_session_created_at", table_name="messages")
    op.drop_index("ix_visitor_sessions_status_started_at", table_name="visitor_sessions")
    op.drop_index("ix_visitor_sessions_estate_started_at", table_name="visitor_sessions")
    op.drop_index("ix_visitor_sessions_homeowner_started_at", table_name="visitor_sessions")
