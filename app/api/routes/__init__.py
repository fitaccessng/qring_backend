from fastapi import APIRouter

from app.api.routes import admin, auth, dashboard, estate, health, homeowner, notifications, payment, qr, visitor, ws_gateway

api_router = APIRouter()
api_router.include_router(health.router, tags=["health"])
api_router.include_router(auth.router, prefix="/auth", tags=["auth"])
api_router.include_router(dashboard.router, prefix="/dashboard", tags=["dashboard"])
api_router.include_router(homeowner.router, prefix="/homeowner", tags=["homeowner"])
api_router.include_router(qr.router, prefix="/qr", tags=["qr"])
api_router.include_router(visitor.router, prefix="/visitor", tags=["visitor"])
api_router.include_router(admin.router, prefix="/admin", tags=["admin"])
api_router.include_router(estate.router, prefix="/estate", tags=["estate"])
api_router.include_router(payment.router, prefix="/payment", tags=["payment"])
api_router.include_router(notifications.router, prefix="/notifications", tags=["notifications"])
api_router.include_router(ws_gateway.router, tags=["websocket"])

