"""expand qr code mode length

Revision ID: 20260827_0013
Revises: 20260810_0012
Create Date: 2026-08-27 00:00:00.000000
"""

from __future__ import annotations

from alembic import op
import sqlalchemy as sa
from sqlalchemy import inspect


revision = "20260827_0013"
down_revision = "20260810_0012"
branch_labels = None
depends_on = None


def _qr_mode_column_exists() -> bool:
    inspector = inspect(op.get_bind())
    if "qr_codes" not in set(inspector.get_table_names()):
        return False
    return "mode" in {column["name"] for column in inspector.get_columns("qr_codes")}


def upgrade() -> None:
    if not _qr_mode_column_exists():
        return
    op.alter_column(
        "qr_codes",
        "mode",
        existing_type=sa.String(length=20),
        type_=sa.String(length=80),
        existing_nullable=True,
        existing_server_default=None,
    )


def downgrade() -> None:
    if not _qr_mode_column_exists():
        return
    op.alter_column(
        "qr_codes",
        "mode",
        existing_type=sa.String(length=80),
        type_=sa.String(length=20),
        existing_nullable=True,
        existing_server_default=None,
    )
