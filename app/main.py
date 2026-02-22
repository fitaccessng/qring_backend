import logging
import uuid

import socketio
from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from sqlalchemy import inspect, text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.api.routes import api_router
from app.core.config import get_settings
from app.core.exceptions import register_exception_handlers
from app.core.logging import setup_logging
from app.core.security import hash_password
from app.db.base import Base
from app.db.models import Door, Home, Notification, QRCode, User, UserRole
from app.db.session import SessionLocal, engine
from app.middleware.request_context import RequestContextMiddleware
from app.socket.server import sio

settings = get_settings()
setup_logging(logging.DEBUG if settings.DEBUG else logging.INFO)

fastapi_app = FastAPI(title=settings.APP_NAME, debug=settings.DEBUG)
fastapi_app.include_router(api_router, prefix=settings.API_V1_PREFIX)
fastapi_app.add_middleware(RequestContextMiddleware)
fastapi_app.add_middleware(
    CORSMiddleware,
    allow_origins=settings.cors_origins,
    allow_origin_regex=settings.cors_allow_origin_regex,
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)
register_exception_handlers(fastapi_app)


def _seed_dev_data(db: Session):
    if db.query(User).count() > 0:
        return

    homeowner = User(
        id=str(uuid.uuid4()),
        full_name="Demo Homeowner",
        email="homeowner@useqring.online",
        password_hash=hash_password("Password123!"),
        role=UserRole.homeowner,
        email_verified=True,
    )
    admin = User(
        id=str(uuid.uuid4()),
        full_name="Demo Admin",
        email="admin@useqring.online",
        password_hash=hash_password("Password123!"),
        role=UserRole.admin,
        email_verified=True,
    )
    estate_user = User(
        id=str(uuid.uuid4()),
        full_name="Demo Estate",
        email="estate@useqring.online",
        password_hash=hash_password("Password123!"),
        role=UserRole.estate,
        email_verified=True,
    )

    try:
        db.add_all([homeowner, admin, estate_user])
        db.flush()

        home = Home(name="Unit A1", homeowner_id=homeowner.id)
        db.add(home)
        db.flush()

        door = Door(name="Front Door", home_id=home.id)
        db.add(door)
        db.flush()

        qr = QRCode(
            qr_id="demo-qr-001",
            plan="single",
            home_id=home.id,
            doors_csv=door.id,
            mode="direct",
            active=True,
        )
        db.add(qr)

        notification = Notification(
            user_id=homeowner.id,
            kind="system",
            payload='{"message":"Welcome to Qring. Your first door is ready for QR generation."}',
        )
        db.add(notification)
        db.commit()
    except IntegrityError:
        # Another worker/process already inserted seed rows.
        db.rollback()


def _next_referral_code() -> str:
    return f"QR{uuid.uuid4().hex[:8].upper()}"


def _ensure_referral_schema() -> None:
    inspector = inspect(engine)
    table_names = set(inspector.get_table_names())
    user_columns = {col["name"] for col in inspector.get_columns("users")} if "users" in table_names else set()

    with engine.begin() as conn:
        if "users" in table_names and "referral_code" not in user_columns:
            conn.execute(text("ALTER TABLE users ADD COLUMN referral_code VARCHAR(24)"))
        if "users" in table_names and "referred_by_user_id" not in user_columns:
            conn.execute(text("ALTER TABLE users ADD COLUMN referred_by_user_id VARCHAR(36)"))
        if "users" in table_names and "referral_earnings" not in user_columns:
            conn.execute(text("ALTER TABLE users ADD COLUMN referral_earnings INTEGER DEFAULT 0"))
        conn.execute(text("CREATE UNIQUE INDEX IF NOT EXISTS ix_users_referral_code ON users (referral_code)"))
        conn.execute(text("CREATE INDEX IF NOT EXISTS ix_users_referred_by_user_id ON users (referred_by_user_id)"))

    # Create referral reward table for existing installs.
    Base.metadata.tables["referral_rewards"].create(bind=engine, checkfirst=True)

    db = SessionLocal()
    try:
        used_codes = {row[0] for row in db.query(User.referral_code).filter(User.referral_code.is_not(None)).all()}
        changed = False
        for user in db.query(User).all():
            if not user.referral_code:
                code = _next_referral_code()
                while code in used_codes:
                    code = _next_referral_code()
                user.referral_code = code
                used_codes.add(code)
                changed = True
            if user.referral_earnings is None:
                user.referral_earnings = 0
                changed = True
        if changed:
            db.commit()
    finally:
        db.close()


@fastapi_app.on_event("startup")
async def on_startup():
    Base.metadata.create_all(bind=engine)
    _ensure_referral_schema()
    db = SessionLocal()
    try:
        if settings.ENVIRONMENT.lower() == "development":
            _seed_dev_data(db)
    finally:
        db.close()


app = socketio.ASGIApp(
    sio,
    other_asgi_app=fastapi_app,
    socketio_path=settings.SOCKET_PATH.lstrip("/"),
)

