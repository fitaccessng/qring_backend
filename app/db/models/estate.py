from __future__ import annotations

import uuid
from datetime import datetime
from typing import Optional

from sqlalchemy import Boolean, DateTime, ForeignKey, Integer, String
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.db.base import Base


class Estate(Base):
    __tablename__ = "estates"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=lambda: str(uuid.uuid4()))
    name: Mapped[str] = mapped_column(String(120), nullable=False)
    owner_id: Mapped[str] = mapped_column(String(36), ForeignKey("users.id"), nullable=False)
    reminder_frequency_days: Mapped[int] = mapped_column(Integer, default=1)
    security_can_approve_without_homeowner: Mapped[bool] = mapped_column(Boolean, default=False)
    security_must_notify_homeowner: Mapped[bool] = mapped_column(Boolean, default=True)
    security_require_photo_verification: Mapped[bool] = mapped_column(Boolean, default=False)
    security_require_call_before_approval: Mapped[bool] = mapped_column(Boolean, default=False)
    auto_approve_trusted_visitors: Mapped[bool] = mapped_column(Boolean, default=False)
    suspicious_visit_window_minutes: Mapped[int] = mapped_column(Integer, default=20)
    suspicious_house_threshold: Mapped[int] = mapped_column(Integer, default=3)
    suspicious_rejection_threshold: Mapped[int] = mapped_column(Integer, default=2)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)

    homes = relationship("Home", back_populates="estate", cascade="all, delete-orphan")


class Home(Base):
    __tablename__ = "homes"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=lambda: str(uuid.uuid4()))
    name: Mapped[str] = mapped_column(String(120), nullable=False)
    estate_id: Mapped[Optional[str]] = mapped_column(String(36), ForeignKey("estates.id"), nullable=True)
    homeowner_id: Mapped[str] = mapped_column(String(36), ForeignKey("users.id"), nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)

    estate = relationship("Estate", back_populates="homes")
    doors = relationship("Door", back_populates="home", cascade="all, delete-orphan")


class Door(Base):
    __tablename__ = "doors"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=lambda: str(uuid.uuid4()))
    name: Mapped[str] = mapped_column(String(120), nullable=False)
    home_id: Mapped[str] = mapped_column(String(36), ForeignKey("homes.id"), nullable=False, index=True)
    is_active: Mapped[str] = mapped_column(String(10), default="online")
    gate_label: Mapped[Optional[str]] = mapped_column(String(120), nullable=True)

    home = relationship("Home", back_populates="doors")
