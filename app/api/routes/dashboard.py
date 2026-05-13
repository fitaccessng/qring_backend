from __future__ import annotations

from fastapi import APIRouter, Depends
from sqlalchemy.orm import Session

from app.api.deps import get_current_user
from app.core.cache import cache_key, get_or_set_json
from app.core.config import get_settings
from app.db.models import User
from app.db.session import get_db
from app.services.dashboard_service import get_dashboard_overview

router = APIRouter()
settings = get_settings()


@router.get("/overview")
def dashboard_overview(
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
):
    data = get_or_set_json(
        cache_key("dashboard-overview", user.id),
        lambda: get_dashboard_overview(db, homeowner_id=user.id),
        settings.CACHE_DASHBOARD_TTL_SECONDS,
    )
    return {"data": data}
