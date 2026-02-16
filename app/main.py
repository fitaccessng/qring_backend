import logging
import uuid

import socketio
from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
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
    allow_origin_regex=settings.CORS_ALLOW_ORIGIN_REGEX,
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


@fastapi_app.on_event("startup")
async def on_startup():
    Base.metadata.create_all(bind=engine)
    db = SessionLocal()
    try:
        _seed_dev_data(db)
    finally:
        db.close()


app = socketio.ASGIApp(
    sio,
    other_asgi_app=fastapi_app,
    socketio_path=settings.SOCKET_PATH.lstrip("/"),
)

