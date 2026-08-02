"""panic audio segments

Revision ID: 20260710_0011
Revises: 20260709_0010
Create Date: 2026-07-10 00:00:00.000000
"""

from __future__ import annotations

from alembic import op
import sqlalchemy as sa
from sqlalchemy import inspect


revision = "20260710_0011"
down_revision = "20260709_0010"
branch_labels = None
depends_on = None


def upgrade() -> None:
    bind = op.get_bind()
    inspector = inspect(bind)
    table_names = set(inspector.get_table_names())

    if "panic_audio_segments" not in table_names:
        op.create_table(
            "panic_audio_segments",
            sa.Column("id", sa.String(length=36), primary_key=True, nullable=False),
            sa.Column("panic_id", sa.String(length=36), nullable=False),
            sa.Column("uploader_user_id", sa.String(length=36), nullable=False),
            sa.Column("segment_index", sa.Integer(), nullable=False, server_default="0"),
            sa.Column("media_type", sa.String(length=40), nullable=False, server_default="audio/webm"),
            sa.Column("media_path", sa.Text(), nullable=False),
            sa.Column("media_url", sa.Text(), nullable=True),
            sa.Column("media_sha256", sa.String(length=128), nullable=True),
            sa.Column("created_at", sa.DateTime(), nullable=False),
            sa.ForeignKeyConstraint(["panic_id"], ["panic_events.id"]),
            sa.ForeignKeyConstraint(["uploader_user_id"], ["users.id"]),
        )
        op.create_index(
            "ix_panic_audio_segments_panic_id",
            "panic_audio_segments",
            ["panic_id"],
            unique=False,
            if_not_exists=True,
        )
        op.create_index(
            "ix_panic_audio_segments_uploader_user_id",
            "panic_audio_segments",
            ["uploader_user_id"],
            unique=False,
            if_not_exists=True,
        )
        op.create_index(
            "ix_panic_audio_segments_segment_index",
            "panic_audio_segments",
            ["segment_index"],
            unique=False,
            if_not_exists=True,
        )
        op.create_index(
            "ix_panic_audio_segments_created_at",
            "panic_audio_segments",
            ["created_at"],
            unique=False,
            if_not_exists=True,
        )


def downgrade() -> None:
    bind = op.get_bind()
    inspector = inspect(bind)
    table_names = set(inspector.get_table_names())

    if "panic_audio_segments" in table_names:
        indexes = {index["name"] for index in inspector.get_indexes("panic_audio_segments")}
        if "ix_panic_audio_segments_created_at" in indexes:
            op.drop_index("ix_panic_audio_segments_created_at", table_name="panic_audio_segments")
        if "ix_panic_audio_segments_segment_index" in indexes:
            op.drop_index("ix_panic_audio_segments_segment_index", table_name="panic_audio_segments")
        if "ix_panic_audio_segments_uploader_user_id" in indexes:
            op.drop_index("ix_panic_audio_segments_uploader_user_id", table_name="panic_audio_segments")
        if "ix_panic_audio_segments_panic_id" in indexes:
            op.drop_index("ix_panic_audio_segments_panic_id", table_name="panic_audio_segments")
        op.drop_table("panic_audio_segments")
