from __future__ import annotations

from fastapi import APIRouter

from app.api.routes import (
    advanced,
    alert,
    calls,
    admin,
    auth,
    dashboard,
    estate,
    health,
    homeowner,
    livekit,
    media,
    notifications,
    payment,
    panic,
    subscription_policy,
    qr,
    safety,
    security,
    visitor,
    ws_gateway,
)

api_router = APIRouter()
api_router.include_router(health.router, tags=["health"])
api_router.include_router(auth.router, prefix="/auth", tags=["auth"])
api_router.include_router(dashboard.router, prefix="/dashboard", tags=["dashboard"])
api_router.include_router(homeowner.router, prefix="/homeowner", tags=["homeowner"])
api_router.include_router(qr.router, prefix="/qr", tags=["qr"])
api_router.include_router(panic.router, prefix="/panic", tags=["panic"])
api_router.include_router(safety.router, prefix="/safety", tags=["safety"])
api_router.include_router(security.router, prefix="/security", tags=["security"])
api_router.include_router(visitor.router, prefix="/visitor", tags=["visitor"])
api_router.include_router(admin.router, prefix="/admin", tags=["admin"])
api_router.include_router(estate.router, prefix="/estate", tags=["estate"])
api_router.include_router(alert.router, prefix="/alert", tags=["alert"])
api_router.include_router(payment.router, prefix="/payment", tags=["payment"])
api_router.include_router(subscription_policy.router, prefix="/payment", tags=["payment"])
api_router.include_router(notifications.router, prefix="/notifications", tags=["notifications"])
api_router.include_router(ws_gateway.router, tags=["websocket"])
api_router.include_router(calls.router, prefix="/calls", tags=["calls"])
api_router.include_router(livekit.router, tags=["livekit"])
api_router.include_router(advanced.router, prefix="/advanced", tags=["advanced"])
api_router.include_router(media.router, tags=["media"])
