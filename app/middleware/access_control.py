from starlette.middleware.base import BaseHTTPMiddleware
from starlette.responses import JSONResponse

from app.core.security import decode_token
from app.db.models import User
from app.db.session import SessionLocal

PROTECTED_PREFIXES = (
    "/api/v1/dashboard",
    "/api/v1/homeowner",
    "/api/v1/estate",
    "/api/v1/admin",
    "/api/v1/notifications",
)


class AccessControlMiddleware(BaseHTTPMiddleware):
    async def dispatch(self, request, call_next):
        path = request.url.path
        if request.method == "OPTIONS" or not path.startswith(PROTECTED_PREFIXES):
            return await call_next(request)

        request.state.db_access_mode = "read" if request.method in {"GET", "HEAD"} else "write"
        header_mode = (request.headers.get("x-db-access-mode") or "").strip().lower()
        if header_mode and header_mode != request.state.db_access_mode:
            return JSONResponse(
                status_code=403,
                content={"detail": "Access mode mismatch for request type"},
            )

        auth_header = request.headers.get("authorization") or ""
        if not auth_header.startswith("Bearer "):
            return JSONResponse(status_code=401, content={"detail": "Missing token"})

        token = auth_header.split(" ", 1)[1].strip()
        try:
            payload = decode_token(token)
        except ValueError:
            return JSONResponse(status_code=401, content={"detail": "Invalid or expired token"})

        if payload.get("type") != "access":
            return JSONResponse(status_code=401, content={"detail": "Invalid token type"})

        user_id = payload.get("sub")
        db = SessionLocal()
        try:
            user = db.query(User).filter(User.id == user_id).first()
            if not user or not user.is_active:
                return JSONResponse(status_code=401, content={"detail": "User not found"})
            request.state.authenticated_user_id = user.id
            request.state.authenticated_user_role = user.role.value
        finally:
            db.close()

        response = await call_next(request)
        response.headers["X-DB-Access-Mode"] = request.state.db_access_mode
        return response
