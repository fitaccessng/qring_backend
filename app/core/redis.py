from __future__ import annotations

import json
import logging
import time
from functools import lru_cache
from typing import Any

from redis import Redis
from redis.asyncio import Redis as AsyncRedis
from redis.backoff import ExponentialBackoff
from redis.retry import Retry

from app.core.config import get_settings


settings = get_settings()
logger = logging.getLogger(__name__)
_REDIS_HEALTH_TTL_SECONDS = 5
_redis_health_cache: dict[str, Any] = {"checked_at": 0.0, "value": None}


def prefixed_key(*parts: str) -> str:
    prefix = (settings.REDIS_KEY_PREFIX or "qring").strip(": ")
    clean_parts = [str(part).strip(": ") for part in parts if str(part or "").strip()]
    return ":".join([prefix, *clean_parts]) if clean_parts else prefix


def describe_redis_configuration() -> dict[str, Any]:
    return {
        "configured": settings.redis_enabled,
        "url": settings.redis_url_masked,
        "host": settings.redis_url_host,
        "looksPlaceholder": settings.redis_url_looks_placeholder,
        "productionLike": settings.production_like,
    }


def _redis_client_kwargs() -> dict[str, Any]:
    retry = Retry(
        ExponentialBackoff(
            cap=max(int(settings.REDIS_RETRY_MAX_SECONDS or 1), 1),
            base=max(int(settings.REDIS_RETRY_BASE_SECONDS or 1), 1),
        ),
        3,
    )
    return {
        "decode_responses": True,
        "socket_connect_timeout": max(float(settings.REDIS_CONNECT_TIMEOUT_SECONDS or 1), 0.1),
        "socket_timeout": max(float(settings.REDIS_SOCKET_TIMEOUT_SECONDS or 1), 0.1),
        "health_check_interval": max(int(settings.REDIS_HEALTHCHECK_INTERVAL_SECONDS or 0), 0),
        "retry": retry,
        "retry_on_timeout": True,
    }


@lru_cache
def get_redis_client() -> Redis | None:
    if not settings.redis_enabled:
        return None
    return Redis.from_url(settings.REDIS_URL, **_redis_client_kwargs())


@lru_cache
def get_async_redis_client() -> AsyncRedis | None:
    if not settings.redis_enabled:
        return None
    return AsyncRedis.from_url(settings.REDIS_URL, **_redis_client_kwargs())


async def get_async_redis_health(force: bool = False) -> dict[str, Any]:
    now = time.monotonic()
    cached_value = _redis_health_cache.get("value")
    checked_at = float(_redis_health_cache.get("checked_at") or 0.0)
    if not force and cached_value is not None and now - checked_at < _REDIS_HEALTH_TTL_SECONDS:
        return dict(cached_value)

    base = describe_redis_configuration()
    if not base["configured"]:
        result = {
            **base,
            "healthy": False,
            "reachable": False,
            "latencyMs": None,
            "error": "REDIS_URL is not configured.",
        }
        _redis_health_cache.update({"checked_at": now, "value": result})
        return dict(result)

    client = get_async_redis_client()
    if client is None:
        result = {
            **base,
            "healthy": False,
            "reachable": False,
            "latencyMs": None,
            "error": "Async Redis client is unavailable.",
        }
        _redis_health_cache.update({"checked_at": now, "value": result})
        return dict(result)

    started = time.perf_counter()
    try:
        await client.ping()
        latency_ms = round((time.perf_counter() - started) * 1000, 1)
        result = {
            **base,
            "healthy": True,
            "reachable": True,
            "latencyMs": latency_ms,
            "error": "",
        }
    except Exception as exc:
        result = {
            **base,
            "healthy": False,
            "reachable": False,
            "latencyMs": None,
            "error": str(exc),
        }
    _redis_health_cache.update({"checked_at": now, "value": result})
    return dict(result)


def get_cached_json(key: str) -> Any | None:
    client = get_redis_client()
    if client is None:
        return None
    try:
        raw = client.get(key)
    except Exception:
        logger.exception("redis_get_failed key=%s", key)
        return None
    if raw is None:
        return None
    try:
        return json.loads(raw)
    except Exception:
        logger.exception("redis_json_decode_failed key=%s", key)
        return None


def set_cached_json(key: str, value: Any, ttl_seconds: int | None = None) -> None:
    client = get_redis_client()
    if client is None:
        return
    try:
        payload = json.dumps(value, default=str)
        ttl = max(1, int(ttl_seconds or settings.CACHE_DEFAULT_TTL_SECONDS))
        client.set(key, payload, ex=ttl)
    except Exception:
        logger.exception("redis_set_failed key=%s", key)


def delete_cached_keys(*keys: str) -> None:
    client = get_redis_client()
    if client is None:
        return
    filtered = [key for key in keys if key]
    if filtered:
        try:
            client.delete(*filtered)
        except Exception:
            logger.exception("redis_delete_failed keys=%s", ",".join(filtered))
