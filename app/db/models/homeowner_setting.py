from __future__ import annotations

import uuid
from datetime import datetime

from sqlalchemy import Boolean, DateTime, ForeignKey, String, Text
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base


class HomeownerSetting(Base):
    __tablename__ = "homeowner_settings"

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
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)
