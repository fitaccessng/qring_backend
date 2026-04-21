from __future__ import annotations

import uuid
from datetime import datetime

from sqlalchemy import Boolean, DateTime, Float, ForeignKey, Integer, String, Text
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base


class ResidentSetting(Base):
    __tablename__ = "resident_settings"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=lambda: str(uuid.uuid4()))
    user_id: Mapped[str] = mapped_column(String(36), ForeignKey("users.id"), nullable=False, unique=True, index=True)
    push_alerts: Mapped[bool] = mapped_column(Boolean, default=True)
    sound_alerts: Mapped[bool] = mapped_column(Boolean, default=True)
    auto_reject_unknown_visitors: Mapped[bool] = mapped_column(Boolean, default=False)
    auto_approve_trusted_visitors: Mapped[bool] = mapped_column(Boolean, default=False)
    auto_approve_known_contacts: Mapped[bool] = mapped_column(Boolean, default=False)
    known_contacts_json: Mapped[str] = mapped_column(Text, default="[]")
    allow_delivery_drop_at_gate: Mapped[bool] = mapped_column(Boolean, default=True)
    sms_fallback_enabled: Mapped[bool] = mapped_column(Boolean, default=False)
    nearby_panic_alerts_enabled: Mapped[bool] = mapped_column(Boolean, default=True)
    nearby_panic_alert_radius_m: Mapped[int] = mapped_column(Integer, default=500)
    nearby_panic_availability_mode: Mapped[str] = mapped_column(String(24), default="always")
    nearby_panic_schedule_json: Mapped[str] = mapped_column(Text, default="[]")
    nearby_panic_receive_from: Mapped[str] = mapped_column(String(24), default="everyone")
    nearby_panic_muted_until: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    nearby_panic_same_area_label: Mapped[str | None] = mapped_column(String(120), nullable=True)
    panic_identity_visibility: Mapped[str] = mapped_column(String(24), default="masked")
    safety_home_lat: Mapped[float | None] = mapped_column(Float, nullable=True)
    safety_home_lng: Mapped[float | None] = mapped_column(Float, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)


# Alias for backward compatibility
HomeownerSetting = ResidentSetting
