from fastapi import APIRouter, Depends
from sqlalchemy.orm import Session

from app.api.deps import get_current_user
from app.db.models import User
from app.db.session import get_db
from app.services.dashboard_service import get_dashboard_overview

router = APIRouter()


@router.get("/overview")
def dashboard_overview(
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
):
    data = get_dashboard_overview(db, homeowner_id=user.id)
    return {"data": data}
