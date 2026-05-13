"""add scaling indexes for sessions and notifications

Revision ID: 20260513_0007
Revises: 20260419_0006
Create Date: 2026-05-13 11:00:00.000000
"""

from alembic import op


# revision identifiers, used by Alembic.
revision = "20260513_0007"
down_revision = "20260419_0006"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_index(
        "ix_visitor_sessions_homeowner_started_at",
        "visitor_sessions",
        ["homeowner_id", "started_at"],
        unique=False,
    )
    op.create_index(
        "ix_visitor_sessions_estate_started_at",
        "visitor_sessions",
        ["estate_id", "started_at"],
        unique=False,
    )
    op.create_index(
        "ix_visitor_sessions_status_started_at",
        "visitor_sessions",
        ["status", "started_at"],
        unique=False,
    )
    op.create_index(
        "ix_messages_session_created_at",
        "messages",
        ["session_id", "created_at"],
        unique=False,
    )
    op.create_index(
        "ix_notifications_user_created_at",
        "notifications",
        ["user_id", "created_at"],
        unique=False,
    )
    op.create_index(
        "ix_call_sessions_homeowner_created_at",
        "call_sessions",
        ["homeowner_id", "created_at"],
        unique=False,
    )
    op.create_index(
        "ix_call_sessions_status_created_at",
        "call_sessions",
        ["status", "created_at"],
        unique=False,
    )


def downgrade() -> None:
    op.drop_index("ix_call_sessions_status_created_at", table_name="call_sessions")
    op.drop_index("ix_call_sessions_homeowner_created_at", table_name="call_sessions")
    op.drop_index("ix_notifications_user_created_at", table_name="notifications")
    op.drop_index("ix_messages_session_created_at", table_name="messages")
    op.drop_index("ix_visitor_sessions_status_started_at", table_name="visitor_sessions")
    op.drop_index("ix_visitor_sessions_estate_started_at", table_name="visitor_sessions")
    op.drop_index("ix_visitor_sessions_homeowner_started_at", table_name="visitor_sessions")
