from __future__ import annotations

import uuid
from datetime import datetime
from enum import Enum
from typing import Optional

from sqlalchemy import Boolean, DateTime, Enum as SqlEnum, ForeignKey, Integer, Numeric, String, Text
from sqlalchemy.orm import Mapped, mapped_column

from app.core.time import utc_now
from app.db.base import Base


class EmergencyAlertType(str, Enum):
    panic = "panic"
    fire = "fire"
    break_in = "break_in"


class EmergencyAlertPriority(str, Enum):
    critical = "critical"
    high = "high"


class EmergencyAlertStatus(str, Enum):
    dispatched = "dispatched"
    acknowledged = "acknowledged"
    escalated = "escalated"
    resolved = "resolved"
    cancelled = "cancelled"


class AlertDeliveryStatus(str, Enum):
    queued = "queued"
    sent = "sent"
    received = "received"
    acknowledged = "acknowledged"
    failed = "failed"


class VisitorReportSeverity(str, Enum):
    low = "low"
    medium = "medium"
    high = "high"
    critical = "critical"


class VisitorReportStatus(str, Enum):
    pending_review = "pending_review"
    confirmed = "confirmed"
    dismissed = "dismissed"


class WatchlistRiskLevel(str, Enum):
    low = "low"
    medium = "medium"
    high = "high"
    critical = "critical"


class PanicMode(str, Enum):
    personal = "personal"
    estate = "estate"


class PanicEventStatus(str, Enum):
    active = "active"
    resolved = "resolved"


class EmergencyAlert(Base):
    __tablename__ = "emergency_alerts"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=lambda: str(uuid.uuid4()))
    estate_id: Mapped[str] = mapped_column(String(36), ForeignKey("estates.id"), nullable=False, index=True)
    home_id: Mapped[Optional[str]] = mapped_column(String(36), ForeignKey("homes.id"), nullable=True, index=True)
    user_id: Mapped[str] = mapped_column(String(36), ForeignKey("users.id"), nullable=False, index=True)
    acknowledged_by_user_id: Mapped[Optional[str]] = mapped_column(String(36), ForeignKey("users.id"), nullable=True, index=True)
    resolved_by_user_id: Mapped[Optional[str]] = mapped_column(String(36), ForeignKey("users.id"), nullable=True, index=True)
    alert_type: Mapped[EmergencyAlertType] = mapped_column(SqlEnum(EmergencyAlertType), nullable=False)
    priority: Mapped[EmergencyAlertPriority] = mapped_column(SqlEnum(EmergencyAlertPriority), nullable=False)
    status: Mapped[EmergencyAlertStatus] = mapped_column(
        SqlEnum(EmergencyAlertStatus),
        nullable=False,
        default=EmergencyAlertStatus.dispatched,
        index=True,
    )
    unit_label: Mapped[Optional[str]] = mapped_column(String(120), nullable=True)
    trigger_mode: Mapped[str] = mapped_column(String(24), default="hold", nullable=False)
    silent_trigger: Mapped[bool] = mapped_column(Boolean, default=False)
    offline_queued: Mapped[bool] = mapped_column(Boolean, default=False)
    cancel_window_seconds: Mapped[int] = mapped_column(Integer, default=8)
    cancel_expires_at: Mapped[Optional[datetime]] = mapped_column(DateTime, nullable=True)
    acknowledged_at: Mapped[Optional[datetime]] = mapped_column(DateTime, nullable=True)
    escalated_at: Mapped[Optional[datetime]] = mapped_column(DateTime, nullable=True)
    resolved_at: Mapped[Optional[datetime]] = mapped_column(DateTime, nullable=True)
    last_known_lat: Mapped[Optional[float]] = mapped_column(Numeric(10, 7), nullable=True)
    last_known_lng: Mapped[Optional[float]] = mapped_column(Numeric(10, 7), nullable=True)
    last_known_address: Mapped[Optional[str]] = mapped_column(String(255), nullable=True)
    last_known_source: Mapped[Optional[str]] = mapped_column(String(40), nullable=True)
    notes: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    triggered_at: Mapped[datetime] = mapped_column(DateTime, default=utc_now, index=True)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=utc_now, onupdate=utc_now)


class PanicEvent(Base):
    __tablename__ = "panic_events"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=lambda: str(uuid.uuid4()))
    user_id: Mapped[str] = mapped_column(String(36), ForeignKey("users.id"), nullable=False, index=True)
    estate_id: Mapped[Optional[str]] = mapped_column(String(36), ForeignKey("estates.id"), nullable=True, index=True)
    home_id: Mapped[Optional[str]] = mapped_column(String(36), ForeignKey("homes.id"), nullable=True, index=True)
    type: Mapped[str] = mapped_column(String(24), nullable=False, default="panic")
    mode: Mapped[PanicMode] = mapped_column(SqlEnum(PanicMode), nullable=False, default=PanicMode.personal)
    status: Mapped[PanicEventStatus] = mapped_column(
        SqlEnum(PanicEventStatus),
        nullable=False,
        default=PanicEventStatus.active,
        index=True,
    )
    acknowledged: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False, index=True)
    acknowledged_by_user_id: Mapped[Optional[str]] = mapped_column(String(36), ForeignKey("users.id"), nullable=True, index=True)
    resolved_by_user_id: Mapped[Optional[str]] = mapped_column(String(36), ForeignKey("users.id"), nullable=True, index=True)
    unit_label: Mapped[Optional[str]] = mapped_column(String(120), nullable=True)
    last_known_lat: Mapped[Optional[float]] = mapped_column(Numeric(10, 7), nullable=True)
    last_known_lng: Mapped[Optional[float]] = mapped_column(Numeric(10, 7), nullable=True)
    last_known_address: Mapped[Optional[str]] = mapped_column(String(255), nullable=True)
    last_known_source: Mapped[Optional[str]] = mapped_column(String(40), nullable=True)
    recipient_user_ids_json: Mapped[str] = mapped_column(Text, default="[]")
    responder_user_ids_json: Mapped[str] = mapped_column(Text, default="[]")
    responder_details_json: Mapped[str] = mapped_column(Text, default="[]")
    ignored_user_ids_json: Mapped[str] = mapped_column(Text, default="[]")
    false_report_user_ids_json: Mapped[str] = mapped_column(Text, default="[]")
    trigger_trust_score: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    incident_notes: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    response_started_at: Mapped[Optional[datetime]] = mapped_column(DateTime, nullable=True)
    last_responder_at: Mapped[Optional[datetime]] = mapped_column(DateTime, nullable=True)
    retry_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    last_dispatched_at: Mapped[datetime] = mapped_column(DateTime, default=utc_now)
    acknowledged_at: Mapped[Optional[datetime]] = mapped_column(DateTime, nullable=True)
    resolved_at: Mapped[Optional[datetime]] = mapped_column(DateTime, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utc_now, index=True)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=utc_now, onupdate=utc_now)


class EmergencyAlertEvent(Base):
    __tablename__ = "emergency_alert_events"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=lambda: str(uuid.uuid4()))
    alert_id: Mapped[str] = mapped_column(String(36), ForeignKey("emergency_alerts.id"), nullable=False, index=True)
    actor_user_id: Mapped[Optional[str]] = mapped_column(String(36), ForeignKey("users.id"), nullable=True, index=True)
    event_type: Mapped[str] = mapped_column(String(40), nullable=False, index=True)
    channel: Mapped[str] = mapped_column(String(20), nullable=False, default="internet")
    delivery_status: Mapped[AlertDeliveryStatus] = mapped_column(
        SqlEnum(AlertDeliveryStatus),
        nullable=False,
        default=AlertDeliveryStatus.sent,
    )
    target_type: Mapped[Optional[str]] = mapped_column(String(30), nullable=True)
    target_user_id: Mapped[Optional[str]] = mapped_column(String(36), ForeignKey("users.id"), nullable=True, index=True)
    target_label: Mapped[Optional[str]] = mapped_column(String(180), nullable=True)
    metadata_json: Mapped[str] = mapped_column(Text, default="{}")
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utc_now, index=True)


class VisitorReport(Base):
    __tablename__ = "visitor_reports"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=lambda: str(uuid.uuid4()))
    estate_id: Mapped[str] = mapped_column(String(36), ForeignKey("estates.id"), nullable=False, index=True)
    visitor_session_id: Mapped[Optional[str]] = mapped_column(String(36), ForeignKey("visitor_sessions.id"), nullable=True, index=True)
    reporter_user_id: Mapped[str] = mapped_column(String(36), ForeignKey("users.id"), nullable=False, index=True)
    host_user_id: Mapped[Optional[str]] = mapped_column(String(36), ForeignKey("users.id"), nullable=True, index=True)
    moderated_by_user_id: Mapped[Optional[str]] = mapped_column(String(36), ForeignKey("users.id"), nullable=True, index=True)
    reported_name: Mapped[str] = mapped_column(String(120), nullable=False, index=True)
    reported_phone: Mapped[Optional[str]] = mapped_column(String(40), nullable=True, index=True)
    reason: Mapped[str] = mapped_column(String(200), nullable=False)
    notes: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    severity: Mapped[VisitorReportSeverity] = mapped_column(
        SqlEnum(VisitorReportSeverity),
        nullable=False,
        default=VisitorReportSeverity.medium,
    )
    status: Mapped[VisitorReportStatus] = mapped_column(
        SqlEnum(VisitorReportStatus),
        nullable=False,
        default=VisitorReportStatus.pending_review,
        index=True,
    )
    occurrence_count: Mapped[int] = mapped_column(Integer, default=1)
    moderated_at: Mapped[Optional[datetime]] = mapped_column(DateTime, nullable=True)
    moderation_notes: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utc_now, index=True)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=utc_now, onupdate=utc_now)


class WatchlistEntry(Base):
    __tablename__ = "watchlist_entries"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=lambda: str(uuid.uuid4()))
    estate_id: Mapped[str] = mapped_column(String(36), ForeignKey("estates.id"), nullable=False, index=True)
    latest_report_id: Mapped[Optional[str]] = mapped_column(String(36), ForeignKey("visitor_reports.id"), nullable=True, index=True)
    display_name: Mapped[str] = mapped_column(String(120), nullable=False)
    display_phone: Mapped[Optional[str]] = mapped_column(String(40), nullable=True)
    normalized_name: Mapped[str] = mapped_column(String(120), nullable=False, index=True)
    normalized_phone: Mapped[Optional[str]] = mapped_column(String(40), nullable=True, index=True)
    risk_level: Mapped[WatchlistRiskLevel] = mapped_column(
        SqlEnum(WatchlistRiskLevel),
        nullable=False,
        default=WatchlistRiskLevel.medium,
        index=True,
    )
    report_count: Mapped[int] = mapped_column(Integer, default=1)
    active: Mapped[bool] = mapped_column(Boolean, default=True)
    blocked: Mapped[bool] = mapped_column(Boolean, default=False)
    auto_flagged: Mapped[bool] = mapped_column(Boolean, default=False)
    last_reported_at: Mapped[datetime] = mapped_column(DateTime, default=utc_now, index=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utc_now)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=utc_now, onupdate=utc_now)
