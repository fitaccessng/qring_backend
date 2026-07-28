"""office attendance logs

Revision ID: 20260709_0010
Revises: 20260703_0009
Create Date: 2026-07-09 00:10:00.000000
"""

from __future__ import annotations

from alembic import op
import sqlalchemy as sa
from sqlalchemy import inspect


revision = "20260709_0010"
down_revision = "20260703_0009"
branch_labels = None
depends_on = None


def upgrade() -> None:
    bind = op.get_bind()
    inspector = inspect(bind)
    table_names = set(inspector.get_table_names())

    if "office_attendance_logs" not in table_names:
        op.create_table(
            "office_attendance_logs",
            sa.Column("id", sa.String(length=36), primary_key=True, nullable=False),
            sa.Column("office_id", sa.String(length=36), nullable=False),
            sa.Column("office_member_id", sa.String(length=36), nullable=False),
            sa.Column("user_id", sa.String(length=36), nullable=True),
            sa.Column("employee_name", sa.String(length=160), nullable=False),
            sa.Column("action", sa.String(length=40), nullable=False),
            sa.Column("note", sa.String(length=255), nullable=True),
            sa.Column("source", sa.String(length=40), nullable=False, server_default="qr_scan"),
            sa.Column("created_at", sa.DateTime(), nullable=False),
            sa.ForeignKeyConstraint(["office_id"], ["offices.id"]),
            sa.ForeignKeyConstraint(["office_member_id"], ["office_members.id"]),
            sa.ForeignKeyConstraint(["user_id"], ["users.id"]),
        )
        op.create_index(
            "ix_office_attendance_logs_office_id",
            "office_attendance_logs",
            ["office_id"],
            unique=False,
            if_not_exists=True,
        )
        op.create_index(
            "ix_office_attendance_logs_office_member_id",
            "office_attendance_logs",
            ["office_member_id"],
            unique=False,
            if_not_exists=True,
        )
        op.create_index(
            "ix_office_attendance_logs_user_id",
            "office_attendance_logs",
            ["user_id"],
            unique=False,
            if_not_exists=True,
        )
        op.create_index(
            "ix_office_attendance_logs_created_at",
            "office_attendance_logs",
            ["created_at"],
            unique=False,
            if_not_exists=True,
        )


def downgrade() -> None:
    bind = op.get_bind()
    inspector = inspect(bind)
    table_names = set(inspector.get_table_names())

    if "office_attendance_logs" in table_names:
        indexes = {index["name"] for index in inspector.get_indexes("office_attendance_logs")}
        if "ix_office_attendance_logs_created_at" in indexes:
            op.drop_index("ix_office_attendance_logs_created_at", table_name="office_attendance_logs")
        if "ix_office_attendance_logs_user_id" in indexes:
            op.drop_index("ix_office_attendance_logs_user_id", table_name="office_attendance_logs")
        if "ix_office_attendance_logs_office_member_id" in indexes:
            op.drop_index("ix_office_attendance_logs_office_member_id", table_name="office_attendance_logs")
        if "ix_office_attendance_logs_office_id" in indexes:
            op.drop_index("ix_office_attendance_logs_office_id", table_name="office_attendance_logs")
        op.drop_table("office_attendance_logs")
