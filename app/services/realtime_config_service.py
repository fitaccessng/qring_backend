from __future__ import annotations

import os

from app.core.config import get_settings

settings = get_settings()


def _read_env_value(*names: str) -> str:
    for name in names:
        value = os.getenv(name)
        if value and str(value).strip():
            return str(value).strip()
    return ""


def _configured_turn_urls() -> list[str]:
    urls: list[str] = []
    csv_urls = _read_env_value("WEBRTC_TURN_URLS", "TURN_URLS")
    if csv_urls:
        for part in csv_urls.split(","):
            value = str(part or "").strip()
            if value:
                urls.append(value)
    for raw in (
        settings.WEBRTC_TURN_URL,
        settings.WEBRTC_TURN_TLS_URL,
        _read_env_value("TURN_URL", "TURN_UDP_URL", "WEBRTC_TURN_TCP_URL", "TURN_TCP_URL"),
        _read_env_value("TURN_TLS_URL", "WEBRTC_TURN_TLS_TCP_URL"),
    ):
        value = str(raw or "").strip()
        if value and value not in urls:
            urls.append(value)
    return urls


def _turn_username() -> str:
    return str(
        settings.WEBRTC_TURN_USERNAME
        or _read_env_value("TURN_USERNAME", "WEBRTC_TURN_USER")
        or ""
    ).strip()


def _turn_credential() -> str:
    return str(
        settings.WEBRTC_TURN_CREDENTIAL
        or _read_env_value("TURN_PASSWORD", "TURN_CREDENTIAL", "WEBRTC_TURN_PASSWORD")
        or ""
    ).strip()


def build_webrtc_rtc_config(*, force_relay: bool = False) -> dict:
    ice_servers: list[dict] = []

    stun_url = str(settings.WEBRTC_STUN_URL or "").strip() or "stun:stun.l.google.com:19302"
    if stun_url:
        ice_servers.append({"urls": stun_url})

    turn_urls = _configured_turn_urls()
    if turn_urls:
        ice_servers.append(
            {
                "urls": turn_urls if len(turn_urls) > 1 else turn_urls[0],
                "username": _turn_username(),
                "credential": _turn_credential(),
            }
        )

    return {
        "iceServers": ice_servers,
        "iceTransportPolicy": "relay" if force_relay else "all",
    }


def webrtc_realtime_configured() -> bool:
    diagnostics = get_turn_diagnostics()
    return bool(diagnostics["configured"])


def get_turn_diagnostics() -> dict:
    turn_urls = _configured_turn_urls()
    return {
        "configured": bool(turn_urls and _turn_username() and _turn_credential()),
        "urls": turn_urls,
        "usernameConfigured": bool(_turn_username()),
        "credentialConfigured": bool(_turn_credential()),
        "tlsEnabled": any(str(url).startswith("turns:") for url in turn_urls),
        "tcpEnabled": any("transport=tcp" in str(url).lower() or str(url).startswith("turns:") for url in turn_urls),
        "udpEnabled": any("transport=udp" in str(url).lower() or str(url).startswith("turn:") for url in turn_urls),
    }
