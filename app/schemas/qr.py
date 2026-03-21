from __future__ import annotations

from pydantic import BaseModel
from typing import Optional


class QRResolveResponse(BaseModel):
    qr_id: str
    plan: str
    home_id: str
    doors: list[str]
    mode: str
    estate_id: Optional[str]
    active: bool
