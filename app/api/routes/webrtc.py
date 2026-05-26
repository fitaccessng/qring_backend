from __future__ import annotations

import logging

from fastapi import APIRouter

from app.services.realtime_config_service import get_dynamic_ice_servers

router = APIRouter()
logger = logging.getLogger(__name__)


@router.get("/webrtc/ice-servers")
async def get_webrtc_ice_servers():
    ice_servers, metadata = await get_dynamic_ice_servers()
    logger.info(
        "api.webrtc.ice_servers provider=%s fallback=%s cached=%s count=%s",
        metadata.get("provider"),
        metadata.get("fallback"),
        metadata.get("cached"),
        len(ice_servers),
    )
    return {
        "iceServers": ice_servers,
    }
