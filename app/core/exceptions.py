from __future__ import annotations

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
    @app.exception_handler(AppException)
    async def _app_exception_handler(_: Request, exc: AppException):
        return JSONResponse(
            status_code=exc.status_code,
            content={"message": exc.message, **({"code": exc.code} if exc.code else {}), **exc.extra},
        )

    @app.exception_handler(Exception)
    async def _unhandled_exception_handler(_: Request, exc: Exception):
        return JSONResponse(
            status_code=500,
            content={"message": "Internal server error", "detail": str(exc)},
        )
