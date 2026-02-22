import uuid
from datetime import datetime

from sqlalchemy import DateTime, ForeignKey, Integer, String
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base


class ReferralReward(Base):
    __tablename__ = "referral_rewards"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=lambda: str(uuid.uuid4()))
    referrer_user_id: Mapped[str] = mapped_column(String(36), ForeignKey("users.id"), nullable=False, index=True)
    referred_user_id: Mapped[str] = mapped_column(String(36), ForeignKey("users.id"), nullable=False, unique=True, index=True)
    plan_id: Mapped[str] = mapped_column(String(50), nullable=False)
    reward_amount: Mapped[int] = mapped_column(Integer, nullable=False, default=2000)
    currency: Mapped[str] = mapped_column(String(10), nullable=False, default="NGN")
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)
