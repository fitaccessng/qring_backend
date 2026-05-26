from __future__ import annotations

import logging

from fastapi import APIRouter

from app.core.config import get_settings
from app.services.realtime_config_service import get_turn_diagnostics, webrtc_realtime_configured
from app.services.realtime_runtime_service import get_realtime_runtime_snapshot
from app.socket.manager import socket_state

router = APIRouter()
settings = get_settings()
logger = logging.getLogger(__name__)


@router.get("/health")
async def health():
    turn = get_turn_diagnostics()
    runtime = get_realtime_runtime_snapshot()
    socket_diagnostics = await socket_state.diagnostics()
    status = "degraded" if socket_diagnostics.get("degraded") else "ok"
    return {
        "status": status,
        "realtimeConfigured": webrtc_realtime_configured(),
        "turnConfigured": turn["configured"],
        "stunUrl": settings.WEBRTC_STUN_URL,
        "environment": settings.ENVIRONMENT,
        "databaseBackend": settings.database_backend,
        "redisEnabled": settings.redis_enabled,
        "processRole": settings.PROCESS_ROLE,
        "turn": turn,
        "realtimeRuntime": runtime,
        "socketState": socket_diagnostics,
    }


@router.get("/health/realtime")
async def realtime_health():
    try:
        socket_diagnostics = await socket_state.diagnostics()
    except Exception as exc:
        logger.exception("health.realtime socket diagnostics failed")
        socket_diagnostics = {
            "activeSockets": 0,
            "activeRooms": 0,
            "activeSessions": {},
            "activeCalls": 0,
            "metrics": {},
            "degraded": True,
            "error": str(exc),
        }
    return {
        "status": "degraded" if socket_diagnostics.get("degraded") else "ok",
        "turn": get_turn_diagnostics(),
        "runtime": get_realtime_runtime_snapshot(),
        "socketState": socket_diagnostics,
    }
