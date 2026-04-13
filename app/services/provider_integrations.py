from __future__ import annotations

import json
import logging
import smtplib
from email.message import EmailMessage
from email.utils import make_msgid
from threading import Lock
from urllib import error, request

from sqlalchemy.orm import Session

from app.core.config import get_settings
from app.db.models import PushSubscription, User

settings = get_settings()
logger = logging.getLogger(__name__)

_firebase_lock = Lock()
_firebase_app = None

try:
    import firebase_admin
    from firebase_admin import credentials as firebase_credentials
    from firebase_admin import messaging as firebase_messaging
except ImportError:
    firebase_admin = None
    firebase_credentials = None
    firebase_messaging = None


def _load_firebase_credentials() -> dict | None:
    raw_json = (settings.FIREBASE_SERVICE_ACCOUNT_JSON or "").strip()
    if raw_json:
        return json.loads(raw_json)
    raw_base64 = (settings.FIREBASE_SERVICE_ACCOUNT_BASE64 or "").strip()
    if raw_base64:
        import base64

        decoded = base64.b64decode(raw_base64).decode("utf-8")
        return json.loads(decoded)
    return None


def _get_firebase_app():
    global _firebase_app
    if firebase_admin is None or firebase_credentials is None or firebase_messaging is None:
        return None
    if _firebase_app is not None:
        return _firebase_app
    with _firebase_lock:
        if _firebase_app is not None:
            return _firebase_app
        if not settings.FIREBASE_PROJECT_ID.strip():
            return None
        if firebase_admin._apps:
            _firebase_app = firebase_admin.get_app()
            return _firebase_app
        creds = _load_firebase_credentials()
        if creds:
            _firebase_app = firebase_admin.initialize_app(
                credential=firebase_credentials.Certificate(creds),
                options={"projectId": settings.FIREBASE_PROJECT_ID},
            )
            return _firebase_app
        _firebase_app = firebase_admin.initialize_app(options={"projectId": settings.FIREBASE_PROJECT_ID})
        return _firebase_app


def upsert_push_subscription(
    db: Session,
    *,
    user_id: str,
    provider: str,
    endpoint: str,
    token: str | None = None,
    keys: dict | None = None,
) -> dict:
    normalized_provider = (provider or "fcm").strip().lower()
    endpoint_value = (endpoint or "").strip()
    token_value = (token or "").strip()
    if not endpoint_value and not token_value:
        raise ValueError("A push endpoint or token is required.")

    row = (
        db.query(PushSubscription)
        .filter(
            PushSubscription.user_id == user_id,
            PushSubscription.provider == normalized_provider,
            PushSubscription.endpoint == endpoint_value,
        )
        .first()
    )
    payload = json.dumps(keys or {})
    if not row:
        row = PushSubscription(
            user_id=user_id,
            provider=normalized_provider,
            endpoint=endpoint_value,
            token=token_value or None,
            keys_json=payload,
            is_active=True,
        )
        db.add(row)
    else:
        row.token = token_value or row.token
        row.keys_json = payload
        row.is_active = True
    db.commit()
    db.refresh(row)
    return {
        "id": row.id,
        "provider": row.provider,
        "endpoint": row.endpoint,
        "token": row.token,
        "isActive": bool(row.is_active),
    }


def send_push_fcm(
    db: Session,
    *,
    user_id: str,
    title: str,
    body: str,
    data: dict | None = None,
) -> dict:
    app = _get_firebase_app()
    if app is None or firebase_messaging is None:
        return {"status": "disabled", "reason": "firebase_not_configured"}

    rows = (
        db.query(PushSubscription)
        .filter(
            PushSubscription.user_id == user_id,
            PushSubscription.provider == "fcm",
            PushSubscription.is_active == True,  # noqa: E712
        )
        .all()
    )
    tokens = [str(row.token or "").strip() for row in rows if str(row.token or "").strip()]
    if not tokens:
        return {"status": "skipped", "reason": "no_tokens"}

    ok = 0
    failed = 0
    for token in tokens:
        try:
            message = firebase_messaging.Message(
                token=token,
                notification=firebase_messaging.Notification(title=title, body=body),
                data={k: str(v) for k, v in (data or {}).items()},
            )
            firebase_messaging.send(message, app=app)
            ok += 1
        except Exception:
            failed += 1
            logger.exception("Failed sending FCM message to user=%s token=%s", user_id, token[:12])
    status = "sent" if ok > 0 and failed == 0 else ("partial" if ok > 0 else "failed")
    return {"status": status, "sent": ok, "failed": failed}


def _send_email_via_transport(
    *,
    host: str,
    port: int,
    username: str,
    password: str,
    message: EmailMessage,
):
    if port == 465:
        with smtplib.SMTP_SSL(host=host, port=port, timeout=20) as server:
            server.ehlo()
            if username and password:
                server.login(username, password)
            server.send_message(message)
        return

    with smtplib.SMTP(host=host, port=port, timeout=20) as server:
        server.ehlo()
        server.starttls()
        server.ehlo()
        if username and password:
            server.login(username, password)
        server.send_message(message)


def send_email_smtp(*, to_email: str, subject: str, body: str) -> dict:
    host = (settings.SMTP_HOST or "").strip()
    username = (settings.SMTP_USERNAME or "").strip()
    password = (settings.SMTP_PASSWORD or "").strip()
    from_email = (settings.SMTP_FROM_EMAIL or "").strip()
    domain = (from_email.split("@", 1)[1] if "@" in from_email else "useqring.online").strip() or "useqring.online"
    if not host or not from_email:
        return {"status": "disabled", "reason": "smtp_not_configured", "messageId": None}
    if not to_email.strip():
        return {"status": "skipped", "reason": "missing_recipient", "messageId": None}

    message = EmailMessage()
    message["Subject"] = subject
    message["From"] = from_email
    message["To"] = to_email.strip()
    # Add a deterministic-ish trace key so Brevo logs can be searched even if delivery is delayed/bounced.
    message_id = make_msgid(domain=domain)
    message["Message-ID"] = message_id
    message.set_content(body)

    port = int(settings.SMTP_PORT or 587)

    try:
        _send_email_via_transport(
            host=host,
            port=port,
            username=username,
            password=password,
            message=message,
        )
        return {"status": "sent", "messageId": message_id}
    except Exception as primary_exc:
        if port != 465:
            try:
                _send_email_via_transport(
                    host=host,
                    port=465,
                    username=username,
                    password=password,
                    message=message,
                )
                return {"status": "sent", "messageId": message_id}
            except Exception as fallback_exc:
                logger.exception("SMTP send failed to %s on ports %s and 465", to_email, port)
                return {
                    "status": "failed",
                    "reason": f"{primary_exc} | fallback_465: {fallback_exc}",
                    "messageId": message_id,
                }
        logger.exception("SMTP send failed to %s", to_email)
        return {"status": "failed", "reason": str(primary_exc), "messageId": message_id}


def send_sms_provider(*, phone_number: str, message: str) -> dict:
    api_key = (settings.SMS_PROVIDER_API_KEY or "").strip()
    base_url = (settings.SMS_PROVIDER_BASE_URL or "").strip()
    sender_id = (settings.SMS_PROVIDER_SENDER_ID or "Qring").strip()
    if not api_key or not base_url:
        return {"status": "disabled", "reason": "sms_not_configured"}
    if not phone_number.strip():
        return {"status": "skipped", "reason": "missing_phone"}

    payload = json.dumps(
        {
            "to": phone_number.strip(),
            "message": message,
            "sender": sender_id,
        }
    ).encode("utf-8")
    req = request.Request(
        base_url,
        data=payload,
        method="POST",
        headers={
            "Content-Type": "application/json",
            "Authorization": f"Bearer {api_key}",
            "Accept": "application/json",
        },
    )
    try:
        with request.urlopen(req, timeout=20) as resp:
            raw = resp.read().decode("utf-8")
            parsed = json.loads(raw) if raw else {}
        return {"status": "sent", "providerResponse": parsed}
    except error.HTTPError as exc:
        detail = exc.read().decode("utf-8", errors="ignore")
        return {"status": "failed", "reason": detail or str(exc)}
    except Exception as exc:
        return {"status": "failed", "reason": str(exc)}


def recognize_face_provider(
    *,
    homeowner_id: str,
    display_name: str,
    identifier: str,
    encrypted_template: str | None,
) -> dict | None:
    api_url = (settings.FACE_RECOGNITION_API_URL or "").strip()
    api_key = (settings.FACE_RECOGNITION_API_KEY or "").strip()
    if not api_url or not api_key:
        return None

    payload = json.dumps(
        {
            "homeownerId": homeowner_id,
            "displayName": display_name,
            "identifier": identifier,
            "encryptedTemplate": encrypted_template,
        }
    ).encode("utf-8")
    req = request.Request(
        api_url,
        data=payload,
        method="POST",
        headers={
            "Content-Type": "application/json",
            "Authorization": f"Bearer {api_key}",
            "Accept": "application/json",
        },
    )
    try:
        with request.urlopen(req, timeout=20) as resp:
            raw = resp.read().decode("utf-8")
            return json.loads(raw) if raw else {}
    except Exception:
        logger.exception("Face recognition provider request failed")
        return None


def get_user_contact(db: Session, *, user_id: str) -> User | None:
    return db.query(User).filter(User.id == user_id).first()
