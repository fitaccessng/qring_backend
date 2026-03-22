from __future__ import annotations

import uuid
from datetime import datetime
from typing import Optional

from sqlalchemy import Boolean, DateTime, ForeignKey, Integer, Numeric, String, Text
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base
from app.core.time import utc_now


class SubscriptionPlan(Base):
    __tablename__ = "subscription_plans"

    id: Mapped[str] = mapped_column(String(50), primary_key=True)
    name: Mapped[str] = mapped_column(String(120), nullable=False)
    amount: Mapped[int] = mapped_column(Integer, default=0)
    currency: Mapped[str] = mapped_column(String(10), default="NGN")
    audience: Mapped[str] = mapped_column(String(30), default="homeowner")
    max_doors: Mapped[int] = mapped_column(Integer, default=1)
    max_qr_codes: Mapped[int] = mapped_column(Integer, default=1)
    max_admins: Mapped[int] = mapped_column(Integer, default=1)
    duration_days: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)
    trial_days: Mapped[int] = mapped_column(Integer, default=0)
    self_serve: Mapped[bool] = mapped_column(Boolean, default=True)
    manual_activation_required: Mapped[bool] = mapped_column(Boolean, default=False)
    hidden: Mapped[bool] = mapped_column(Boolean, default=False)
    enabled_features: Mapped[str] = mapped_column(Text, default="[]")
    restrictions: Mapped[str] = mapped_column(Text, default="[]")
    active: Mapped[bool] = mapped_column(Boolean, default=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utc_now)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime,
        default=utc_now,
        onupdate=utc_now,
    )


class PaymentPurpose(Base):
    __tablename__ = "payment_purposes"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=lambda: str(uuid.uuid4()))
    name: Mapped[str] = mapped_column(String(120), nullable=False, unique=True)
    description: Mapped[str] = mapped_column(Text, default="")
    account_info: Mapped[str] = mapped_column(Text, default="")
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utc_now)


class Subscription(Base):
    __tablename__ = "subscriptions"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=lambda: str(uuid.uuid4()))
    user_id: Mapped[str] = mapped_column(String(36), ForeignKey("users.id"), nullable=False, index=True)
    plan: Mapped[str] = mapped_column(String(50), nullable=False)
    status: Mapped[str] = mapped_column(String(30), default="inactive")
    payment_status: Mapped[str] = mapped_column(String(30), default="unpaid")
    billing_cycle: Mapped[str] = mapped_column(String(20), default="monthly")
    tenant_type: Mapped[str] = mapped_column(String(20), default="homeowner")
    tenant_id: Mapped[Optional[str]] = mapped_column(String(36), nullable=True, index=True)
    billing_scope: Mapped[str] = mapped_column(String(20), default="homeowner")
    auto_renew: Mapped[bool] = mapped_column(Boolean, default=True)
    cancel_at_period_end: Mapped[bool] = mapped_column(Boolean, default=False)
    grace_days: Mapped[int] = mapped_column(Integer, default=5)
    grace_ends_at: Mapped[Optional[datetime]] = mapped_column(DateTime, nullable=True)
    warning_phase: Mapped[Optional[str]] = mapped_column(String(20), nullable=True)
    suspension_reason: Mapped[Optional[str]] = mapped_column(String(50), nullable=True)
    last_payment_attempt_at: Mapped[Optional[datetime]] = mapped_column(DateTime, nullable=True)
    last_successful_payment_at: Mapped[Optional[datetime]] = mapped_column(DateTime, nullable=True)
    amount_due: Mapped[float] = mapped_column(Numeric(12, 2), default=0)
    amount_paid: Mapped[float] = mapped_column(Numeric(12, 2), default=0)
    timezone: Mapped[str] = mapped_column(String(64), default="Africa/Lagos")
    starts_at: Mapped[Optional[datetime]] = mapped_column(DateTime, nullable=True)
    ends_at: Mapped[Optional[datetime]] = mapped_column(DateTime, nullable=True)
    trial_started_at: Mapped[Optional[datetime]] = mapped_column(DateTime, nullable=True)
    trial_ends_at: Mapped[Optional[datetime]] = mapped_column(DateTime, nullable=True)


class HomeownerWallet(Base):
    __tablename__ = "homeowner_wallets"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=lambda: str(uuid.uuid4()))
    user_id: Mapped[str] = mapped_column(String(36), ForeignKey("users.id"), nullable=False, unique=True, index=True)
    balance: Mapped[float] = mapped_column(Numeric(12, 2), default=0)
    currency: Mapped[str] = mapped_column(String(10), default="NGN")
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utc_now)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime,
        default=utc_now,
        onupdate=utc_now,
    )


class HomeownerWalletTransaction(Base):
    __tablename__ = "homeowner_wallet_transactions"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=lambda: str(uuid.uuid4()))
    user_id: Mapped[str] = mapped_column(String(36), ForeignKey("users.id"), nullable=False, index=True)
    amount: Mapped[float] = mapped_column(Numeric(12, 2), default=0)
    balance_after: Mapped[float] = mapped_column(Numeric(12, 2), default=0)
    currency: Mapped[str] = mapped_column(String(10), default="NGN")
    type: Mapped[str] = mapped_column(String(40), default="fund")
    note: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utc_now)
