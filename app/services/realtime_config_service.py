from __future__ import annotations

from app.core.config import get_settings

settings = get_settings()


def _configured_turn_urls() -> list[str]:
    urls: list[str] = []
    for raw in (settings.WEBRTC_TURN_URL, settings.WEBRTC_TURN_TLS_URL):
        value = str(raw or "").strip()
        if value:
            urls.append(value)
    return urls


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
                "username": str(settings.WEBRTC_TURN_USERNAME or "").strip(),
                "credential": str(settings.WEBRTC_TURN_CREDENTIAL or "").strip(),
            }
        )

    return {
        "iceServers": ice_servers,
        "iceTransportPolicy": "relay" if force_relay else "all",
    }


def webrtc_realtime_configured() -> bool:
    return bool(
        _configured_turn_urls()
        and str(settings.WEBRTC_TURN_USERNAME or "").strip()
        and str(settings.WEBRTC_TURN_CREDENTIAL or "").strip()
    )
