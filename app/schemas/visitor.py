from __future__ import annotations

from pydantic import BaseModel
from typing import Optional


class VisitorRequestCreate(BaseModel):
    requestId: Optional[str] = None
    qrId: str
    doorId: Optional[str] = None
    name: Optional[str] = None
    phoneNumber: Optional[str] = None
    purpose: Optional[str] = None
    visitorType: Optional[str] = None
    deliveryOption: Optional[str] = None
    snapshotBase64: Optional[str] = None
    snapshotMime: Optional[str] = None
    deviceId: Optional[str] = None


class VisitorRequestResponse(BaseModel):
    sessionId: str
    status: str
