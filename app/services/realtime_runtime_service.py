from __future__ import annotations

import logging
from datetime import datetime
from typing import Any

logger = logging.getLogger(__name__)

_startup_at = datetime.utcnow()
_state: dict[str, Any] = {
    "websocketInitialized": False,
    "redisConfigured": False,
    "redisConnected": False,
    "redisError": "",
    "redisUrl": "",
    "redisHost": "",
    "redisAdapterMode": "memory",
    "socketNamespaces": [],
    "socketPath": "",
    "transportModes": ["polling", "websocket"],
    "socketServerMounted": False,
    "socketRedisAdapterAttached": False,
    "turnConfigured": False,
    "turnRequired": False,
    "turnWarnings": [],
    "degradedReasons": [],
    "startupDiagnostics": [],
}


def mark_realtime_state(**updates: Any) -> None:
    for key, value in updates.items():
        _state[key] = value


def append_startup_diagnostic(message: str, *, level: str = "info", code: str = "") -> None:
    row = {
        "message": message,
        "level": str(level or "info").lower(),
        "code": str(code or "").strip(),
        "at": datetime.utcnow().isoformat(),
    }
    _state.setdefault("startupDiagnostics", []).append(row)
    _state["startupDiagnostics"] = _state["startupDiagnostics"][-20:]
    log_method = getattr(logger, row["level"], logger.info)
    log_method("realtime.startup code=%s %s", row["code"] or "n/a", message)


def get_realtime_runtime_snapshot() -> dict[str, Any]:
    return {
        **_state,
        "uptimeSeconds": max(0, int((datetime.utcnow() - _startup_at).total_seconds())),
    }
