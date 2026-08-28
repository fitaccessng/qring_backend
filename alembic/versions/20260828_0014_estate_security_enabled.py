"""add estate security enabled flag

Revision ID: 20260828_0014
Revises: 20260827_0013
Create Date: 2026-08-28 00:00:00.000000
"""

from __future__ import annotations

from alembic import op
import sqlalchemy as sa
from sqlalchemy import inspect


revision = "20260828_0014"
down_revision = "20260827_0013"
branch_labels = None
depends_on = None


def _column_exists(table_name: str, column_name: str) -> bool:
    inspector = inspect(op.get_bind())
    if table_name not in set(inspector.get_table_names()):
        return False
    return column_name in {column["name"] for column in inspector.get_columns(table_name)}


def upgrade() -> None:
    if not _column_exists("estates", "security_enabled"):
        op.add_column("estates", sa.Column("security_enabled", sa.Boolean(), server_default=sa.true(), nullable=True))


def downgrade() -> None:
    if _column_exists("estates", "security_enabled"):
        op.drop_column("estates", "security_enabled")
