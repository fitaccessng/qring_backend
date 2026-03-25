from __future__ import annotations

import hashlib
import secrets
import uuid
from datetime import datetime
from enum import Enum
from typing import Optional

from sqlalchemy import DateTime, Enum as SqlEnum, ForeignKey, String, Text
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.core.time import utc_now
from app.db.base import Base


class UserTokenType(str, Enum):
    email_verify = "email_verify"
    password_reset = "password_reset"


def hash_user_token(token: str) -> str:
    # Store only a hash so DB leaks don't become account-takeover leaks.
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


def generate_user_token() -> str:
    # 256-bit random token (urlsafe)
    return secrets.token_urlsafe(32)


class UserToken(Base):
    __tablename__ = "user_tokens"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=lambda: str(uuid.uuid4()))
    user_id: Mapped[str] = mapped_column(String(36), ForeignKey("users.id"), nullable=False, index=True)
    token_type: Mapped[UserTokenType] = mapped_column(SqlEnum(UserTokenType), nullable=False, index=True)
    token_hash: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    expires_at: Mapped[datetime] = mapped_column(DateTime, nullable=False, index=True)
    used_at: Mapped[Optional[datetime]] = mapped_column(DateTime, nullable=True, index=True)
    metadata_json: Mapped[str] = mapped_column(Text, default="{}")
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utc_now, index=True)

    user = relationship("User")

