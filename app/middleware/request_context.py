from __future__ import annotations

import uuid

from starlette.middleware.base import BaseHTTPMiddleware


def get_client_ip(request) -> str:
    header_candidates = (
        "cf-connecting-ip",
        "x-forwarded-for",
        "x-real-ip",
    )
    for header in header_candidates:
        raw_value = (request.headers.get(header) or "").strip()
        if not raw_value:
            continue
        if header == "x-forwarded-for":
            first_hop = raw_value.split(",")[0].strip()
            if first_hop:
                return first_hop
            continue
        return raw_value
    return request.client.host if request.client else "unknown"


class RequestContextMiddleware(BaseHTTPMiddleware):
    async def dispatch(self, request, call_next):
        request.state.request_id = str(uuid.uuid4())
        request.state.client_ip = get_client_ip(request)
        response = await call_next(request)
        response.headers["X-Request-ID"] = request.state.request_id
        return response
