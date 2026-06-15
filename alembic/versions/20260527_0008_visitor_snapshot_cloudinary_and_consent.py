"""visitor snapshot cloudinary and consent fields

Revision ID: 20260527_0008
Revises: 20260513_0007
Create Date: 2026-05-27 00:08:00.000000
"""

from __future__ import annotations

from alembic import op
import sqlalchemy as sa


revision = "20260527_0008"
down_revision = "20260513_0007"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("visitor_sessions", sa.Column("snapshot_url", sa.Text(), nullable=True))
    op.add_column("visitor_snapshot_audits", sa.Column("media_url", sa.Text(), nullable=True))
    op.add_column("visitor_snapshot_audits", sa.Column("cloudinary_public_id", sa.String(length=255), nullable=True))
    op.create_index(
        "ix_visitor_snapshot_audits_cloudinary_public_id",
        "visitor_snapshot_audits",
        ["cloudinary_public_id"],
        unique=False,
    )


def downgrade() -> None:
    op.drop_index("ix_visitor_snapshot_audits_cloudinary_public_id", table_name="visitor_snapshot_audits")
    op.drop_column("visitor_snapshot_audits", "cloudinary_public_id")
    op.drop_column("visitor_snapshot_audits", "media_url")
    op.drop_column("visitor_sessions", "snapshot_url")
