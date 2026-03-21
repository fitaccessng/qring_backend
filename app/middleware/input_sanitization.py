from __future__ import annotations

from urllib.parse import parse_qsl, urlencode

from starlette.middleware.base import BaseHTTPMiddleware

from app.core.sanitize import sanitize_json_bytes, sanitize_text


def _sanitize_query_string(raw: bytes) -> bytes:
    if not raw:
        return raw
    try:
        parsed = parse_qsl(raw.decode("utf-8"), keep_blank_values=True)
    except UnicodeDecodeError:
        return raw

    sanitized_pairs = [
        (sanitize_text(str(key)), sanitize_text(str(value)))
        for key, value in parsed
    ]
    return urlencode(sanitized_pairs, doseq=True).encode("utf-8")


class InputSanitizationMiddleware(BaseHTTPMiddleware):
    async def dispatch(self, request, call_next):
        if request.method == "OPTIONS":
            return await call_next(request)

        path = request.url.path or ""
        if not path.startswith("/api/"):
            return await call_next(request)

        request.scope["query_string"] = _sanitize_query_string(request.scope.get("query_string", b""))

        content_type = (request.headers.get("content-type") or "").lower()
        should_sanitize_json = (
            request.method in {"POST", "PUT", "PATCH"}
            and "application/json" in content_type
        )
        if not should_sanitize_json:
            return await call_next(request)

        body = await request.body()
        sanitized_body = sanitize_json_bytes(body)
        if sanitized_body == body:
            return await call_next(request)

        async def receive():
            return {"type": "http.request", "body": sanitized_body, "more_body": False}

        request._receive = receive
        request._body = sanitized_body

        return await call_next(request)
