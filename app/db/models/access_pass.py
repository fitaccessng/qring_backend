from __future__ import annotations

import uuid
from datetime import datetime
from typing import Optional

from sqlalchemy import Boolean, DateTime, ForeignKey, Integer, String
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base


class DigitalAccessPass(Base):
    __tablename__ = "digital_access_passes"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=lambda: str(uuid.uuid4()))
    homeowner_id: Mapped[str] = mapped_column(String(36), ForeignKey("users.id"), nullable=False, index=True)
    estate_id: Mapped[Optional[str]] = mapped_column(String(36), ForeignKey("estates.id"), nullable=True, index=True)
    home_id: Mapped[Optional[str]] = mapped_column(String(36), ForeignKey("homes.id"), nullable=True, index=True)
    door_id: Mapped[Optional[str]] = mapped_column(String(36), ForeignKey("doors.id"), nullable=True, index=True)
    pass_type: Mapped[str] = mapped_column(String(20), default="qr", index=True)
    label: Mapped[str] = mapped_column(String(120), default="Guest Access")
    visitor_name: Mapped[Optional[str]] = mapped_column(String(120), nullable=True)
    code_value: Mapped[str] = mapped_column(String(80), unique=True, nullable=False, index=True)
    valid_from: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow, index=True)
    valid_until: Mapped[datetime] = mapped_column(DateTime, nullable=False, index=True)
    max_uses: Mapped[int] = mapped_column(Integer, default=1)
    used_count: Mapped[int] = mapped_column(Integer, default=0)
    is_active: Mapped[bool] = mapped_column(Boolean, default=True, index=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow, index=True)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)
