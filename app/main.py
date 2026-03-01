import logging
import uuid

import socketio
from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from sqlalchemy import DateTime, inspect, text
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
from app.middleware.access_control import AccessControlMiddleware
from app.socket.server import sio

settings = get_settings()
setup_logging(logging.DEBUG if settings.DEBUG else logging.INFO)

fastapi_app = FastAPI(title=settings.APP_NAME, debug=settings.DEBUG)
fastapi_app.include_router(api_router, prefix=settings.API_V1_PREFIX)
fastapi_app.add_middleware(RequestContextMiddleware)
fastapi_app.add_middleware(AccessControlMiddleware)
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


def _ensure_message_read_schema() -> None:
    inspector = inspect(engine)
    table_names = set(inspector.get_table_names())
    message_columns = {col["name"] for col in inspector.get_columns("messages")} if "messages" in table_names else set()

    with engine.begin() as conn:
        if "messages" in table_names and "read_by_homeowner_at" not in message_columns:
            datetime_sql = DateTime().compile(dialect=conn.dialect)
            conn.execute(text(f"ALTER TABLE messages ADD COLUMN read_by_homeowner_at {datetime_sql}"))
        conn.execute(
            text(
                "CREATE INDEX IF NOT EXISTS ix_messages_read_by_homeowner_at "
                "ON messages (read_by_homeowner_at)"
            )
        )


def _ensure_auth_runtime_schema() -> None:
    inspector = inspect(engine)
    table_names = set(inspector.get_table_names())

    if "device_sessions" not in table_names:
        Base.metadata.tables["device_sessions"].create(bind=engine, checkfirst=True)
    else:
        session_columns = {col["name"] for col in inspector.get_columns("device_sessions")}
        with engine.begin() as conn:
            if "revoked_at" not in session_columns:
                datetime_sql = DateTime().compile(dialect=conn.dialect)
                conn.execute(text(f"ALTER TABLE device_sessions ADD COLUMN revoked_at {datetime_sql}"))


def _add_column_if_missing(
    conn,
    table_columns: set[str],
    table: str,
    column: str,
    sql_fragment: str,
) -> None:
    if column in table_columns:
        return
    conn.execute(text(f"ALTER TABLE {table} ADD COLUMN {column} {sql_fragment}"))


def _ensure_runtime_compatibility_schema() -> None:
    inspector = inspect(engine)
    table_names = set(inspector.get_table_names())

    with engine.begin() as conn:
        datetime_sql = str(DateTime().compile(dialect=conn.dialect))

        if "device_sessions" in table_names:
            columns = {col["name"] for col in inspector.get_columns("device_sessions")}
            _add_column_if_missing(conn, columns, "device_sessions", "user_agent", "VARCHAR(255) DEFAULT ''")
            _add_column_if_missing(conn, columns, "device_sessions", "ip_address", "VARCHAR(80) DEFAULT ''")
            _add_column_if_missing(conn, columns, "device_sessions", "created_at", datetime_sql)
            _add_column_if_missing(conn, columns, "device_sessions", "revoked_at", datetime_sql)

        if "notifications" in table_names:
            columns = {col["name"] for col in inspector.get_columns("notifications")}
            _add_column_if_missing(conn, columns, "notifications", "kind", "VARCHAR(50) DEFAULT 'system'")
            _add_column_if_missing(conn, columns, "notifications", "payload", "TEXT DEFAULT '{}'")
            _add_column_if_missing(conn, columns, "notifications", "read_at", datetime_sql)
            _add_column_if_missing(conn, columns, "notifications", "created_at", datetime_sql)

        if "visitor_sessions" in table_names:
            columns = {col["name"] for col in inspector.get_columns("visitor_sessions")}
            _add_column_if_missing(conn, columns, "visitor_sessions", "home_id", "VARCHAR(36)")
            _add_column_if_missing(conn, columns, "visitor_sessions", "door_id", "VARCHAR(36)")
            _add_column_if_missing(conn, columns, "visitor_sessions", "homeowner_id", "VARCHAR(36)")
            _add_column_if_missing(conn, columns, "visitor_sessions", "visitor_label", "VARCHAR(120) DEFAULT 'Visitor'")
            _add_column_if_missing(conn, columns, "visitor_sessions", "status", "VARCHAR(40) DEFAULT 'pending'")
            _add_column_if_missing(conn, columns, "visitor_sessions", "started_at", datetime_sql)
            _add_column_if_missing(conn, columns, "visitor_sessions", "ended_at", datetime_sql)

        if "messages" in table_names:
            columns = {col["name"] for col in inspector.get_columns("messages")}
            _add_column_if_missing(conn, columns, "messages", "sender_type", "VARCHAR(20) DEFAULT 'visitor'")
            _add_column_if_missing(conn, columns, "messages", "body", "TEXT DEFAULT ''")
            _add_column_if_missing(conn, columns, "messages", "created_at", datetime_sql)
            _add_column_if_missing(conn, columns, "messages", "read_by_homeowner_at", datetime_sql)

        if "homeowner_settings" in table_names:
            columns = {col["name"] for col in inspector.get_columns("homeowner_settings")}
            _add_column_if_missing(conn, columns, "homeowner_settings", "push_alerts", "BOOLEAN DEFAULT 1")
            _add_column_if_missing(conn, columns, "homeowner_settings", "sound_alerts", "BOOLEAN DEFAULT 1")
            _add_column_if_missing(
                conn,
                columns,
                "homeowner_settings",
                "auto_reject_unknown_visitors",
                "BOOLEAN DEFAULT 0",
            )
            _add_column_if_missing(conn, columns, "homeowner_settings", "created_at", datetime_sql)
            _add_column_if_missing(conn, columns, "homeowner_settings", "updated_at", datetime_sql)


def _ensure_notification_schema() -> None:
    inspector = inspect(engine)
    table_names = set(inspector.get_table_names())

    if "notifications" not in table_names:
        Base.metadata.tables["notifications"].create(bind=engine, checkfirst=True)
    else:
        notification_columns = {col["name"] for col in inspector.get_columns("notifications")}
        with engine.begin() as conn:
            if "read_at" not in notification_columns:
                datetime_sql = DateTime().compile(dialect=conn.dialect)
                conn.execute(text(f"ALTER TABLE notifications ADD COLUMN read_at {datetime_sql}"))
            conn.execute(
                text(
                    "CREATE INDEX IF NOT EXISTS ix_notifications_user_read "
                    "ON notifications (user_id, read_at)"
                )
            )


def _ensure_homeowner_settings_schema() -> None:
    inspector = inspect(engine)
    table_names = set(inspector.get_table_names())
    if "homeowner_settings" not in table_names:
        Base.metadata.tables["homeowner_settings"].create(bind=engine, checkfirst=True)


def _validate_livekit_runtime() -> tuple[bool, list[str]]:
    missing: list[str] = []
    if not settings.LIVEKIT_URL.strip():
        missing.append("LIVEKIT_URL")
    if not settings.LIVEKIT_API_KEY.strip():
        missing.append("LIVEKIT_API_KEY")
    if not settings.LIVEKIT_API_SECRET.strip():
        missing.append("LIVEKIT_API_SECRET")
    return (len(missing) == 0, missing)


@fastapi_app.on_event("startup")
async def on_startup():
    livekit_ok, missing = _validate_livekit_runtime()
    env = settings.ENVIRONMENT.lower().strip()
    if not livekit_ok:
        message = f"LiveKit configuration missing: {', '.join(missing)}"
        if env in {"production", "staging"}:
            raise RuntimeError(message)
        logging.warning("%s (continuing because ENVIRONMENT=%s)", message, settings.ENVIRONMENT)

    Base.metadata.create_all(bind=engine)
    _ensure_runtime_compatibility_schema()
    _ensure_auth_runtime_schema()
    _ensure_notification_schema()
    _ensure_homeowner_settings_schema()
    _ensure_referral_schema()
    _ensure_message_read_schema()
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

