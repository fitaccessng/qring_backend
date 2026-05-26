from __future__ import annotations

import logging

from fastapi import Depends, HTTPException, Request, status
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from sqlalchemy.orm import Session

from app.core.security import decode_token
from app.db.models import User
from app.db.session import get_db

bearer_scheme = HTTPBearer(auto_error=False)
logger = logging.getLogger(__name__)


def get_current_user(
    request: Request,
    credentials: HTTPAuthorizationCredentials = Depends(bearer_scheme),
    db: Session = Depends(get_db),
) -> User:
    if not credentials:
        logger.warning("auth.failed reason=missing_token path=%s origin=%s", request.url.path, request.headers.get("origin"))
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Missing token")

    try:
        payload = decode_token(credentials.credentials)
    except ValueError:
        logger.warning("auth.failed reason=invalid_or_expired_token path=%s origin=%s", request.url.path, request.headers.get("origin"))
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Invalid or expired token")

    if payload.get("type") != "access":
        logger.warning("auth.failed reason=invalid_token_type path=%s token_type=%s", request.url.path, payload.get("type"))
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Invalid token type")

    user = db.query(User).filter(User.id == payload.get("sub")).first()
    if not user or not user.is_active:
        logger.warning("auth.failed reason=user_not_found path=%s user_id=%s", request.url.path, payload.get("sub"))
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="User not found")
    request.state.authenticated_user_id = user.id
    request.state.authenticated_user_role = user.role.value
    logger.info("auth.success path=%s user_id=%s role=%s", request.url.path, user.id, user.role.value)
    return user


def get_optional_current_user(
    request: Request,
    credentials: HTTPAuthorizationCredentials = Depends(bearer_scheme),
    db: Session = Depends(get_db),
) -> User | None:
    if not credentials:
        return None

    try:
        payload = decode_token(credentials.credentials)
    except ValueError:
        logger.warning("auth.optional_failed reason=invalid_or_expired_token path=%s", request.url.path)
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Invalid or expired token")

    if payload.get("type") != "access":
        logger.warning("auth.optional_failed reason=invalid_token_type path=%s token_type=%s", request.url.path, payload.get("type"))
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Invalid token type")

    user = db.query(User).filter(User.id == payload.get("sub")).first()
    if not user or not user.is_active:
        logger.warning("auth.optional_failed reason=user_not_found path=%s user_id=%s", request.url.path, payload.get("sub"))
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="User not found")
    request.state.authenticated_user_id = user.id
    request.state.authenticated_user_role = user.role.value
    logger.info("auth.optional_success path=%s user_id=%s role=%s", request.url.path, user.id, user.role.value)
    return user


def require_roles(*roles: str):
    def dependency(user: User = Depends(get_current_user)) -> User:
        if user.role.value not in roles:
            raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Insufficient permissions")
        return user

    return dependency
