from __future__ import annotations

import uuid
from datetime import datetime
from enum import Enum
from typing import Optional

from sqlalchemy import Boolean, DateTime, Enum as SqlEnum, ForeignKey, Integer, String
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.db.base import Base
from app.core.time import utc_now


class UserRole(str, Enum):
    homeowner = "homeowner"
    estate = "estate"
    admin = "admin"
    security = "security"


class User(Base):
    __tablename__ = "users"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=lambda: str(uuid.uuid4()))
    full_name: Mapped[str] = mapped_column(String(120), nullable=False)
    email: Mapped[str] = mapped_column(String(255), unique=True, nullable=False, index=True)
    password_hash: Mapped[str] = mapped_column(String(255), nullable=False)
    role: Mapped[UserRole] = mapped_column(SqlEnum(UserRole), nullable=False, default=UserRole.homeowner)
    is_active: Mapped[bool] = mapped_column(Boolean, default=True)
    email_verified: Mapped[bool] = mapped_column(Boolean, default=False)
    phone: Mapped[Optional[str]] = mapped_column(String(40), nullable=True)
    estate_id: Mapped[Optional[str]] = mapped_column(String(36), ForeignKey("estates.id"), nullable=True, index=True)
    gate_id: Mapped[Optional[str]] = mapped_column(String(36), nullable=True, index=True)
    referral_code: Mapped[str] = mapped_column(String(24), unique=True, nullable=False, index=True, default=lambda: f"QR{uuid.uuid4().hex[:8].upper()}")
    referred_by_user_id: Mapped[Optional[str]] = mapped_column(String(36), ForeignKey("users.id"), nullable=True, index=True)
    referral_earnings: Mapped[int] = mapped_column(Integer, default=0)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utc_now)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=utc_now, onupdate=utc_now)

    device_sessions = relationship("DeviceSession", back_populates="user", cascade="all, delete-orphan")
