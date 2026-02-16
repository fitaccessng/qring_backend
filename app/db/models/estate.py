import uuid
from datetime import datetime

from sqlalchemy import DateTime, ForeignKey, String
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.db.base import Base


class Estate(Base):
    __tablename__ = "estates"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=lambda: str(uuid.uuid4()))
    name: Mapped[str] = mapped_column(String(120), nullable=False)
    owner_id: Mapped[str] = mapped_column(String(36), ForeignKey("users.id"), nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)

    homes = relationship("Home", back_populates="estate", cascade="all, delete-orphan")


class Home(Base):
    __tablename__ = "homes"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=lambda: str(uuid.uuid4()))
    name: Mapped[str] = mapped_column(String(120), nullable=False)
    estate_id: Mapped[str | None] = mapped_column(String(36), ForeignKey("estates.id"), nullable=True)
    homeowner_id: Mapped[str] = mapped_column(String(36), ForeignKey("users.id"), nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)

    estate = relationship("Estate", back_populates="homes")
    doors = relationship("Door", back_populates="home", cascade="all, delete-orphan")


class Door(Base):
    __tablename__ = "doors"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=lambda: str(uuid.uuid4()))
    name: Mapped[str] = mapped_column(String(120), nullable=False)
    home_id: Mapped[str] = mapped_column(String(36), ForeignKey("homes.id"), nullable=False, index=True)
    is_active: Mapped[str] = mapped_column(String(10), default="online")

    home = relationship("Home", back_populates="doors")
