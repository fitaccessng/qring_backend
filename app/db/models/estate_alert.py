from __future__ import annotations

import uuid
from datetime import datetime
from enum import Enum
from typing import Optional

from sqlalchemy import DateTime, Enum as SqlEnum, ForeignKey, Numeric, String, Text
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base


class EstateAlertType(str, Enum):
    notice = "notice"
    payment_request = "payment_request"
    meeting = "meeting"
    maintenance_request = "maintenance_request"
    poll = "poll"


class HomeownerPaymentStatus(str, Enum):
    pending = "pending"
    paid = "paid"
    failed = "failed"


class EstateAlert(Base):
    __tablename__ = "estate_alerts"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=lambda: str(uuid.uuid4()))
    estate_id: Mapped[str] = mapped_column(String(36), ForeignKey("estates.id"), nullable=False, index=True)
    title: Mapped[str] = mapped_column(String(180), nullable=False)
    description: Mapped[str] = mapped_column(Text, default="")
    alert_type: Mapped[EstateAlertType] = mapped_column(
        SqlEnum(EstateAlertType),
        nullable=False,
        default=EstateAlertType.notice,
    )
    amount_due: Mapped[Optional[float]] = mapped_column(Numeric(12, 2), nullable=True)
    due_date: Mapped[Optional[datetime]] = mapped_column(DateTime, nullable=True)
    poll_options: Mapped[Optional[str]] = mapped_column(Text, default="")
    target_homeowner_ids: Mapped[Optional[str]] = mapped_column(Text, default="")
    maintenance_status: Mapped[Optional[str]] = mapped_column(String(20), default="pending")
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)


class HomeownerPayment(Base):
    __tablename__ = "homeowner_payments"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=lambda: str(uuid.uuid4()))
    estate_alert_id: Mapped[str] = mapped_column(
        String(36),
        ForeignKey("estate_alerts.id"),
        nullable=False,
        index=True,
    )
    homeowner_id: Mapped[str] = mapped_column(String(36), ForeignKey("users.id"), nullable=False, index=True)
    status: Mapped[HomeownerPaymentStatus] = mapped_column(
        SqlEnum(HomeownerPaymentStatus),
        nullable=False,
        default=HomeownerPaymentStatus.pending,
    )
    amount_paid: Mapped[float] = mapped_column(Numeric(12, 2), default=0)
    payment_method: Mapped[Optional[str]] = mapped_column(String(40), nullable=True)
    payment_provider_reference: Mapped[Optional[str]] = mapped_column(String(120), nullable=True, index=True)
    payment_note: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    payment_proof_url: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    receipt_url: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    paid_at: Mapped[Optional[datetime]] = mapped_column(DateTime, nullable=True)
    reminder_sent_at: Mapped[Optional[datetime]] = mapped_column(DateTime, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)
