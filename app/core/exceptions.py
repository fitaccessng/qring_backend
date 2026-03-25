from __future__ import annotations

import logging
import uuid

from app.core.config import get_settings

try:
    from fastapi import FastAPI, Request
    from fastapi.responses import JSONResponse
except ModuleNotFoundError:  # pragma: no cover - local test fallback
    FastAPI = object
    Request = object

    class JSONResponse(dict):
        def __init__(self, status_code: int, content: dict):
            super().__init__(status_code=status_code, content=content)


class AppException(Exception):
    def __init__(self, message: str, status_code: int = 400, code: str | None = None, extra: dict | None = None):
        self.message = message
        self.status_code = status_code
        self.code = code
        self.extra = extra or {}
        super().__init__(message)


def register_exception_handlers(app: FastAPI) -> None:
    settings = get_settings()
    logger = logging.getLogger(__name__)

    @app.exception_handler(AppException)
    async def _app_exception_handler(_: Request, exc: AppException):
        return JSONResponse(
            status_code=exc.status_code,
            content={"message": exc.message, **({"code": exc.code} if exc.code else {}), **exc.extra},
        )

    @app.exception_handler(Exception)
    async def _unhandled_exception_handler(request: Request, exc: Exception):
        request_id = uuid.uuid4().hex[:12]
        try:
            logger.exception(
                "unhandled_exception request_id=%s method=%s path=%s",
                request_id,
                getattr(request, "method", ""),
                getattr(getattr(request, "url", None), "path", ""),
            )
        except Exception:
            # Avoid exception handler loops.
            pass
        return JSONResponse(
            status_code=500,
            content={
                "message": "Internal server error",
                "requestId": request_id,
                **({"detail": str(exc)} if getattr(settings, "DEBUG", False) else {}),
            },
        )
