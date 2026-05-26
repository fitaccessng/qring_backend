from __future__ import annotations

import logging

from fastapi import APIRouter

from app.core.config import get_settings
from app.core.redis import get_async_redis_health
from app.services.realtime_config_service import get_turn_diagnostics, webrtc_realtime_configured
from app.services.realtime_runtime_service import get_realtime_runtime_snapshot
from app.socket.manager import socket_state

router = APIRouter()
settings = get_settings()
logger = logging.getLogger(__name__)


@router.get("/health")
async def health():
    turn = get_turn_diagnostics()
    redis = await get_async_redis_health()
    runtime = get_realtime_runtime_snapshot()
    socket_diagnostics = await socket_state.diagnostics()
    degraded_reasons: list[str] = []
    if redis.get("configured") and not redis.get("healthy"):
        degraded_reasons.append(f"Redis unhealthy: {redis.get('error')}")
    if settings.production_like and not turn.get("productionReady"):
        degraded_reasons.extend(turn.get("warnings") or ["TURN is not production-ready."])
    if socket_diagnostics.get("degraded"):
        degraded_reasons.append(f"Socket state degraded: {socket_diagnostics.get('error')}")
    status = "degraded" if degraded_reasons else "ok"
    return {
        "status": status,
        "realtimeConfigured": webrtc_realtime_configured(),
        "turnConfigured": turn["configured"],
        "turnProductionReady": turn.get("productionReady"),
        "stunUrl": settings.WEBRTC_STUN_URL,
        "environment": settings.ENVIRONMENT,
        "databaseBackend": settings.database_backend,
        "redisEnabled": settings.redis_enabled,
        "processRole": settings.PROCESS_ROLE,
        "degradedReasons": degraded_reasons,
        "redis": redis,
        "turn": turn,
        "realtimeRuntime": runtime,
        "socketState": socket_diagnostics,
    }


@router.get("/health/realtime")
async def realtime_health():
    try:
        redis = await get_async_redis_health()
        socket_diagnostics = await socket_state.diagnostics()
    except Exception as exc:
        logger.exception("health.realtime socket diagnostics failed")
        redis = await get_async_redis_health()
        socket_diagnostics = {
            "activeSockets": 0,
            "activeRooms": 0,
            "activeSessions": {},
            "activeCalls": 0,
            "metrics": {},
            "degraded": True,
            "error": str(exc),
        }
    turn = get_turn_diagnostics()
    degraded_reasons: list[str] = []
    if redis.get("configured") and not redis.get("healthy"):
        degraded_reasons.append(f"Redis unhealthy: {redis.get('error')}")
    if settings.production_like and not turn.get("productionReady"):
        degraded_reasons.extend(turn.get("warnings") or ["TURN is not production-ready."])
    if socket_diagnostics.get("degraded"):
        degraded_reasons.append(f"Socket state degraded: {socket_diagnostics.get('error')}")
    return {
        "status": "degraded" if degraded_reasons else "ok",
        "degradedReasons": degraded_reasons,
        "redis": redis,
        "turn": turn,
        "runtime": get_realtime_runtime_snapshot(),
        "socketState": socket_diagnostics,
    }
