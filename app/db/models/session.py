from __future__ import annotations

import uuid
from datetime import datetime
from typing import Optional

from sqlalchemy import Boolean, DateTime, ForeignKey, Integer, String, Text
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base


class VisitorSession(Base):
    __tablename__ = "visitor_sessions"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=lambda: str(uuid.uuid4()))
    request_id: Mapped[Optional[str]] = mapped_column(String(64), nullable=True, index=True)
    qr_id: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    home_id: Mapped[str] = mapped_column(String(36), ForeignKey("homes.id"), nullable=False, index=True)
    door_id: Mapped[str] = mapped_column(String(36), ForeignKey("doors.id"), nullable=False, index=True)
    homeowner_id: Mapped[str] = mapped_column(String(36), ForeignKey("users.id"), nullable=False, index=True)
    appointment_id: Mapped[Optional[str]] = mapped_column(String(36), ForeignKey("appointments.id"), nullable=True, index=True)
    visitor_label: Mapped[str] = mapped_column(String(120), default="Visitor")
    status: Mapped[str] = mapped_column(String(40), default="pending")
    visitor_type: Mapped[str] = mapped_column(String(20), default="guest", index=True)
    request_source: Mapped[str] = mapped_column(String(30), default="visitor_qr", index=True)
    creator_role: Mapped[str] = mapped_column(String(20), default="visitor", index=True)
    visitor_phone: Mapped[Optional[str]] = mapped_column(String(40), nullable=True)
    purpose: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    photo_url: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    estate_id: Mapped[Optional[str]] = mapped_column(String(36), ForeignKey("estates.id"), nullable=True, index=True)
    gate_id: Mapped[Optional[str]] = mapped_column(String(36), nullable=True, index=True)
    handled_by_security_id: Mapped[Optional[str]] = mapped_column(String(36), ForeignKey("users.id"), nullable=True, index=True)
    communication_status: Mapped[str] = mapped_column(String(30), default="none", index=True)
    preferred_communication_channel: Mapped[Optional[str]] = mapped_column(String(20), nullable=True)
    preferred_communication_target: Mapped[Optional[str]] = mapped_column(String(20), nullable=True)
    gate_status: Mapped[str] = mapped_column(String(30), default="waiting", index=True)
    trust_status: Mapped[str] = mapped_column(String(20), default="new", index=True)
    trust_score: Mapped[int] = mapped_column(Integer, default=0)
    total_visits_snapshot: Mapped[int] = mapped_column(Integer, default=0)
    approvals_count_snapshot: Mapped[int] = mapped_column(Integer, default=0)
    rejections_count_snapshot: Mapped[int] = mapped_column(Integer, default=0)
    unique_houses_visited_snapshot: Mapped[int] = mapped_column(Integer, default=0)
    repeat_visits_to_home_snapshot: Mapped[int] = mapped_column(Integer, default=0)
    auto_approved: Mapped[bool] = mapped_column(Boolean, default=False)
    auto_approve_suggested: Mapped[bool] = mapped_column(Boolean, default=False)
    pre_approved: Mapped[bool] = mapped_column(Boolean, default=False)
    pre_approved_reason: Mapped[Optional[str]] = mapped_column(String(160), nullable=True)
    delivery_option: Mapped[Optional[str]] = mapped_column(String(40), nullable=True)
    delivery_drop_off_allowed: Mapped[bool] = mapped_column(Boolean, default=False)
    suspicious_flag: Mapped[bool] = mapped_column(Boolean, default=False)
    suspicious_reason: Mapped[Optional[str]] = mapped_column(String(200), nullable=True)
    received_by_security_at: Mapped[Optional[datetime]] = mapped_column(DateTime, nullable=True)
    forwarded_to_homeowner_at: Mapped[Optional[datetime]] = mapped_column(DateTime, nullable=True)
    homeowner_decision_at: Mapped[Optional[datetime]] = mapped_column(DateTime, nullable=True)
    gate_action_at: Mapped[Optional[datetime]] = mapped_column(DateTime, nullable=True)
    state_updated_at: Mapped[Optional[datetime]] = mapped_column(DateTime, nullable=True)
    started_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)
    ended_at: Mapped[Optional[datetime]] = mapped_column(DateTime, nullable=True)


class Message(Base):
    __tablename__ = "messages"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=lambda: str(uuid.uuid4()))
    session_id: Mapped[str] = mapped_column(String(36), ForeignKey("visitor_sessions.id"), nullable=False, index=True)
    sender_type: Mapped[str] = mapped_column(String(20), nullable=False)
    sender_id: Mapped[Optional[str]] = mapped_column(String(36), ForeignKey("users.id"), nullable=True, index=True)
    receiver_id: Mapped[Optional[str]] = mapped_column(String(36), ForeignKey("users.id"), nullable=True, index=True)
    body: Mapped[str] = mapped_column(Text, nullable=False)
    read_by_homeowner_at: Mapped[Optional[datetime]] = mapped_column(DateTime, nullable=True, index=True)
    read_by_security_at: Mapped[Optional[datetime]] = mapped_column(DateTime, nullable=True, index=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)


class Notification(Base):
    __tablename__ = "notifications"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=lambda: str(uuid.uuid4()))
    user_id: Mapped[str] = mapped_column(String(36), ForeignKey("users.id"), nullable=False, index=True)
    kind: Mapped[str] = mapped_column(String(50), nullable=False)
    payload: Mapped[str] = mapped_column(Text, nullable=False)
    read_at: Mapped[Optional[datetime]] = mapped_column(DateTime, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)


class CallSession(Base):
    __tablename__ = "call_sessions"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=lambda: str(uuid.uuid4()))
    appointment_id: Mapped[Optional[str]] = mapped_column(
        String(36), ForeignKey("appointments.id"), nullable=True, index=True
    )
    visitor_session_id: Mapped[Optional[str]] = mapped_column(
        String(36), ForeignKey("visitor_sessions.id"), nullable=True, index=True
    )
    security_user_id: Mapped[Optional[str]] = mapped_column(String(36), ForeignKey("users.id"), nullable=True, index=True)
    caller_id: Mapped[Optional[str]] = mapped_column(String(36), ForeignKey("users.id"), nullable=True, index=True)
    receiver_id: Mapped[Optional[str]] = mapped_column(String(36), ForeignKey("users.id"), nullable=True, index=True)
    call_type: Mapped[str] = mapped_column(String(20), default="audio", index=True)
    room_name: Mapped[str] = mapped_column(String(160), nullable=False, unique=True, index=True)
    visitor_id: Mapped[str] = mapped_column(String(120), nullable=False, index=True)
    homeowner_id: Mapped[str] = mapped_column(String(36), ForeignKey("users.id"), nullable=False, index=True)
    status: Mapped[str] = mapped_column(String(20), default="pending", index=True)
    visitor_request_id: Mapped[Optional[str]] = mapped_column(String(64), nullable=True, index=True)
    initiated_by_role: Mapped[Optional[str]] = mapped_column(String(20), nullable=True, index=True)
    answered_at: Mapped[Optional[datetime]] = mapped_column(DateTime, nullable=True, index=True)
    ended_reason: Mapped[Optional[str]] = mapped_column(String(40), nullable=True, index=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow, index=True)
    ended_at: Mapped[Optional[datetime]] = mapped_column(DateTime, nullable=True, index=True)
