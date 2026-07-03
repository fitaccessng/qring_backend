"""office module schema

Revision ID: 20260703_0009
Revises: 20260527_0008
Create Date: 2026-07-03 00:09:00.000000
"""

from __future__ import annotations

from alembic import op
import sqlalchemy as sa
from sqlalchemy import inspect, text


revision = "20260703_0009"
down_revision = "20260527_0008"
branch_labels = None
depends_on = None


def _add_column_if_missing(table_name: str, column_name: str, column: sa.Column) -> None:
    bind = op.get_bind()
    inspector = inspect(bind)
    table_names = set(inspector.get_table_names())
    if table_name not in table_names:
        return
    existing = {col["name"] for col in inspector.get_columns(table_name)}
    if column_name not in existing:
        op.add_column(table_name, column)


def _create_index_if_missing(name: str, table_name: str, columns: list[str]) -> None:
    bind = op.get_bind()
    inspector = inspect(bind)
    table_names = set(inspector.get_table_names())
    if table_name not in table_names:
        return
    indexes = {index["name"] for index in inspector.get_indexes(table_name)}
    if name not in indexes:
        op.create_index(name, table_name, columns, unique=False)


def _create_fk_if_missing(table_name: str, fk_name: str, local_cols: list[str], remote_cols: list[str]) -> None:
    bind = op.get_bind()
    inspector = inspect(bind)
    table_names = set(inspector.get_table_names())
    if table_name not in table_names:
        return
    foreign_keys = {fk.get("name") for fk in inspector.get_foreign_keys(table_name)}
    if fk_name not in foreign_keys:
        op.create_foreign_key(fk_name, table_name, "offices", local_cols, remote_cols)


def _drop_fk_if_exists(table_name: str, fk_name: str) -> None:
    bind = op.get_bind()
    inspector = inspect(bind)
    table_names = set(inspector.get_table_names())
    if table_name not in table_names:
        return
    foreign_keys = {fk.get("name") for fk in inspector.get_foreign_keys(table_name)}
    if fk_name in foreign_keys:
        op.drop_constraint(fk_name, table_name, type_="foreignkey")


def upgrade() -> None:
    bind = op.get_bind()
    inspector = inspect(bind)
    table_names = set(inspector.get_table_names())

    if "offices" not in table_names:
        op.create_table(
            "offices",
            sa.Column("id", sa.String(length=36), primary_key=True, nullable=False),
            sa.Column("company_name", sa.String(length=160), nullable=False),
            sa.Column("business_email", sa.String(length=255), nullable=False),
            sa.Column("phone_number", sa.String(length=40), nullable=True),
            sa.Column("office_address", sa.Text(), nullable=True),
            sa.Column("country", sa.String(length=80), nullable=True),
            sa.Column("state", sa.String(length=80), nullable=True),
            sa.Column("city", sa.String(length=80), nullable=True),
            sa.Column("office_size", sa.String(length=80), nullable=True),
            sa.Column("industry", sa.String(length=120), nullable=True),
            sa.Column("employee_count", sa.Integer(), nullable=False, server_default="0"),
            sa.Column("timezone", sa.String(length=80), nullable=True),
            sa.Column("administrator_user_id", sa.String(length=36), nullable=False),
            sa.Column("reception_home_id", sa.String(length=36), nullable=True),
            sa.Column("reception_door_id", sa.String(length=36), nullable=True),
            sa.Column("qr_id", sa.String(length=64), nullable=False),
            sa.Column("active", sa.Boolean(), nullable=False, server_default=sa.text("true")),
            sa.Column("created_at", sa.DateTime(), nullable=False),
            sa.Column("updated_at", sa.DateTime(), nullable=False),
            sa.ForeignKeyConstraint(["administrator_user_id"], ["users.id"]),
            sa.ForeignKeyConstraint(["reception_home_id"], ["homes.id"]),
            sa.ForeignKeyConstraint(["reception_door_id"], ["doors.id"]),
            sa.UniqueConstraint("qr_id", name="uq_offices_qr_id"),
        )
        op.create_index("ix_offices_business_email", "offices", ["business_email"], unique=False, if_not_exists=True)
        op.create_index("ix_offices_administrator_user_id", "offices", ["administrator_user_id"], unique=False, if_not_exists=True)

    if "office_members" not in table_names:
        op.create_table(
            "office_members",
            sa.Column("id", sa.String(length=36), primary_key=True, nullable=False),
            sa.Column("office_id", sa.String(length=36), nullable=False),
            sa.Column("user_id", sa.String(length=36), nullable=True),
            sa.Column("full_name", sa.String(length=160), nullable=False),
            sa.Column("role_label", sa.String(length=80), nullable=False, server_default="employee"),
            sa.Column("department", sa.String(length=120), nullable=True),
            sa.Column("floor", sa.String(length=40), nullable=True),
            sa.Column("extension", sa.String(length=40), nullable=True),
            sa.Column("availability_status", sa.String(length=40), nullable=False, server_default="available"),
            sa.Column("status", sa.String(length=40), nullable=False, server_default="active"),
            sa.Column("created_at", sa.DateTime(), nullable=False),
            sa.Column("updated_at", sa.DateTime(), nullable=False),
            sa.ForeignKeyConstraint(["office_id"], ["offices.id"]),
            sa.ForeignKeyConstraint(["user_id"], ["users.id"]),
        )
        op.create_index("ix_office_members_office_id", "office_members", ["office_id"], unique=False, if_not_exists=True)
        op.create_index("ix_office_members_user_id", "office_members", ["user_id"], unique=False, if_not_exists=True)

    _add_column_if_missing(
        "homes",
        "office_id",
        sa.Column("office_id", sa.String(length=36), nullable=True)
    )
    _add_column_if_missing(
        "appointments",
        "office_id",
        sa.Column("office_id", sa.String(length=36), nullable=True)
    )

    _create_index_if_missing("ix_homes_office_id", "homes", ["office_id"])
    _create_index_if_missing("ix_appointments_office_id", "appointments", ["office_id"])
    _create_fk_if_missing("homes", "fk_homes_office_id_offices", ["office_id"], ["id"])
    _create_fk_if_missing("appointments", "fk_appointments_office_id_offices", ["office_id"], ["id"])

    if bind.dialect.name == "postgresql":
        try:
            bind.execute(
                text(
                    "DO $$ BEGIN "
                    "ALTER TYPE userrole ADD VALUE IF NOT EXISTS 'office'; "
                    "EXCEPTION WHEN undefined_object THEN NULL; END $$;"
                )
            )
        except Exception:
            pass


def downgrade() -> None:
    bind = op.get_bind()
    inspector = inspect(bind)
    table_names = set(inspector.get_table_names())

    if "office_members" in table_names:
        indexes = {index["name"] for index in inspector.get_indexes("office_members")}
        if "ix_office_members_user_id" in indexes:
            op.drop_index("ix_office_members_user_id", table_name="office_members")
        if "ix_office_members_office_id" in indexes:
            op.drop_index("ix_office_members_office_id", table_name="office_members")
        op.drop_table("office_members")

    _drop_fk_if_exists("appointments", "fk_appointments_office_id_offices")
    _drop_fk_if_exists("homes", "fk_homes_office_id_offices")

    if "appointments" in table_names:
        appointment_indexes = {index["name"] for index in inspector.get_indexes("appointments")}
        if "ix_appointments_office_id" in appointment_indexes:
            op.drop_index("ix_appointments_office_id", table_name="appointments")
        columns = {col["name"] for col in inspector.get_columns("appointments")}
        if "office_id" in columns:
            op.drop_column("appointments", "office_id")
    if "homes" in table_names:
        home_indexes = {index["name"] for index in inspector.get_indexes("homes")}
        if "ix_homes_office_id" in home_indexes:
            op.drop_index("ix_homes_office_id", table_name="homes")
        columns = {col["name"] for col in inspector.get_columns("homes")}
        if "office_id" in columns:
            op.drop_column("homes", "office_id")

    if "offices" in table_names:
        indexes = {index["name"] for index in inspector.get_indexes("offices")}
        if "ix_offices_administrator_user_id" in indexes:
            op.drop_index("ix_offices_administrator_user_id", table_name="offices")
        if "ix_offices_business_email" in indexes:
            op.drop_index("ix_offices_business_email", table_name="offices")
        op.drop_table("offices")
