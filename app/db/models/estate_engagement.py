from __future__ import annotations

import uuid
from datetime import datetime
from enum import Enum

from sqlalchemy import DateTime, Enum as SqlEnum, ForeignKey, Integer, String, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base


class MeetingResponseType(str, Enum):
    attending = "attending"
    not_attending = "not_attending"
    maybe = "maybe"


class EstateMeetingResponse(Base):
    __tablename__ = "estate_meeting_responses"
    __table_args__ = (
        UniqueConstraint("estate_alert_id", "homeowner_id", name="uq_estate_meeting_response"),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=lambda: str(uuid.uuid4()))
    estate_alert_id: Mapped[str] = mapped_column(String(36), ForeignKey("estate_alerts.id"), nullable=False, index=True)
    homeowner_id: Mapped[str] = mapped_column(String(36), ForeignKey("users.id"), nullable=False, index=True)
    response: Mapped[MeetingResponseType] = mapped_column(SqlEnum(MeetingResponseType), nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)


class EstatePollVote(Base):
    __tablename__ = "estate_poll_votes"
    __table_args__ = (
        UniqueConstraint("estate_alert_id", "homeowner_id", name="uq_estate_poll_vote"),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=lambda: str(uuid.uuid4()))
    estate_alert_id: Mapped[str] = mapped_column(String(36), ForeignKey("estate_alerts.id"), nullable=False, index=True)
    homeowner_id: Mapped[str] = mapped_column(String(36), ForeignKey("users.id"), nullable=False, index=True)
    option_index: Mapped[int] = mapped_column(Integer, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)
