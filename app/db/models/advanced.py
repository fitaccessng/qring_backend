from __future__ import annotations

import uuid
from datetime import datetime
from typing import Optional

from sqlalchemy import Boolean, DateTime, ForeignKey, Integer, String, Text, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base
from app.core.time import utc_now


class VisitorSnapshotAudit(Base):
    __tablename__ = "visitor_snapshot_audits"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=lambda: str(uuid.uuid4()))
    homeowner_id: Mapped[str] = mapped_column(String(36), ForeignKey("users.id"), index=True)
    visitor_session_id: Mapped[Optional[str]] = mapped_column(
        String(36), ForeignKey("visitor_sessions.id"), nullable=True, index=True
    )
    appointment_id: Mapped[Optional[str]] = mapped_column(String(36), ForeignKey("appointments.id"), nullable=True, index=True)
    media_type: Mapped[str] = mapped_column(String(20), default="photo")
    media_path: Mapped[str] = mapped_column(Text, nullable=False)
    media_sha256: Mapped[Optional[str]] = mapped_column(String(128), nullable=True)
    source: Mapped[str] = mapped_column(String(30), default="visitor_device")
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utc_now, index=True)


class VisitorRecognitionProfile(Base):
    __tablename__ = "visitor_recognition_profiles"
    __table_args__ = (
        UniqueConstraint("homeowner_id", "visitor_key_hash", name="uq_recognition_homeowner_key"),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=lambda: str(uuid.uuid4()))
    homeowner_id: Mapped[str] = mapped_column(String(36), ForeignKey("users.id"), index=True)
    display_name: Mapped[str] = mapped_column(String(120), default="Visitor")
    visitor_key_hash: Mapped[str] = mapped_column(String(128), index=True)
    encrypted_template: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    visits_count: Mapped[int] = mapped_column(Integer, default=1)
    last_seen_at: Mapped[Optional[datetime]] = mapped_column(DateTime, nullable=True, index=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utc_now, index=True)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=utc_now, onupdate=utc_now)


class SplitBill(Base):
    __tablename__ = "split_bills"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=lambda: str(uuid.uuid4()))
    owner_user_id: Mapped[str] = mapped_column(String(36), ForeignKey("users.id"), index=True)
    title: Mapped[str] = mapped_column(String(150), nullable=False)
    description: Mapped[str] = mapped_column(Text, default="")
    total_amount_kobo: Mapped[int] = mapped_column(Integer, default=0)
    currency: Mapped[str] = mapped_column(String(10), default="NGN")
    status: Mapped[str] = mapped_column(String(30), default="open", index=True)
    due_at: Mapped[Optional[datetime]] = mapped_column(DateTime, nullable=True, index=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utc_now, index=True)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=utc_now, onupdate=utc_now)


class SplitContribution(Base):
    __tablename__ = "split_contributions"
    __table_args__ = (
        UniqueConstraint("split_bill_id", "contributor_user_id", name="uq_split_contributor"),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=lambda: str(uuid.uuid4()))
    split_bill_id: Mapped[str] = mapped_column(String(36), ForeignKey("split_bills.id"), index=True)
    contributor_user_id: Mapped[str] = mapped_column(String(36), ForeignKey("users.id"), index=True)
    pledged_amount_kobo: Mapped[int] = mapped_column(Integer, default=0)
    paid_amount_kobo: Mapped[int] = mapped_column(Integer, default=0)
    status: Mapped[str] = mapped_column(String(30), default="pending", index=True)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=utc_now, onupdate=utc_now)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utc_now, index=True)


class DigitalReceipt(Base):
    __tablename__ = "digital_receipts"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=lambda: str(uuid.uuid4()))
    owner_user_id: Mapped[str] = mapped_column(String(36), ForeignKey("users.id"), index=True)
    reference: Mapped[str] = mapped_column(String(120), index=True)
    amount_kobo: Mapped[int] = mapped_column(Integer, default=0)
    currency: Mapped[str] = mapped_column(String(10), default="NGN")
    purpose: Mapped[str] = mapped_column(String(80), default="general")
    payload_json: Mapped[str] = mapped_column(Text, default="{}")
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utc_now, index=True)


class ThreatAlertLog(Base):
    __tablename__ = "threat_alert_logs"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=lambda: str(uuid.uuid4()))
    homeowner_id: Mapped[str] = mapped_column(String(36), ForeignKey("users.id"), index=True)
    visitor_session_id: Mapped[Optional[str]] = mapped_column(
        String(36), ForeignKey("visitor_sessions.id"), nullable=True, index=True
    )
    risk_score: Mapped[int] = mapped_column(Integer, default=0)
    category: Mapped[str] = mapped_column(String(80), default="unknown_face")
    message: Mapped[str] = mapped_column(Text, default="")
    snapshot_audit_id: Mapped[Optional[str]] = mapped_column(
        String(36), ForeignKey("visitor_snapshot_audits.id"), nullable=True, index=True
    )
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utc_now, index=True)


class EmergencySignal(Base):
    __tablename__ = "emergency_signals"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=lambda: str(uuid.uuid4()))
    requester_user_id: Mapped[str] = mapped_column(String(36), ForeignKey("users.id"), index=True)
    scope: Mapped[str] = mapped_column(String(40), default="estate")
    message: Mapped[str] = mapped_column(Text, default="")
    notify_sms: Mapped[bool] = mapped_column(Boolean, default=False)
    resolved_at: Mapped[Optional[datetime]] = mapped_column(DateTime, nullable=True, index=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utc_now, index=True)


class CommunityPost(Base):
    __tablename__ = "community_posts"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=lambda: str(uuid.uuid4()))
    author_user_id: Mapped[str] = mapped_column(String(36), ForeignKey("users.id"), index=True)
    audience_scope: Mapped[str] = mapped_column(String(40), default="estate")
    title: Mapped[str] = mapped_column(String(160), nullable=False)
    body: Mapped[str] = mapped_column(Text, default="")
    tag: Mapped[str] = mapped_column(String(40), default="notice")
    pinned: Mapped[bool] = mapped_column(Boolean, default=False, index=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utc_now, index=True)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=utc_now, onupdate=utc_now)


class CommunityPostRead(Base):
    __tablename__ = "community_post_reads"
    __table_args__ = (
        UniqueConstraint("post_id", "reader_user_id", name="uq_community_post_read"),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=lambda: str(uuid.uuid4()))
    post_id: Mapped[str] = mapped_column(String(36), ForeignKey("community_posts.id"), index=True)
    reader_user_id: Mapped[str] = mapped_column(String(36), ForeignKey("users.id"), index=True)
    read_at: Mapped[datetime] = mapped_column(DateTime, default=utc_now, index=True)


class WeeklySummaryLog(Base):
    __tablename__ = "weekly_summary_logs"
    __table_args__ = (
        UniqueConstraint("user_id", "week_start_iso", name="uq_weekly_summary_user_week"),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=lambda: str(uuid.uuid4()))
    user_id: Mapped[str] = mapped_column(String(36), ForeignKey("users.id"), index=True)
    week_start_iso: Mapped[str] = mapped_column(String(30), index=True)
    summary_json: Mapped[str] = mapped_column(Text, default="{}")
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utc_now, index=True)


class PushSubscription(Base):
    __tablename__ = "push_subscriptions"
    __table_args__ = (
        UniqueConstraint("user_id", "provider", "endpoint", name="uq_push_subscription_endpoint"),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=lambda: str(uuid.uuid4()))
    user_id: Mapped[str] = mapped_column(String(36), ForeignKey("users.id"), index=True)
    provider: Mapped[str] = mapped_column(String(20), default="fcm", index=True)
    endpoint: Mapped[str] = mapped_column(Text, default="")
    token: Mapped[Optional[str]] = mapped_column(String(255), nullable=True, index=True)
    keys_json: Mapped[str] = mapped_column(Text, default="{}")
    is_active: Mapped[bool] = mapped_column(Boolean, default=True, index=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utc_now, index=True)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=utc_now, onupdate=utc_now)
