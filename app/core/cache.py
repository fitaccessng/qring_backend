from __future__ import annotations

from collections.abc import Callable
from typing import TypeVar

from app.core.redis import get_cached_json, set_cached_json, prefixed_key


T = TypeVar("T")


def cache_key(*parts: str) -> str:
    return prefixed_key("cache", *parts)


def get_or_set_json(key: str, loader: Callable[[], T], ttl_seconds: int) -> T:
    cached = get_cached_json(key)
    if cached is not None:
        return cached
    value = loader()
    set_cached_json(key, value, ttl_seconds)
    return value
