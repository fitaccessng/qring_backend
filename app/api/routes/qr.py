from fastapi import APIRouter, Depends
from sqlalchemy.orm import Session

from app.db.session import get_db
from app.services.qr_service import resolve_qr

router = APIRouter()


@router.get("/resolve/{qr_id}")
def resolve(qr_id: str, db: Session = Depends(get_db)):
    return {"data": resolve_qr(db, qr_id)}
