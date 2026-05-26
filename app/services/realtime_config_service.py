from __future__ import annotations

import asyncio
import logging
import time
from typing import Any

from app.core.config import get_settings
try:
    from twilio.base.exceptions import TwilioRestException
    from twilio.rest import Client
except ModuleNotFoundError:  # pragma: no cover - dependency installed in deployed/runtime env
    TwilioRestException = Exception
    Client = None

settings = get_settings()
logger = logging.getLogger(__name__)

_ICE_CACHE_TTL_SECONDS = 20 * 60
_ice_cache: dict[str, Any] = {
    "expires_at": 0.0,
    "ice_servers": None,
    "error": "",
}


def _stun_only_servers() -> list[dict[str, Any]]:
    stun_url = str(settings.WEBRTC_STUN_URL or "").strip() or "stun:stun.l.google.com:19302"
    backup = "stun:stun1.l.google.com:19302"
    urls = [stun_url]
    if stun_url != backup:
        urls.append(backup)
    return [{"urls": urls}]


def twilio_credentials_configured() -> bool:
    return bool(str(settings.TWILIO_ACCOUNT_SID or "").strip() and str(settings.TWILIO_AUTH_TOKEN or "").strip())


def _build_twilio_client() -> Client:
    if Client is None:
        raise RuntimeError("Twilio SDK is not installed.")
    return Client(
        username=str(settings.TWILIO_ACCOUNT_SID or "").strip(),
        password=str(settings.TWILIO_AUTH_TOKEN or "").strip(),
    )


def _fetch_twilio_ice_servers_sync() -> list[dict[str, Any]]:
    if not twilio_credentials_configured():
        raise RuntimeError("TWILIO_ACCOUNT_SID is not configured.")

    try:
        token = _build_twilio_client().tokens.create()
    except TwilioRestException as exc:
        logger.warning(
            "twilio.ice_generation.rest_error status=%s code=%s message=%s",
            getattr(exc, "status", ""),
            getattr(exc, "code", ""),
            getattr(exc, "msg", str(exc)),
        )
        raise RuntimeError(f"Twilio token request failed with HTTP {getattr(exc, 'status', 'unknown')}") from exc
    except Exception as exc:
        logger.warning("twilio.ice_generation.client_error error=%s", exc)
        raise RuntimeError(f"Twilio token request failed: {exc}") from exc

    ice_servers = getattr(token, "ice_servers", None)
    if not isinstance(ice_servers, list) or not ice_servers:
        logger.warning("twilio.ice_generation.missing_ice_servers sid=%s", getattr(token, "sid", ""))
        raise RuntimeError("Twilio token response did not include ice_servers.")
    return ice_servers


async def get_dynamic_ice_servers(*, force_refresh: bool = False) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    now = time.monotonic()
    cached = _ice_cache.get("ice_servers")
    expires_at = float(_ice_cache.get("expires_at") or 0.0)
    if not force_refresh and cached and now < expires_at:
        return list(cached), {
            "provider": "twilio",
            "cached": True,
            "fallback": False,
            "error": "",
        }

    if not twilio_credentials_configured():
        error_message = "Twilio Network Traversal credentials are not configured."
        _ice_cache.update({"expires_at": 0.0, "ice_servers": None, "error": error_message})
        return _stun_only_servers(), {
            "provider": "stun-fallback",
            "cached": False,
            "fallback": True,
            "error": error_message,
        }

    try:
        ice_servers = await asyncio.to_thread(_fetch_twilio_ice_servers_sync)
        _ice_cache.update(
            {
                "expires_at": now + _ICE_CACHE_TTL_SECONDS,
                "ice_servers": list(ice_servers),
                "error": "",
            }
        )
        logger.info("twilio.ice_generation.success count=%s", len(ice_servers))
        return list(ice_servers), {
            "provider": "twilio",
            "cached": False,
            "fallback": False,
            "error": "",
        }
    except Exception as exc:
        error_message = str(exc)
        _ice_cache.update({"expires_at": 0.0, "ice_servers": None, "error": error_message})
        logger.exception("twilio.ice_generation.failed")
        return _stun_only_servers(), {
            "provider": "stun-fallback",
            "cached": False,
            "fallback": True,
            "error": error_message,
        }


def build_webrtc_rtc_config(*, force_relay: bool = False, ice_servers: list[dict[str, Any]] | None = None) -> dict[str, Any]:
    return {
        "iceServers": list(ice_servers or _stun_only_servers()),
        "iceTransportPolicy": "relay" if force_relay else "all",
    }


async def get_turn_diagnostics(*, force_refresh: bool = False) -> dict[str, Any]:
    warnings: list[str] = []
    configured = twilio_credentials_configured()
    ice_servers, metadata = await get_dynamic_ice_servers(force_refresh=force_refresh)
    production_ready = configured and not metadata["fallback"] and not metadata["error"]
    if not configured:
        warnings.append("Twilio Network Traversal Service credentials are not configured.")
    elif metadata["error"]:
        warnings.append(f"Twilio Network Traversal token generation failed: {metadata['error']}")

    return {
        "provider": "twilio",
        "configured": configured,
        "productionReady": production_ready,
        "accountSidConfigured": bool(str(settings.TWILIO_ACCOUNT_SID or "").strip()),
        "authTokenConfigured": bool(str(settings.TWILIO_AUTH_TOKEN or "").strip()),
        "stunConfigured": bool(_stun_only_servers()),
        "tlsEnabled": production_ready,
        "tcpEnabled": production_ready,
        "udpEnabled": production_ready,
        "warnings": warnings,
        "iceServersCount": len(ice_servers),
        "iceServerUrlsPreview": [entry.get("urls") for entry in ice_servers[:3]],
        "fallback": bool(metadata["fallback"]),
        "cached": bool(metadata["cached"]),
        "error": str(metadata["error"] or ""),
    }


async def webrtc_realtime_configured() -> bool:
    diagnostics = await get_turn_diagnostics()
    return bool(diagnostics["productionReady"])
