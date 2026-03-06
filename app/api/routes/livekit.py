import hashlib
import hmac
import json
import logging

from fastapi import APIRouter, Depends, Header, Request
from sqlalchemy.orm import Session

from app.core.config import get_settings
from app.core.exceptions import AppException
from app.db.session import get_db
from app.services.livekit_monitor_service import handle_livekit_webhook_event

router = APIRouter()
settings = get_settings()
logger = logging.getLogger(__name__)


def _is_valid_signature(raw_body: bytes, signature: str | None) -> bool:
    secret = (settings.LIVEKIT_WEBHOOK_SECRET or "").strip()
    if not secret:
        return True
    if not signature:
        return False
    expected = hmac.new(secret.encode("utf-8"), raw_body, hashlib.sha256).hexdigest()
    return hmac.compare_digest(expected, signature.strip())


@router.post("/webhooks/livekit")
async def livekit_webhook(
    request: Request,
    x_livekit_signature: str | None = Header(default=None),
    db: Session = Depends(get_db),
):
    raw_body = await request.body()
    if not _is_valid_signature(raw_body, x_livekit_signature):
        logger.warning("livekit.webhook.invalid_signature")
        raise AppException("Invalid LiveKit webhook signature.", status_code=401)

    try:
        payload = json.loads(raw_body.decode("utf-8")) if raw_body else {}
    except Exception as exc:
        raise AppException("Invalid webhook payload.", status_code=400) from exc

    result = handle_livekit_webhook_event(db, payload if isinstance(payload, dict) else {})
    return {"data": result}
