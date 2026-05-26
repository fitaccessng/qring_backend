from __future__ import annotations

import logging

from starlette.middleware.base import BaseHTTPMiddleware
from starlette.responses import JSONResponse

from app.core.security import decode_token

PROTECTED_PREFIXES = (
    "/api/v1/dashboard",
    "/api/v1/homeowner",
    "/api/v1/estate",
    "/api/v1/admin",
    "/api/v1/notifications",
)
logger = logging.getLogger(__name__)


class AccessControlMiddleware(BaseHTTPMiddleware):
    async def dispatch(self, request, call_next):
        path = request.url.path
        if request.method == "OPTIONS" or not path.startswith(PROTECTED_PREFIXES):
            return await call_next(request)

        request.state.db_access_mode = "read" if request.method in {"GET", "HEAD"} else "write"
        header_mode = (request.headers.get("x-db-access-mode") or "").strip().lower()
        if header_mode and header_mode != request.state.db_access_mode:
            logger.warning("access_control.denied reason=mode_mismatch path=%s expected=%s actual=%s", path, request.state.db_access_mode, header_mode)
            return JSONResponse(
                status_code=403,
                content={"detail": "Access mode mismatch for request type"},
            )

        auth_header = request.headers.get("authorization") or ""
        if not auth_header.startswith("Bearer "):
            logger.warning("access_control.denied reason=missing_bearer path=%s origin=%s", path, request.headers.get("origin"))
            return JSONResponse(status_code=401, content={"detail": "Missing token"})

        token = auth_header.split(" ", 1)[1].strip()
        try:
            payload = decode_token(token)
        except ValueError:
            logger.warning("access_control.denied reason=invalid_or_expired_token path=%s", path)
            return JSONResponse(status_code=401, content={"detail": "Invalid or expired token"})

        if payload.get("type") != "access":
            logger.warning("access_control.denied reason=invalid_token_type path=%s token_type=%s", path, payload.get("type"))
            return JSONResponse(status_code=401, content={"detail": "Invalid token type"})

        user_id = str(payload.get("sub") or "").strip()
        user_role = str(payload.get("role") or "").strip()
        if not user_id:
            logger.warning("access_control.denied reason=missing_subject path=%s", path)
            return JSONResponse(status_code=401, content={"detail": "Invalid token subject"})
        request.state.authenticated_user_id = user_id
        request.state.authenticated_user_role = user_role
        logger.info("access_control.allowed path=%s user_id=%s role=%s", path, user_id, user_role)

        response = await call_next(request)
        response.headers["X-DB-Access-Mode"] = request.state.db_access_mode
        return response
