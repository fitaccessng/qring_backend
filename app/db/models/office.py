from __future__ import annotations

import uuid
from datetime import datetime
from typing import Optional

from sqlalchemy import Boolean, DateTime, ForeignKey, Integer, String, Text
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.core.time import utc_now
from app.db.base import Base


class Office(Base):
    __tablename__ = "offices"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=lambda: str(uuid.uuid4()))
    company_name: Mapped[str] = mapped_column(String(160), nullable=False)
    business_email: Mapped[str] = mapped_column(String(255), nullable=False, index=True)
    phone_number: Mapped[Optional[str]] = mapped_column(String(40), nullable=True)
    office_address: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    country: Mapped[Optional[str]] = mapped_column(String(80), nullable=True)
    state: Mapped[Optional[str]] = mapped_column(String(80), nullable=True)
    city: Mapped[Optional[str]] = mapped_column(String(80), nullable=True)
    office_size: Mapped[Optional[str]] = mapped_column(String(80), nullable=True)
    industry: Mapped[Optional[str]] = mapped_column(String(120), nullable=True)
    employee_count: Mapped[int] = mapped_column(Integer, default=0)
    timezone: Mapped[Optional[str]] = mapped_column(String(80), nullable=True)
    administrator_user_id: Mapped[str] = mapped_column(String(36), ForeignKey("users.id"), nullable=False, index=True)
    reception_home_id: Mapped[Optional[str]] = mapped_column(String(36), ForeignKey("homes.id"), nullable=True, index=True)
    reception_door_id: Mapped[Optional[str]] = mapped_column(String(36), ForeignKey("doors.id"), nullable=True, index=True)
    qr_id: Mapped[str] = mapped_column(String(64), unique=True, nullable=False, index=True)
    active: Mapped[bool] = mapped_column(Boolean, default=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utc_now, index=True)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=utc_now, onupdate=utc_now)

    homes = relationship("Home", back_populates="office")
    members = relationship("OfficeMember", back_populates="office", cascade="all, delete-orphan")


class OfficeMember(Base):
    __tablename__ = "office_members"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=lambda: str(uuid.uuid4()))
    office_id: Mapped[str] = mapped_column(String(36), ForeignKey("offices.id"), nullable=False, index=True)
    user_id: Mapped[Optional[str]] = mapped_column(String(36), ForeignKey("users.id"), nullable=True, index=True)
    full_name: Mapped[str] = mapped_column(String(160), nullable=False)
    role_label: Mapped[str] = mapped_column(String(80), default="employee")
    department: Mapped[Optional[str]] = mapped_column(String(120), nullable=True)
    floor: Mapped[Optional[str]] = mapped_column(String(40), nullable=True)
    extension: Mapped[Optional[str]] = mapped_column(String(40), nullable=True)
    availability_status: Mapped[str] = mapped_column(String(40), default="available")
    status: Mapped[str] = mapped_column(String(40), default="active")
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utc_now, index=True)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=utc_now, onupdate=utc_now)

    office = relationship("Office", back_populates="members")
