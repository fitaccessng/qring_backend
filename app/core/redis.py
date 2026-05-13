from __future__ import annotations

import json
from functools import lru_cache
from typing import Any

from redis import Redis
from redis.asyncio import Redis as AsyncRedis

from app.core.config import get_settings


settings = get_settings()


def prefixed_key(*parts: str) -> str:
    prefix = (settings.REDIS_KEY_PREFIX or "qring").strip(": ")
    clean_parts = [str(part).strip(": ") for part in parts if str(part or "").strip()]
    return ":".join([prefix, *clean_parts]) if clean_parts else prefix


@lru_cache
def get_redis_client() -> Redis | None:
    if not settings.redis_enabled:
        return None
    return Redis.from_url(settings.REDIS_URL, decode_responses=True)


@lru_cache
def get_async_redis_client() -> AsyncRedis | None:
    if not settings.redis_enabled:
        return None
    return AsyncRedis.from_url(settings.REDIS_URL, decode_responses=True)


def get_cached_json(key: str) -> Any | None:
    client = get_redis_client()
    if client is None:
        return None
    raw = client.get(key)
    if raw is None:
        return None
    try:
        return json.loads(raw)
    except Exception:
        return None


def set_cached_json(key: str, value: Any, ttl_seconds: int | None = None) -> None:
    client = get_redis_client()
    if client is None:
        return
    payload = json.dumps(value, default=str)
    ttl = max(1, int(ttl_seconds or settings.CACHE_DEFAULT_TTL_SECONDS))
    client.set(key, payload, ex=ttl)


def delete_cached_keys(*keys: str) -> None:
    client = get_redis_client()
    if client is None:
        return
    filtered = [key for key in keys if key]
    if filtered:
        client.delete(*filtered)
