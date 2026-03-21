from __future__ import annotations

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
    security_user = User(
        id=str(uuid.uuid4()),
        full_name="Demo Gateman",
        email="security@useqring.online",
        password_hash=hash_password("Password123!"),
        role=UserRole.security,
        email_verified=True,
        phone="+2347000000000",
    )

    try:
        db.add_all([homeowner, admin, estate_user, security_user])
        db.flush()

        from app.db.models import Estate

        estate = Estate(
            name="Demo Estate",
            owner_id=estate_user.id,
            security_can_approve_without_homeowner=False,
            security_must_notify_homeowner=True,
            security_require_photo_verification=True,
            security_require_call_before_approval=False,
        )
        db.add(estate)
        db.flush()

        security_user.estate_id = estate.id
        security_user.gate_id = "main-gate"

        home = Home(name="Unit A1", homeowner_id=homeowner.id, estate_id=estate.id)
        db.add(home)
        db.flush()

        door = Door(name="Front Door", home_id=home.id, gate_label="Main Gate")
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
    normalized_fragment = (
        sql_fragment
        .replace("BOOLEAN DEFAULT 1", "BOOLEAN DEFAULT TRUE")
        .replace("BOOLEAN DEFAULT 0", "BOOLEAN DEFAULT FALSE")
    )
    conn.execute(text(f"ALTER TABLE {table} ADD COLUMN {column} {normalized_fragment}"))


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
            _add_column_if_missing(conn, columns, "visitor_sessions", "visitor_type", "VARCHAR(20) DEFAULT 'guest'")
            _add_column_if_missing(conn, columns, "visitor_sessions", "request_source", "VARCHAR(30) DEFAULT 'visitor_qr'")
            _add_column_if_missing(conn, columns, "visitor_sessions", "creator_role", "VARCHAR(20) DEFAULT 'visitor'")
            _add_column_if_missing(conn, columns, "visitor_sessions", "visitor_phone", "VARCHAR(40)")
            _add_column_if_missing(conn, columns, "visitor_sessions", "purpose", "TEXT")
            _add_column_if_missing(conn, columns, "visitor_sessions", "photo_url", "TEXT")
            _add_column_if_missing(conn, columns, "visitor_sessions", "estate_id", "VARCHAR(36)")
            _add_column_if_missing(conn, columns, "visitor_sessions", "gate_id", "VARCHAR(36)")
            _add_column_if_missing(conn, columns, "visitor_sessions", "handled_by_security_id", "VARCHAR(36)")
            _add_column_if_missing(conn, columns, "visitor_sessions", "communication_status", "VARCHAR(30) DEFAULT 'none'")
            _add_column_if_missing(conn, columns, "visitor_sessions", "preferred_communication_channel", "VARCHAR(20)")
            _add_column_if_missing(conn, columns, "visitor_sessions", "preferred_communication_target", "VARCHAR(20)")
            _add_column_if_missing(conn, columns, "visitor_sessions", "gate_status", "VARCHAR(30) DEFAULT 'waiting'")
            _add_column_if_missing(conn, columns, "visitor_sessions", "trust_status", "VARCHAR(20) DEFAULT 'new'")
            _add_column_if_missing(conn, columns, "visitor_sessions", "trust_score", "INTEGER DEFAULT 0")
            _add_column_if_missing(conn, columns, "visitor_sessions", "total_visits_snapshot", "INTEGER DEFAULT 0")
            _add_column_if_missing(conn, columns, "visitor_sessions", "approvals_count_snapshot", "INTEGER DEFAULT 0")
            _add_column_if_missing(conn, columns, "visitor_sessions", "rejections_count_snapshot", "INTEGER DEFAULT 0")
            _add_column_if_missing(conn, columns, "visitor_sessions", "unique_houses_visited_snapshot", "INTEGER DEFAULT 0")
            _add_column_if_missing(conn, columns, "visitor_sessions", "repeat_visits_to_home_snapshot", "INTEGER DEFAULT 0")
            _add_column_if_missing(conn, columns, "visitor_sessions", "auto_approved", "BOOLEAN DEFAULT 0")
            _add_column_if_missing(conn, columns, "visitor_sessions", "auto_approve_suggested", "BOOLEAN DEFAULT 0")
            _add_column_if_missing(conn, columns, "visitor_sessions", "pre_approved", "BOOLEAN DEFAULT 0")
            _add_column_if_missing(conn, columns, "visitor_sessions", "pre_approved_reason", "VARCHAR(160)")
            _add_column_if_missing(conn, columns, "visitor_sessions", "delivery_option", "VARCHAR(40)")
            _add_column_if_missing(conn, columns, "visitor_sessions", "delivery_drop_off_allowed", "BOOLEAN DEFAULT 0")
            _add_column_if_missing(conn, columns, "visitor_sessions", "suspicious_flag", "BOOLEAN DEFAULT 0")
            _add_column_if_missing(conn, columns, "visitor_sessions", "suspicious_reason", "VARCHAR(200)")
            _add_column_if_missing(conn, columns, "visitor_sessions", "received_by_security_at", datetime_sql)
            _add_column_if_missing(conn, columns, "visitor_sessions", "forwarded_to_homeowner_at", datetime_sql)
            _add_column_if_missing(conn, columns, "visitor_sessions", "homeowner_decision_at", datetime_sql)
            _add_column_if_missing(conn, columns, "visitor_sessions", "gate_action_at", datetime_sql)
            _add_column_if_missing(conn, columns, "visitor_sessions", "state_updated_at", datetime_sql)
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
            _add_column_if_missing(conn, columns, "messages", "sender_id", "VARCHAR(36)")
            _add_column_if_missing(conn, columns, "messages", "receiver_id", "VARCHAR(36)")
            _add_column_if_missing(conn, columns, "messages", "body", "TEXT DEFAULT ''")
            _add_column_if_missing(conn, columns, "messages", "created_at", datetime_sql)
            _add_column_if_missing(conn, columns, "messages", "read_by_homeowner_at", datetime_sql)
            _add_column_if_missing(conn, columns, "messages", "read_by_security_at", datetime_sql)

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
            _add_column_if_missing(conn, columns, "homeowner_settings", "auto_approve_trusted_visitors", "BOOLEAN DEFAULT 0")
            _add_column_if_missing(conn, columns, "homeowner_settings", "auto_approve_known_contacts", "BOOLEAN DEFAULT 0")
            _add_column_if_missing(conn, columns, "homeowner_settings", "known_contacts_json", "TEXT DEFAULT '[]'")
            _add_column_if_missing(conn, columns, "homeowner_settings", "allow_delivery_drop_at_gate", "BOOLEAN DEFAULT 1")
            _add_column_if_missing(conn, columns, "homeowner_settings", "sms_fallback_enabled", "BOOLEAN DEFAULT 0")
            _add_column_if_missing(conn, columns, "homeowner_settings", "created_at", datetime_sql)
            _add_column_if_missing(conn, columns, "homeowner_settings", "updated_at", datetime_sql)

        if "estates" in table_names:
            columns = {col["name"] for col in inspector.get_columns("estates")}
            _add_column_if_missing(conn, columns, "estates", "reminder_frequency_days", "INTEGER DEFAULT 1")
            _add_column_if_missing(conn, columns, "estates", "security_can_approve_without_homeowner", "BOOLEAN DEFAULT 0")
            _add_column_if_missing(conn, columns, "estates", "security_must_notify_homeowner", "BOOLEAN DEFAULT 1")
            _add_column_if_missing(conn, columns, "estates", "security_require_photo_verification", "BOOLEAN DEFAULT 0")
            _add_column_if_missing(conn, columns, "estates", "security_require_call_before_approval", "BOOLEAN DEFAULT 0")
            _add_column_if_missing(conn, columns, "estates", "auto_approve_trusted_visitors", "BOOLEAN DEFAULT 0")
            _add_column_if_missing(conn, columns, "estates", "suspicious_visit_window_minutes", "INTEGER DEFAULT 20")
            _add_column_if_missing(conn, columns, "estates", "suspicious_house_threshold", "INTEGER DEFAULT 3")
            _add_column_if_missing(conn, columns, "estates", "suspicious_rejection_threshold", "INTEGER DEFAULT 2")

        if "gate_logs" in table_names:
            columns = {col["name"] for col in inspector.get_columns("gate_logs")}
            _add_column_if_missing(conn, columns, "gate_logs", "visitor_session_id", "VARCHAR(36)")
            _add_column_if_missing(conn, columns, "gate_logs", "estate_id", "VARCHAR(36)")
            _add_column_if_missing(conn, columns, "gate_logs", "home_id", "VARCHAR(36)")
            _add_column_if_missing(conn, columns, "gate_logs", "gate_id", "VARCHAR(120)")
            _add_column_if_missing(conn, columns, "gate_logs", "actor_user_id", "VARCHAR(36)")
            _add_column_if_missing(conn, columns, "gate_logs", "actor_role", "VARCHAR(30)")
            _add_column_if_missing(conn, columns, "gate_logs", "action", "VARCHAR(80)")
            _add_column_if_missing(conn, columns, "gate_logs", "resulting_status", "VARCHAR(40)")
            _add_column_if_missing(conn, columns, "gate_logs", "notes", "TEXT")
            _add_column_if_missing(conn, columns, "gate_logs", "meta_json", "TEXT DEFAULT '{}'")
            _add_column_if_missing(conn, columns, "gate_logs", "created_at", datetime_sql)

        if "doors" in table_names:
            columns = {col["name"] for col in inspector.get_columns("doors")}
            _add_column_if_missing(conn, columns, "doors", "gate_label", "VARCHAR(120)")

        if "users" in table_names:
            columns = {col["name"] for col in inspector.get_columns("users")}
            _add_column_if_missing(conn, columns, "users", "phone", "VARCHAR(40)")
            _add_column_if_missing(conn, columns, "users", "estate_id", "VARCHAR(36)")
            _add_column_if_missing(conn, columns, "users", "gate_id", "VARCHAR(36)")
            if conn.dialect.name == "postgresql":
                try:
                    conn.execute(
                        text(
                            "DO $$ BEGIN "
                            "ALTER TYPE userrole ADD VALUE IF NOT EXISTS 'security'; "
                            "EXCEPTION WHEN undefined_object THEN NULL; END $$;"
                        )
                    )
                except Exception:
                    pass

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
            _add_column_if_missing(conn, columns, "call_sessions", "security_user_id", "VARCHAR(36)")
            _add_column_if_missing(conn, columns, "call_sessions", "caller_id", "VARCHAR(36)")
            _add_column_if_missing(conn, columns, "call_sessions", "receiver_id", "VARCHAR(36)")
            _add_column_if_missing(conn, columns, "call_sessions", "call_type", "VARCHAR(20) DEFAULT 'audio'")
            _add_column_if_missing(conn, columns, "call_sessions", "room_name", "VARCHAR(160)")
            _add_column_if_missing(conn, columns, "call_sessions", "visitor_id", "VARCHAR(120)")
            _add_column_if_missing(conn, columns, "call_sessions", "homeowner_id", "VARCHAR(36)")
            _add_column_if_missing(conn, columns, "call_sessions", "status", "VARCHAR(20) DEFAULT 'pending'")
            _add_column_if_missing(conn, columns, "call_sessions", "visitor_request_id", "VARCHAR(64)")
            _add_column_if_missing(conn, columns, "call_sessions", "initiated_by_role", "VARCHAR(20)")
            _add_column_if_missing(conn, columns, "call_sessions", "answered_at", datetime_sql)
            _add_column_if_missing(conn, columns, "call_sessions", "ended_reason", "VARCHAR(40)")
            _add_column_if_missing(conn, columns, "call_sessions", "created_at", datetime_sql)
            _add_column_if_missing(conn, columns, "call_sessions", "ended_at", datetime_sql)
            conn.execute(text("CREATE UNIQUE INDEX IF NOT EXISTS ix_call_sessions_room_name ON call_sessions (room_name)"))

        if "digital_access_passes" in table_names:
            columns = {col["name"] for col in inspector.get_columns("digital_access_passes")}
            _add_column_if_missing(conn, columns, "digital_access_passes", "homeowner_id", "VARCHAR(36)")
            _add_column_if_missing(conn, columns, "digital_access_passes", "estate_id", "VARCHAR(36)")
            _add_column_if_missing(conn, columns, "digital_access_passes", "home_id", "VARCHAR(36)")
            _add_column_if_missing(conn, columns, "digital_access_passes", "door_id", "VARCHAR(36)")
            _add_column_if_missing(conn, columns, "digital_access_passes", "pass_type", "VARCHAR(20) DEFAULT 'qr'")
            _add_column_if_missing(conn, columns, "digital_access_passes", "label", "VARCHAR(120) DEFAULT 'Guest Access'")
            _add_column_if_missing(conn, columns, "digital_access_passes", "visitor_name", "VARCHAR(120)")
            _add_column_if_missing(conn, columns, "digital_access_passes", "code_value", "VARCHAR(80)")
            _add_column_if_missing(conn, columns, "digital_access_passes", "valid_from", datetime_sql)
            _add_column_if_missing(conn, columns, "digital_access_passes", "valid_until", datetime_sql)
            _add_column_if_missing(conn, columns, "digital_access_passes", "max_uses", "INTEGER DEFAULT 1")
            _add_column_if_missing(conn, columns, "digital_access_passes", "used_count", "INTEGER DEFAULT 0")
            _add_column_if_missing(conn, columns, "digital_access_passes", "is_active", "BOOLEAN DEFAULT 1")
            _add_column_if_missing(conn, columns, "digital_access_passes", "created_at", datetime_sql)
            _add_column_if_missing(conn, columns, "digital_access_passes", "updated_at", datetime_sql)


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
        _add_column_if_missing(conn, columns, "call_sessions", "security_user_id", "VARCHAR(36)")
        _add_column_if_missing(conn, columns, "call_sessions", "caller_id", "VARCHAR(36)")
        _add_column_if_missing(conn, columns, "call_sessions", "receiver_id", "VARCHAR(36)")
        _add_column_if_missing(conn, columns, "call_sessions", "call_type", "VARCHAR(20) DEFAULT 'audio'")
        _add_column_if_missing(conn, columns, "call_sessions", "room_name", "VARCHAR(160)")
        _add_column_if_missing(conn, columns, "call_sessions", "visitor_id", "VARCHAR(120)")
        _add_column_if_missing(conn, columns, "call_sessions", "homeowner_id", "VARCHAR(36)")
        _add_column_if_missing(conn, columns, "call_sessions", "status", "VARCHAR(20) DEFAULT 'pending'")
        _add_column_if_missing(conn, columns, "call_sessions", "visitor_request_id", "VARCHAR(64)")
        _add_column_if_missing(conn, columns, "call_sessions", "initiated_by_role", "VARCHAR(20)")
        _add_column_if_missing(conn, columns, "call_sessions", "answered_at", str(DateTime().compile(dialect=conn.dialect)))
        _add_column_if_missing(conn, columns, "call_sessions", "ended_reason", "VARCHAR(40)")
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
    Base.metadata.tables["gate_logs"].create(bind=engine, checkfirst=True)
    Base.metadata.tables["digital_access_passes"].create(bind=engine, checkfirst=True)


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


def _ensure_subscription_schema() -> None:
    Base.metadata.tables["subscription_plans"].create(bind=engine, checkfirst=True)
    Base.metadata.tables["subscriptions"].create(bind=engine, checkfirst=True)
    Base.metadata.tables["payment_purposes"].create(bind=engine, checkfirst=True)

    inspector = inspect(engine)
    table_names = set(inspector.get_table_names())
    datetime_sql = str(DateTime().compile(dialect=engine.dialect))

    with engine.begin() as conn:
        if "subscription_plans" in table_names:
            columns = {col["name"] for col in inspector.get_columns("subscription_plans")}
            _add_column_if_missing(conn, columns, "subscription_plans", "audience", "VARCHAR(30) DEFAULT 'homeowner'")
            _add_column_if_missing(conn, columns, "subscription_plans", "max_admins", "INTEGER DEFAULT 1")
            _add_column_if_missing(conn, columns, "subscription_plans", "duration_days", "INTEGER")
            _add_column_if_missing(conn, columns, "subscription_plans", "trial_days", "INTEGER DEFAULT 0")
            _add_column_if_missing(conn, columns, "subscription_plans", "self_serve", "BOOLEAN DEFAULT 1")
            _add_column_if_missing(conn, columns, "subscription_plans", "manual_activation_required", "BOOLEAN DEFAULT 0")
            _add_column_if_missing(conn, columns, "subscription_plans", "hidden", "BOOLEAN DEFAULT 0")
            _add_column_if_missing(conn, columns, "subscription_plans", "enabled_features", "TEXT DEFAULT '[]'")
            _add_column_if_missing(conn, columns, "subscription_plans", "restrictions", "TEXT DEFAULT '[]'")

        if "subscriptions" in table_names:
            columns = {col["name"] for col in inspector.get_columns("subscriptions")}
            _add_column_if_missing(conn, columns, "subscriptions", "payment_status", "VARCHAR(30) DEFAULT 'unpaid'")
            _add_column_if_missing(conn, columns, "subscriptions", "billing_cycle", "VARCHAR(20) DEFAULT 'monthly'")
            _add_column_if_missing(conn, columns, "subscriptions", "trial_started_at", datetime_sql)
            _add_column_if_missing(conn, columns, "subscriptions", "trial_ends_at", datetime_sql)


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
    _ensure_subscription_schema()
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
