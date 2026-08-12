from __future__ import annotations

import uuid
from datetime import datetime
from typing import Optional

from sqlalchemy import Boolean, DateTime, ForeignKey, Index, Integer, String, Text
from sqlalchemy.orm import Mapped, mapped_column

from app.core.time import utc_now
from app.db.base import Base


class ResidentVehicle(Base):
    __tablename__ = "resident_vehicles"
    __table_args__ = (
        Index("ix_resident_vehicles_estate_plate", "estate_id", "plate_number"),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=lambda: str(uuid.uuid4()))
    estate_id: Mapped[str] = mapped_column(String(36), ForeignKey("estates.id"), nullable=False, index=True)
    home_id: Mapped[str] = mapped_column(String(36), ForeignKey("homes.id"), nullable=False, index=True)
    resident_id: Mapped[str] = mapped_column(String(36), ForeignKey("users.id"), nullable=False, index=True)
    plate_number: Mapped[str] = mapped_column(String(40), nullable=False, index=True)
    vehicle_type: Mapped[str] = mapped_column(String(40), default="car")
    make_model: Mapped[Optional[str]] = mapped_column(String(120), nullable=True)
    color: Mapped[Optional[str]] = mapped_column(String(60), nullable=True)
    is_active: Mapped[bool] = mapped_column(Boolean, default=True, index=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utc_now, index=True)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=utc_now, onupdate=utc_now)


class VisitorBlocklistEntry(Base):
    __tablename__ = "visitor_blocklist_entries"
    __table_args__ = (
        Index("ix_visitor_blocklist_estate_phone", "estate_id", "visitor_phone"),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=lambda: str(uuid.uuid4()))
    estate_id: Mapped[str] = mapped_column(String(36), ForeignKey("estates.id"), nullable=False, index=True)
    visitor_name: Mapped[str] = mapped_column(String(120), default="")
    visitor_phone: Mapped[Optional[str]] = mapped_column(String(40), nullable=True, index=True)
    reason: Mapped[str] = mapped_column(Text, default="")
    is_active: Mapped[bool] = mapped_column(Boolean, default=True, index=True)
    created_by_user_id: Mapped[str] = mapped_column(String(36), ForeignKey("users.id"), nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utc_now, index=True)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=utc_now, onupdate=utc_now)


class EstatePackage(Base):
    __tablename__ = "estate_packages"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=lambda: str(uuid.uuid4()))
    estate_id: Mapped[str] = mapped_column(String(36), ForeignKey("estates.id"), nullable=False, index=True)
    home_id: Mapped[str] = mapped_column(String(36), ForeignKey("homes.id"), nullable=False, index=True)
    resident_id: Mapped[str] = mapped_column(String(36), ForeignKey("users.id"), nullable=False, index=True)
    courier: Mapped[Optional[str]] = mapped_column(String(120), nullable=True)
    description: Mapped[str] = mapped_column(Text, default="")
    status: Mapped[str] = mapped_column(String(30), default="arrived", index=True)
    recorded_by_user_id: Mapped[str] = mapped_column(String(36), ForeignKey("users.id"), nullable=False)
    collected_by_user_id: Mapped[Optional[str]] = mapped_column(String(36), ForeignKey("users.id"), nullable=True)
    gate_id: Mapped[Optional[str]] = mapped_column(String(80), nullable=True, index=True)
    arrived_at: Mapped[datetime] = mapped_column(DateTime, default=utc_now, index=True)
    collected_at: Mapped[Optional[datetime]] = mapped_column(DateTime, nullable=True)


class GuardAttendance(Base):
    __tablename__ = "guard_attendance"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=lambda: str(uuid.uuid4()))
    estate_id: Mapped[str] = mapped_column(String(36), ForeignKey("estates.id"), nullable=False, index=True)
    guard_user_id: Mapped[str] = mapped_column(String(36), ForeignKey("users.id"), nullable=False, index=True)
    gate_id: Mapped[Optional[str]] = mapped_column(String(80), nullable=True, index=True)
    status: Mapped[str] = mapped_column(String(30), default="on_duty", index=True)
    clock_in_at: Mapped[datetime] = mapped_column(DateTime, default=utc_now, index=True)
    clock_out_at: Mapped[Optional[datetime]] = mapped_column(DateTime, nullable=True)


class SecurityIncident(Base):
    __tablename__ = "security_incidents"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=lambda: str(uuid.uuid4()))
    estate_id: Mapped[str] = mapped_column(String(36), ForeignKey("estates.id"), nullable=False, index=True)
    reported_by_user_id: Mapped[str] = mapped_column(String(36), ForeignKey("users.id"), nullable=False, index=True)
    incident_type: Mapped[str] = mapped_column(String(80), default="general", index=True)
    severity: Mapped[str] = mapped_column(String(30), default="medium", index=True)
    description: Mapped[str] = mapped_column(Text, default="")
    gate_id: Mapped[Optional[str]] = mapped_column(String(80), nullable=True, index=True)
    related_visitor_session_id: Mapped[Optional[str]] = mapped_column(String(36), ForeignKey("visitor_sessions.id"), nullable=True)
    photo_url: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    status: Mapped[str] = mapped_column(String(30), default="open", index=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utc_now, index=True)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=utc_now, onupdate=utc_now)
