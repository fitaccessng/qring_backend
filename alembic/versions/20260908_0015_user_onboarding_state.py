"""add user onboarding state

Revision ID: 20260908_0015
Revises: 20260828_0014
Create Date: 2026-09-08 00:00:00.000000
"""

from alembic import op
import sqlalchemy as sa
from sqlalchemy import inspect


revision = "20260908_0015"
down_revision = "20260828_0014"
branch_labels = None
depends_on = None


def upgrade() -> None:
    inspector = inspect(op.get_bind())
    columns = {column["name"] for column in inspector.get_columns("users")}
    if "onboarding_state" not in columns:
        op.add_column("users", sa.Column("onboarding_state", sa.JSON(), nullable=True))


def downgrade() -> None:
    inspector = inspect(op.get_bind())
    columns = {column["name"] for column in inspector.get_columns("users")}
    if "onboarding_state" in columns:
        op.drop_column("users", "onboarding_state")