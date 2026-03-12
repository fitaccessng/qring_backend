import uuid
from datetime import datetime

from sqlalchemy import DateTime, ForeignKey, String
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base


class MaintenanceStatusAudit(Base):
    __tablename__ = "maintenance_status_audits"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=lambda: str(uuid.uuid4()))
    estate_alert_id: Mapped[str] = mapped_column(String(36), ForeignKey("estate_alerts.id"), nullable=False, index=True)
    estate_id: Mapped[str] = mapped_column(String(36), ForeignKey("estates.id"), nullable=False, index=True)
    changed_by_user_id: Mapped[str] = mapped_column(String(36), ForeignKey("users.id"), nullable=False, index=True)
    from_status: Mapped[str] = mapped_column(String(20), nullable=False)
    to_status: Mapped[str] = mapped_column(String(20), nullable=False)
    note: Mapped[str | None] = mapped_column(String(240), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)
