from __future__ import annotations

import uuid
from datetime import datetime
from typing import Optional

from sqlalchemy import DateTime, Float, ForeignKey, Integer, String, Text
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base
from app.core.time import utc_now


class Appointment(Base):
    __tablename__ = "appointments"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=lambda: str(uuid.uuid4()))
    homeowner_id: Mapped[str] = mapped_column(String(36), ForeignKey("users.id"), nullable=False, index=True)
    home_id: Mapped[str] = mapped_column(String(36), ForeignKey("homes.id"), nullable=False, index=True)
    door_id: Mapped[str] = mapped_column(String(36), ForeignKey("doors.id"), nullable=False, index=True)
    visitor_name: Mapped[str] = mapped_column(String(120), default="Visitor")
    visitor_contact: Mapped[str] = mapped_column(String(120), default="")
    visitor_email: Mapped[Optional[str]] = mapped_column(String(255), nullable=True, index=True)
    purpose: Mapped[str] = mapped_column(Text, default="")
    starts_at: Mapped[datetime] = mapped_column(DateTime, nullable=False, index=True)
    ends_at: Mapped[datetime] = mapped_column(DateTime, nullable=False, index=True)
    status: Mapped[str] = mapped_column(String(40), default="created", index=True)

    geofence_lat: Mapped[Optional[float]] = mapped_column(Float, nullable=True)
    geofence_lng: Mapped[Optional[float]] = mapped_column(Float, nullable=True)
    geofence_radius_m: Mapped[int] = mapped_column(Integer, default=120)

    share_token_hash: Mapped[Optional[str]] = mapped_column(String(128), nullable=True, index=True)
    share_token_created_at: Mapped[Optional[datetime]] = mapped_column(DateTime, nullable=True)
    accepted_at: Mapped[Optional[datetime]] = mapped_column(DateTime, nullable=True)
    accepted_device_id: Mapped[Optional[str]] = mapped_column(String(120), nullable=True)

    qr_token_hash: Mapped[Optional[str]] = mapped_column(String(128), nullable=True, index=True)
    qr_payload_encrypted: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    qr_signature: Mapped[Optional[str]] = mapped_column(String(200), nullable=True)
    qr_expires_at: Mapped[Optional[datetime]] = mapped_column(DateTime, nullable=True)
    qr_used_at: Mapped[Optional[datetime]] = mapped_column(DateTime, nullable=True)
    qr_used_device_id: Mapped[Optional[str]] = mapped_column(String(120), nullable=True)

    arrived_at: Mapped[Optional[datetime]] = mapped_column(DateTime, nullable=True)
    arrival_lat: Mapped[Optional[float]] = mapped_column(Float, nullable=True)
    arrival_lng: Mapped[Optional[float]] = mapped_column(Float, nullable=True)
    arrival_battery_pct: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)

    created_at: Mapped[datetime] = mapped_column(DateTime, default=utc_now)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=utc_now, onupdate=utc_now)
