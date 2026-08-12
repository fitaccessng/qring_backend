"""estate phase 1 operations

Revision ID: 20260810_0012
Revises: 20260710_0011
Create Date: 2026-08-10 00:00:00.000000
"""

from __future__ import annotations

from alembic import op
import sqlalchemy as sa
from sqlalchemy import inspect


revision = "20260810_0012"
down_revision = "20260710_0011"
branch_labels = None
depends_on = None


def _create_index(name: str, table: str, columns: list[str]) -> None:
    op.create_index(name, table, columns, unique=False, if_not_exists=True)


def upgrade() -> None:
    inspector = inspect(op.get_bind())
    tables = set(inspector.get_table_names())

    if "resident_vehicles" not in tables:
        op.create_table(
            "resident_vehicles",
            sa.Column("id", sa.String(length=36), primary_key=True),
            sa.Column("estate_id", sa.String(length=36), nullable=False),
            sa.Column("home_id", sa.String(length=36), nullable=False),
            sa.Column("resident_id", sa.String(length=36), nullable=False),
            sa.Column("plate_number", sa.String(length=40), nullable=False),
            sa.Column("vehicle_type", sa.String(length=40), nullable=False, server_default="car"),
            sa.Column("make_model", sa.String(length=120), nullable=True),
            sa.Column("color", sa.String(length=60), nullable=True),
            sa.Column("is_active", sa.Boolean(), nullable=False, server_default=sa.true()),
            sa.Column("created_at", sa.DateTime(), nullable=False),
            sa.Column("updated_at", sa.DateTime(), nullable=False),
            sa.ForeignKeyConstraint(["estate_id"], ["estates.id"]),
            sa.ForeignKeyConstraint(["home_id"], ["homes.id"]),
            sa.ForeignKeyConstraint(["resident_id"], ["users.id"]),
        )
        _create_index("ix_resident_vehicles_estate_id", "resident_vehicles", ["estate_id"])
        _create_index("ix_resident_vehicles_home_id", "resident_vehicles", ["home_id"])
        _create_index("ix_resident_vehicles_resident_id", "resident_vehicles", ["resident_id"])
        _create_index("ix_resident_vehicles_plate_number", "resident_vehicles", ["plate_number"])
        _create_index("ix_resident_vehicles_estate_plate", "resident_vehicles", ["estate_id", "plate_number"])

    if "visitor_blocklist_entries" not in tables:
        op.create_table(
            "visitor_blocklist_entries",
            sa.Column("id", sa.String(length=36), primary_key=True),
            sa.Column("estate_id", sa.String(length=36), nullable=False),
            sa.Column("visitor_name", sa.String(length=120), nullable=False, server_default=""),
            sa.Column("visitor_phone", sa.String(length=40), nullable=True),
            sa.Column("reason", sa.Text(), nullable=False, server_default=""),
            sa.Column("is_active", sa.Boolean(), nullable=False, server_default=sa.true()),
            sa.Column("created_by_user_id", sa.String(length=36), nullable=False),
            sa.Column("created_at", sa.DateTime(), nullable=False),
            sa.Column("updated_at", sa.DateTime(), nullable=False),
            sa.ForeignKeyConstraint(["estate_id"], ["estates.id"]),
            sa.ForeignKeyConstraint(["created_by_user_id"], ["users.id"]),
        )
        _create_index("ix_visitor_blocklist_entries_estate_id", "visitor_blocklist_entries", ["estate_id"])
        _create_index("ix_visitor_blocklist_entries_visitor_phone", "visitor_blocklist_entries", ["visitor_phone"])
        _create_index("ix_visitor_blocklist_estate_phone", "visitor_blocklist_entries", ["estate_id", "visitor_phone"])

    if "estate_packages" not in tables:
        op.create_table(
            "estate_packages",
            sa.Column("id", sa.String(length=36), primary_key=True),
            sa.Column("estate_id", sa.String(length=36), nullable=False),
            sa.Column("home_id", sa.String(length=36), nullable=False),
            sa.Column("resident_id", sa.String(length=36), nullable=False),
            sa.Column("courier", sa.String(length=120), nullable=True),
            sa.Column("description", sa.Text(), nullable=False, server_default=""),
            sa.Column("status", sa.String(length=30), nullable=False, server_default="arrived"),
            sa.Column("recorded_by_user_id", sa.String(length=36), nullable=False),
            sa.Column("collected_by_user_id", sa.String(length=36), nullable=True),
            sa.Column("gate_id", sa.String(length=80), nullable=True),
            sa.Column("arrived_at", sa.DateTime(), nullable=False),
            sa.Column("collected_at", sa.DateTime(), nullable=True),
            sa.ForeignKeyConstraint(["estate_id"], ["estates.id"]),
            sa.ForeignKeyConstraint(["home_id"], ["homes.id"]),
            sa.ForeignKeyConstraint(["resident_id"], ["users.id"]),
            sa.ForeignKeyConstraint(["recorded_by_user_id"], ["users.id"]),
            sa.ForeignKeyConstraint(["collected_by_user_id"], ["users.id"]),
        )
        _create_index("ix_estate_packages_estate_id", "estate_packages", ["estate_id"])
        _create_index("ix_estate_packages_home_id", "estate_packages", ["home_id"])
        _create_index("ix_estate_packages_resident_id", "estate_packages", ["resident_id"])
        _create_index("ix_estate_packages_status", "estate_packages", ["status"])

    if "guard_attendance" not in tables:
        op.create_table(
            "guard_attendance",
            sa.Column("id", sa.String(length=36), primary_key=True),
            sa.Column("estate_id", sa.String(length=36), nullable=False),
            sa.Column("guard_user_id", sa.String(length=36), nullable=False),
            sa.Column("gate_id", sa.String(length=80), nullable=True),
            sa.Column("status", sa.String(length=30), nullable=False, server_default="on_duty"),
            sa.Column("clock_in_at", sa.DateTime(), nullable=False),
            sa.Column("clock_out_at", sa.DateTime(), nullable=True),
            sa.ForeignKeyConstraint(["estate_id"], ["estates.id"]),
            sa.ForeignKeyConstraint(["guard_user_id"], ["users.id"]),
        )
        _create_index("ix_guard_attendance_estate_id", "guard_attendance", ["estate_id"])
        _create_index("ix_guard_attendance_guard_user_id", "guard_attendance", ["guard_user_id"])
        _create_index("ix_guard_attendance_status", "guard_attendance", ["status"])

    if "security_incidents" not in tables:
        op.create_table(
            "security_incidents",
            sa.Column("id", sa.String(length=36), primary_key=True),
            sa.Column("estate_id", sa.String(length=36), nullable=False),
            sa.Column("reported_by_user_id", sa.String(length=36), nullable=False),
            sa.Column("incident_type", sa.String(length=80), nullable=False, server_default="general"),
            sa.Column("severity", sa.String(length=30), nullable=False, server_default="medium"),
            sa.Column("description", sa.Text(), nullable=False, server_default=""),
            sa.Column("gate_id", sa.String(length=80), nullable=True),
            sa.Column("related_visitor_session_id", sa.String(length=36), nullable=True),
            sa.Column("photo_url", sa.Text(), nullable=True),
            sa.Column("status", sa.String(length=30), nullable=False, server_default="open"),
            sa.Column("created_at", sa.DateTime(), nullable=False),
            sa.Column("updated_at", sa.DateTime(), nullable=False),
            sa.ForeignKeyConstraint(["estate_id"], ["estates.id"]),
            sa.ForeignKeyConstraint(["reported_by_user_id"], ["users.id"]),
            sa.ForeignKeyConstraint(["related_visitor_session_id"], ["visitor_sessions.id"]),
        )
        _create_index("ix_security_incidents_estate_id", "security_incidents", ["estate_id"])
        _create_index("ix_security_incidents_reported_by_user_id", "security_incidents", ["reported_by_user_id"])
        _create_index("ix_security_incidents_incident_type", "security_incidents", ["incident_type"])
        _create_index("ix_security_incidents_status", "security_incidents", ["status"])


def downgrade() -> None:
    inspector = inspect(op.get_bind())
    for table in ["security_incidents", "guard_attendance", "estate_packages", "visitor_blocklist_entries", "resident_vehicles"]:
        if table in set(inspector.get_table_names()):
            op.drop_table(table)
