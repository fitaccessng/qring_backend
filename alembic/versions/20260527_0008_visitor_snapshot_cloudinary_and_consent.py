"""visitor snapshot cloudinary and consent fields

Revision ID: 20260527_0008
Revises: 20260513_0007
Create Date: 2026-05-27 00:08:00.000000
"""

from __future__ import annotations

from alembic import op
import sqlalchemy as sa
from sqlalchemy import inspect, text


revision = "20260527_0008"
down_revision = "20260513_0007"
branch_labels = None
depends_on = None


def upgrade() -> None:
    bind = op.get_bind()
    inspector = inspect(bind)
    table_names = set(inspector.get_table_names())

    if "visitor_sessions" in table_names:
        visitor_session_columns = {col["name"] for col in inspector.get_columns("visitor_sessions")}
        if "snapshot_url" not in visitor_session_columns:
            op.add_column("visitor_sessions", sa.Column("snapshot_url", sa.Text(), nullable=True))

    if "visitor_snapshot_audits" in table_names:
        audit_columns = {col["name"] for col in inspector.get_columns("visitor_snapshot_audits")}
        audit_indexes = {index["name"] for index in inspector.get_indexes("visitor_snapshot_audits")}

        if "media_url" not in audit_columns:
            op.add_column("visitor_snapshot_audits", sa.Column("media_url", sa.Text(), nullable=True))

        if "cloudinary_public_id" not in audit_columns:
            op.add_column(
                "visitor_snapshot_audits",
                sa.Column("cloudinary_public_id", sa.String(length=255), nullable=True),
            )

        if "ix_visitor_snapshot_audits_cloudinary_public_id" not in audit_indexes:
            op.execute(
                text(
                    "CREATE INDEX IF NOT EXISTS ix_visitor_snapshot_audits_cloudinary_public_id "
                    "ON visitor_snapshot_audits (cloudinary_public_id)"
                )
            )


def downgrade() -> None:
    bind = op.get_bind()
    inspector = inspect(bind)
    table_names = set(inspector.get_table_names())

    if "visitor_snapshot_audits" in table_names:
        audit_columns = {col["name"] for col in inspector.get_columns("visitor_snapshot_audits")}
        audit_indexes = {index["name"] for index in inspector.get_indexes("visitor_snapshot_audits")}

        if "ix_visitor_snapshot_audits_cloudinary_public_id" in audit_indexes:
            op.drop_index("ix_visitor_snapshot_audits_cloudinary_public_id", table_name="visitor_snapshot_audits")

        if "cloudinary_public_id" in audit_columns:
            op.drop_column("visitor_snapshot_audits", "cloudinary_public_id")

        if "media_url" in audit_columns:
            op.drop_column("visitor_snapshot_audits", "media_url")

    if "visitor_sessions" in table_names:
        visitor_session_columns = {col["name"] for col in inspector.get_columns("visitor_sessions")}
        if "snapshot_url" in visitor_session_columns:
            op.drop_column("visitor_sessions", "snapshot_url")
