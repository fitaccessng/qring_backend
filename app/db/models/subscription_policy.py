from __future__ import annotations

import uuid
from datetime import datetime
from decimal import Decimal
from typing import Optional

from sqlalchemy import DateTime, ForeignKey, Numeric, String, Text
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base
from app.core.time import utc_now


class SubscriptionInvoice(Base):
    __tablename__ = "subscription_invoices"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=lambda: str(uuid.uuid4()))
    subscription_id: Mapped[str] = mapped_column(String(36), ForeignKey("subscriptions.id"), index=True, nullable=False)
    provider: Mapped[str] = mapped_column(String(20), default="paystack")
    provider_reference: Mapped[Optional[str]] = mapped_column(String(120), index=True, nullable=True)
    amount_expected: Mapped[Decimal] = mapped_column(Numeric(12, 2), default=0)
    amount_received: Mapped[Decimal] = mapped_column(Numeric(12, 2), default=0)
    currency: Mapped[str] = mapped_column(String(10), default="NGN")
    status: Mapped[str] = mapped_column(String(30), default="pending")
    due_at: Mapped[Optional[datetime]] = mapped_column(DateTime, nullable=True)
    paid_at: Mapped[Optional[datetime]] = mapped_column(DateTime, nullable=True)
    raw_payload: Mapped[str] = mapped_column(Text, default="{}")
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utc_now)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=utc_now, onupdate=utc_now)


class PaymentAttempt(Base):
    __tablename__ = "payment_attempts"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=lambda: str(uuid.uuid4()))
    subscription_id: Mapped[str] = mapped_column(String(36), ForeignKey("subscriptions.id"), index=True, nullable=False)
    invoice_id: Mapped[Optional[str]] = mapped_column(String(36), ForeignKey("subscription_invoices.id"), nullable=True)
    provider: Mapped[str] = mapped_column(String(20), default="paystack")
    provider_reference: Mapped[Optional[str]] = mapped_column(String(120), index=True, nullable=True)
    status: Mapped[str] = mapped_column(String(30), default="pending")
    amount: Mapped[Decimal] = mapped_column(Numeric(12, 2), default=0)
    failure_code: Mapped[Optional[str]] = mapped_column(String(50), nullable=True)
    failure_reason: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    attempted_at: Mapped[datetime] = mapped_column(DateTime, default=utc_now)
    confirmed_at: Mapped[Optional[datetime]] = mapped_column(DateTime, nullable=True)


class SubscriptionEvent(Base):
    __tablename__ = "subscription_events"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=lambda: str(uuid.uuid4()))
    subscription_id: Mapped[str] = mapped_column(String(36), ForeignKey("subscriptions.id"), index=True, nullable=False)
    event_type: Mapped[str] = mapped_column(String(50), index=True, nullable=False)
    old_status: Mapped[Optional[str]] = mapped_column(String(30), nullable=True)
    new_status: Mapped[Optional[str]] = mapped_column(String(30), nullable=True)
    metadata_json: Mapped[str] = mapped_column(Text, default="{}")
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utc_now)


class SubscriptionNotification(Base):
    __tablename__ = "subscription_notifications"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=lambda: str(uuid.uuid4()))
    subscription_id: Mapped[str] = mapped_column(String(36), ForeignKey("subscriptions.id"), index=True, nullable=False)
    channel: Mapped[str] = mapped_column(String(20), nullable=False)
    template_key: Mapped[str] = mapped_column(String(80), nullable=False)
    warning_phase: Mapped[Optional[str]] = mapped_column(String(20), nullable=True)
    scheduled_for: Mapped[Optional[datetime]] = mapped_column(DateTime, nullable=True)
    sent_at: Mapped[Optional[datetime]] = mapped_column(DateTime, nullable=True)
    delivery_status: Mapped[str] = mapped_column(String(30), default="pending")
    dedupe_key: Mapped[Optional[str]] = mapped_column(String(120), index=True, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utc_now)
