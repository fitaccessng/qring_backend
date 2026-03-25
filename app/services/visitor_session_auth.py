from __future__ import annotations

import hashlib
import hmac
import secrets
from datetime import datetime, timedelta

from sqlalchemy.orm import Session

from app.core.exceptions import AppException
from app.db.models import VisitorSession


VISITOR_SESSION_TOKEN_TTL_HOURS = 6


def _hash_token(token: str) -> str:
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


def issue_visitor_session_token(
    db: Session,
    *,
    session: VisitorSession,
    ttl_hours: int = VISITOR_SESSION_TOKEN_TTL_HOURS,
) -> str:
    """
    Rotate the visitor session token and persist only a hash. Return the raw token once.
    """
    raw = f"vst1_{secrets.token_urlsafe(32)}"
    session.visitor_token_hash = _hash_token(raw)
    session.visitor_token_expires_at = datetime.utcnow() + timedelta(hours=max(1, int(ttl_hours)))
    db.commit()
    db.refresh(session)
    return raw


def require_visitor_session_access(
    db: Session,
    *,
    session: VisitorSession,
    visitor_token: str | None,
) -> None:
    token = str(visitor_token or "").strip()
    if not token:
        # Backwards-compatible header for browser/mobile clients.
        # FastAPI passes headers separately, but our callers can feed it into visitor_token.
        token = ""
    if not token:
        raise AppException("Visitor token is required for this session.", status_code=401)
    if not session.visitor_token_hash or not session.visitor_token_expires_at:
        raise AppException("Visitor token is not available for this session.", status_code=401)
    if session.visitor_token_expires_at <= datetime.utcnow():
        raise AppException("Visitor token expired. Please rescan and try again.", status_code=401)
    provided_hash = _hash_token(token)
    if not hmac.compare_digest(provided_hash, session.visitor_token_hash):
        raise AppException("Invalid visitor token for this session.", status_code=401)
