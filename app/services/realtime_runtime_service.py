from __future__ import annotations

import logging
from datetime import datetime
from typing import Any

logger = logging.getLogger(__name__)

_startup_at = datetime.utcnow()
_state: dict[str, Any] = {
    "websocketInitialized": False,
    "redisConnected": False,
    "redisError": "",
    "socketNamespaces": [],
    "socketPath": "",
    "transportModes": ["polling", "websocket"],
    "socketServerMounted": False,
    "socketRedisAdapterAttached": False,
    "startupDiagnostics": [],
}


def mark_realtime_state(**updates: Any) -> None:
    for key, value in updates.items():
        _state[key] = value


def append_startup_diagnostic(message: str) -> None:
    row = {
        "message": message,
        "at": datetime.utcnow().isoformat(),
    }
    _state.setdefault("startupDiagnostics", []).append(row)
    _state["startupDiagnostics"] = _state["startupDiagnostics"][-20:]
    logger.info("realtime.startup %s", message)


def get_realtime_runtime_snapshot() -> dict[str, Any]:
    return {
        **_state,
        "uptimeSeconds": max(0, int((datetime.utcnow() - _startup_at).total_seconds())),
    }
