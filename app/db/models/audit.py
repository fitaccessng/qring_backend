from __future__ import annotations

import uuid
from datetime import datetime
from typing import Optional

from sqlalchemy import DateTime, ForeignKey, String, Text
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base


class AuditLog(Base):
    __tablename__ = "audit_logs"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=lambda: str(uuid.uuid4()))
    actor_user_id: Mapped[Optional[str]] = mapped_column(String(36), ForeignKey("users.id"), nullable=True, index=True)
    action: Mapped[str] = mapped_column(String(80), nullable=False, index=True)
    resource_type: Mapped[str] = mapped_column(String(80), nullable=False, index=True)
    resource_id: Mapped[Optional[str]] = mapped_column(String(64), nullable=True, index=True)
    meta_json: Mapped[str] = mapped_column(Text, default="{}")
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow, index=True)


class GateLog(Base):
    __tablename__ = "gate_logs"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=lambda: str(uuid.uuid4()))
    visitor_session_id: Mapped[Optional[str]] = mapped_column(String(36), ForeignKey("visitor_sessions.id"), nullable=True, index=True)
    estate_id: Mapped[Optional[str]] = mapped_column(String(36), ForeignKey("estates.id"), nullable=True, index=True)
    home_id: Mapped[Optional[str]] = mapped_column(String(36), ForeignKey("homes.id"), nullable=True, index=True)
    gate_id: Mapped[Optional[str]] = mapped_column(String(120), nullable=True, index=True)
    actor_user_id: Mapped[Optional[str]] = mapped_column(String(36), ForeignKey("users.id"), nullable=True, index=True)
    actor_role: Mapped[Optional[str]] = mapped_column(String(30), nullable=True, index=True)
    action: Mapped[str] = mapped_column(String(80), nullable=False, index=True)
    resulting_status: Mapped[Optional[str]] = mapped_column(String(40), nullable=True, index=True)
    notes: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    meta_json: Mapped[str] = mapped_column(Text, default="{}")
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow, index=True)
