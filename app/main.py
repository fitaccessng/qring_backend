import logging
import uuid
import asyncio

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
from app.middleware.input_sanitization import InputSanitizationMiddleware
from app.middleware.rate_limit import RateLimitMiddleware
from app.socket.server import sio
from app.services.estate_alert_service import (
    cleanup_broken_alerts,
    repair_estate_alert_schema,
    run_scheduled_payment_reminders,
)

settings = get_settings()
setup_logging(logging.DEBUG if settings.DEBUG else logging.INFO)

fastapi_app = FastAPI(title=settings.APP_NAME, debug=settings.DEBUG)
fastapi_app.include_router(api_router, prefix=settings.API_V1_PREFIX)
fastapi_app.add_middleware(RequestContextMiddleware)
fastapi_app.add_middleware(
    RateLimitMiddleware,
    window_seconds=settings.RATE_LIMIT_WINDOW_SECONDS,
    max_requests=settings.RATE_LIMIT_MAX_REQUESTS,
    auth_window_seconds=settings.RATE_LIMIT_AUTH_WINDOW_SECONDS,
    auth_max_requests=settings.RATE_LIMIT_AUTH_MAX_REQUESTS,
)
fastapi_app.add_middleware(InputSanitizationMiddleware)
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
            _add_column_if_missing(conn, columns, "visitor_sessions", "request_id", "VARCHAR(64)")
            _add_column_if_missing(conn, columns, "visitor_sessions", "home_id", "VARCHAR(36)")
            _add_column_if_missing(conn, columns, "visitor_sessions", "door_id", "VARCHAR(36)")
            _add_column_if_missing(conn, columns, "visitor_sessions", "homeowner_id", "VARCHAR(36)")
            _add_column_if_missing(conn, columns, "visitor_sessions", "appointment_id", "VARCHAR(36)")
            _add_column_if_missing(conn, columns, "visitor_sessions", "visitor_label", "VARCHAR(120) DEFAULT 'Visitor'")
            _add_column_if_missing(conn, columns, "visitor_sessions", "status", "VARCHAR(40) DEFAULT 'pending'")
            _add_column_if_missing(conn, columns, "visitor_sessions", "started_at", datetime_sql)
            _add_column_if_missing(conn, columns, "visitor_sessions", "ended_at", datetime_sql)
            conn.execute(
                text(
                    "CREATE UNIQUE INDEX IF NOT EXISTS ix_visitor_sessions_request_id ON visitor_sessions (request_id)"
                )
            )

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

        if "estates" in table_names:
            columns = {col["name"] for col in inspector.get_columns("estates")}
            _add_column_if_missing(conn, columns, "estates", "reminder_frequency_days", "INTEGER DEFAULT 1")

        if "appointments" in table_names:
            columns = {col["name"] for col in inspector.get_columns("appointments")}
            _add_column_if_missing(conn, columns, "appointments", "homeowner_id", "VARCHAR(36)")
            _add_column_if_missing(conn, columns, "appointments", "home_id", "VARCHAR(36)")
            _add_column_if_missing(conn, columns, "appointments", "door_id", "VARCHAR(36)")
            _add_column_if_missing(conn, columns, "appointments", "visitor_name", "VARCHAR(120) DEFAULT 'Visitor'")
            _add_column_if_missing(conn, columns, "appointments", "visitor_contact", "VARCHAR(120) DEFAULT ''")
            _add_column_if_missing(conn, columns, "appointments", "purpose", "TEXT DEFAULT ''")
            _add_column_if_missing(conn, columns, "appointments", "starts_at", datetime_sql)
            _add_column_if_missing(conn, columns, "appointments", "ends_at", datetime_sql)
            _add_column_if_missing(conn, columns, "appointments", "status", "VARCHAR(40) DEFAULT 'created'")
            _add_column_if_missing(conn, columns, "appointments", "geofence_lat", "FLOAT")
            _add_column_if_missing(conn, columns, "appointments", "geofence_lng", "FLOAT")
            _add_column_if_missing(conn, columns, "appointments", "geofence_radius_m", "INTEGER DEFAULT 120")
            _add_column_if_missing(conn, columns, "appointments", "share_token_hash", "VARCHAR(128)")
            _add_column_if_missing(conn, columns, "appointments", "share_token_created_at", datetime_sql)
            _add_column_if_missing(conn, columns, "appointments", "accepted_at", datetime_sql)
            _add_column_if_missing(conn, columns, "appointments", "accepted_device_id", "VARCHAR(120)")
            _add_column_if_missing(conn, columns, "appointments", "qr_token_hash", "VARCHAR(128)")
            _add_column_if_missing(conn, columns, "appointments", "qr_payload_encrypted", "TEXT")
            _add_column_if_missing(conn, columns, "appointments", "qr_signature", "VARCHAR(200)")
            _add_column_if_missing(conn, columns, "appointments", "qr_expires_at", datetime_sql)
            _add_column_if_missing(conn, columns, "appointments", "qr_used_at", datetime_sql)
            _add_column_if_missing(conn, columns, "appointments", "qr_used_device_id", "VARCHAR(120)")
            _add_column_if_missing(conn, columns, "appointments", "arrived_at", datetime_sql)
            _add_column_if_missing(conn, columns, "appointments", "arrival_lat", "FLOAT")
            _add_column_if_missing(conn, columns, "appointments", "arrival_lng", "FLOAT")
            _add_column_if_missing(conn, columns, "appointments", "arrival_battery_pct", "INTEGER")
            _add_column_if_missing(conn, columns, "appointments", "created_at", datetime_sql)
            _add_column_if_missing(conn, columns, "appointments", "updated_at", datetime_sql)

        if "estate_alerts" in table_names:
            columns = {col["name"] for col in inspector.get_columns("estate_alerts")}
            _add_column_if_missing(conn, columns, "estate_alerts", "poll_options", "TEXT DEFAULT ''")
            _add_column_if_missing(conn, columns, "estate_alerts", "target_homeowner_ids", "TEXT DEFAULT ''")

        if "homeowner_payments" in table_names:
            columns = {col["name"] for col in inspector.get_columns("homeowner_payments")}
            _add_column_if_missing(conn, columns, "homeowner_payments", "reminder_sent_at", datetime_sql)

        if "call_sessions" in table_names:
            columns = {col["name"] for col in inspector.get_columns("call_sessions")}
            _add_column_if_missing(conn, columns, "call_sessions", "appointment_id", "VARCHAR(36)")
            _add_column_if_missing(conn, columns, "call_sessions", "visitor_session_id", "VARCHAR(36)")
            _add_column_if_missing(conn, columns, "call_sessions", "room_name", "VARCHAR(160)")
            _add_column_if_missing(conn, columns, "call_sessions", "visitor_id", "VARCHAR(120)")
            _add_column_if_missing(conn, columns, "call_sessions", "homeowner_id", "VARCHAR(36)")
            _add_column_if_missing(conn, columns, "call_sessions", "status", "VARCHAR(20) DEFAULT 'pending'")
            _add_column_if_missing(conn, columns, "call_sessions", "created_at", datetime_sql)
            _add_column_if_missing(conn, columns, "call_sessions", "ended_at", datetime_sql)
            conn.execute(text("CREATE UNIQUE INDEX IF NOT EXISTS ix_call_sessions_room_name ON call_sessions (room_name)"))


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


def _ensure_call_sessions_schema() -> None:
    # Keep call runtime available even when migrations were not executed in an environment.
    Base.metadata.tables["call_sessions"].create(bind=engine, checkfirst=True)
    inspector = inspect(engine)
    table_names = set(inspector.get_table_names())
    if "call_sessions" not in table_names:
        return
    columns = {col["name"] for col in inspector.get_columns("call_sessions")}
    with engine.begin() as conn:
        _add_column_if_missing(conn, columns, "call_sessions", "appointment_id", "VARCHAR(36)")
        _add_column_if_missing(conn, columns, "call_sessions", "visitor_session_id", "VARCHAR(36)")
        _add_column_if_missing(conn, columns, "call_sessions", "room_name", "VARCHAR(160)")
        _add_column_if_missing(conn, columns, "call_sessions", "visitor_id", "VARCHAR(120)")
        _add_column_if_missing(conn, columns, "call_sessions", "homeowner_id", "VARCHAR(36)")
        _add_column_if_missing(conn, columns, "call_sessions", "status", "VARCHAR(20) DEFAULT 'pending'")
        _add_column_if_missing(conn, columns, "call_sessions", "created_at", str(DateTime().compile(dialect=conn.dialect)))
        _add_column_if_missing(conn, columns, "call_sessions", "ended_at", str(DateTime().compile(dialect=conn.dialect)))
        if conn.dialect.name == "postgresql":
            conn.execute(text("ALTER TABLE call_sessions ALTER COLUMN appointment_id DROP NOT NULL"))
        conn.execute(text("CREATE UNIQUE INDEX IF NOT EXISTS ix_call_sessions_room_name ON call_sessions (room_name)"))
        conn.execute(
            text("CREATE INDEX IF NOT EXISTS ix_call_sessions_visitor_session_id ON call_sessions (visitor_session_id)")
        )


def _ensure_advanced_features_schema() -> None:
    tables = [
        "visitor_snapshot_audits",
        "visitor_recognition_profiles",
        "split_bills",
        "split_contributions",
        "digital_receipts",
        "threat_alert_logs",
        "emergency_signals",
        "community_posts",
        "community_post_reads",
        "weekly_summary_logs",
        "push_subscriptions",
        "estate_meeting_responses",
        "estate_poll_votes",
        "maintenance_status_audits",
    ]
    for table in tables:
        Base.metadata.tables[table].create(bind=engine, checkfirst=True)


def _ensure_estate_alert_schema() -> None:
    inspector = inspect(engine)
    table_names = set(inspector.get_table_names())
    if "estate_alerts" not in table_names:
        Base.metadata.tables["estate_alerts"].create(bind=engine, checkfirst=True)
        inspector = inspect(engine)
    columns = {col["name"] for col in inspector.get_columns("estate_alerts")}
    with engine.begin() as conn:
        _add_column_if_missing(conn, columns, "estate_alerts", "poll_options", "TEXT DEFAULT ''")
        _add_column_if_missing(conn, columns, "estate_alerts", "target_homeowner_ids", "TEXT DEFAULT ''")
        _add_column_if_missing(conn, columns, "estate_alerts", "maintenance_status", "VARCHAR(20) DEFAULT 'pending'")
        if conn.dialect.name == "postgresql":
            # Ensure enum values exist for older deployments.
            enum_names = ["estatealerttype", "estate_alert_type"]
            enum_values = ["notice", "payment_request", "meeting", "maintenance_request", "poll"]
            for enum_name in enum_names:
                for enum_value in enum_values:
                    try:
                        conn.execute(
                            text(
                                "DO $$ BEGIN "
                                f"ALTER TYPE {enum_name} ADD VALUE IF NOT EXISTS '{enum_value}'; "
                                "EXCEPTION WHEN undefined_object THEN END $$;"
                            )
                        )
                    except Exception:
                        pass


def _ensure_homeowner_payment_schema() -> None:
    inspector = inspect(engine)
    table_names = set(inspector.get_table_names())
    if "homeowner_payments" not in table_names:
        return
    columns = {col["name"] for col in inspector.get_columns("homeowner_payments")}
    with engine.begin() as conn:
        datetime_sql = str(DateTime().compile(dialect=conn.dialect))
        _add_column_if_missing(conn, columns, "homeowner_payments", "payment_method", "VARCHAR(40)")
        _add_column_if_missing(conn, columns, "homeowner_payments", "payment_note", "TEXT")
        _add_column_if_missing(conn, columns, "homeowner_payments", "payment_proof_url", "TEXT")
        _add_column_if_missing(conn, columns, "homeowner_payments", "reminder_sent_at", datetime_sql)


def _ensure_wallet_schema() -> None:
    inspector = inspect(engine)
    table_names = set(inspector.get_table_names())
    if "homeowner_wallets" in table_names and "homeowner_wallet_transactions" in table_names:
        return
    Base.metadata.tables["homeowner_wallets"].create(bind=engine, checkfirst=True)
    Base.metadata.tables["homeowner_wallet_transactions"].create(bind=engine, checkfirst=True)


async def _payment_reminder_loop() -> None:
    while True:
        try:
            db = SessionLocal()
            try:
                run_scheduled_payment_reminders(db)
            finally:
                db.close()
        except Exception:
            logging.exception("Automatic payment reminder cycle failed.")
        await asyncio.sleep(6 * 60 * 60)


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

    # Runtime schema mutations are intentionally disabled in favor of Alembic migrations.
    # Keep automatic table creation only for local development convenience.
    if settings.ENVIRONMENT.lower() == "development":
        Base.metadata.create_all(bind=engine)
    _ensure_call_sessions_schema()
    _ensure_advanced_features_schema()
    _ensure_estate_alert_schema()
    _ensure_homeowner_payment_schema()
    _ensure_wallet_schema()
    _ensure_runtime_compatibility_schema()
    db = SessionLocal()
    try:
        if settings.ENVIRONMENT.lower() == "development":
            _seed_dev_data(db)
        try:
            repair_estate_alert_schema(db)
            cleanup_broken_alerts(db)
        except Exception:
            logging.exception("Startup alert repair/cleanup failed.")
    finally:
        db.close()
    asyncio.create_task(_payment_reminder_loop())


app = socketio.ASGIApp(
    sio,
    other_asgi_app=fastapi_app,
    socketio_path=settings.SOCKET_PATH.lstrip("/"),
)

# Apply CORS at the top-level ASGI app so CORS headers are present even when
# requests are handled by the socket.io wrapper (or fail before reaching FastAPI).
app = CORSMiddleware(
    app,
    allow_origins=settings.cors_origins,
    allow_origin_regex=settings.cors_allow_origin_regex,
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

