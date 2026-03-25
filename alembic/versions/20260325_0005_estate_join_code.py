"""estate join code

Revision ID: 20260325_0005
Revises: 20260322_0004
Create Date: 2026-03-25
"""

from alembic import op
import sqlalchemy as sa
from sqlalchemy import inspect, text


# revision identifiers, used by Alembic.
revision = "20260325_0005"
down_revision = "20260322_0004"
branch_labels = None
depends_on = None


def _has_index(inspector, table_name: str, index_name: str) -> bool:
    return any(index.get("name") == index_name for index in inspector.get_indexes(table_name))


def upgrade() -> None:
    bind = op.get_bind()
    inspector = inspect(bind)
    table_names = set(inspector.get_table_names())

    if "estates" not in table_names:
        return

    columns = {col["name"]: col for col in inspector.get_columns("estates")}
    if "join_code" not in columns:
        op.add_column("estates", sa.Column("join_code", sa.String(length=24), nullable=True))

    inspector = inspect(bind)
    # Ensure there is an index to speed up join lookups; unique when supported.
    if not _has_index(inspector, "estates", "ix_estates_join_code"):
        if bind.dialect.name == "postgresql":
            op.execute(text("CREATE UNIQUE INDEX IF NOT EXISTS ix_estates_join_code ON estates (join_code)"))
        else:
            op.execute(text("CREATE UNIQUE INDEX IF NOT EXISTS ix_estates_join_code ON estates (join_code)"))


def downgrade() -> None:
    bind = op.get_bind()
    inspector = inspect(bind)
    table_names = set(inspector.get_table_names())
    if "estates" not in table_names:
        return

    try:
        op.execute(text("DROP INDEX IF EXISTS ix_estates_join_code"))
    except Exception:
        pass

    columns = {col["name"]: col for col in inspector.get_columns("estates")}
    if "join_code" in columns:
        op.drop_column("estates", "join_code")

