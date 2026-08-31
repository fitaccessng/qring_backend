from __future__ import annotations

from datetime import datetime
from pydantic import BaseModel, model_validator
from typing import Optional


class VisitorRequestCreate(BaseModel):
    requestId: Optional[str] = None
    qrId: str
    doorId: Optional[str] = None
    doorName: Optional[str] = None
    name: Optional[str] = None
    phoneNumber: Optional[str] = None
    purpose: Optional[str] = None
    visitorType: Optional[str] = None
    deliveryOption: Optional[str] = None
    snapshotBase64: Optional[str] = None
    snapshotMime: Optional[str] = None
    deviceId: Optional[str] = None
    consentAccepted: Optional[bool] = None
    consentAcceptedAt: Optional[datetime] = None
    consentStorage: Optional[str] = None

    @model_validator(mode="before")
    @classmethod
    def normalize_visitor_request_aliases(cls, value):
        if not isinstance(value, dict):
            return value
        data = dict(value)
        alias_groups = {
            "name": ("visitorName", "visitor_name", "fullName", "full_name"),
            "phoneNumber": ("phone", "phone_number", "visitorPhone", "visitor_phone"),
            "purpose": ("purposeOfVisit", "purpose_of_visit", "visitPurpose", "visit_purpose"),
            "snapshotBase64": ("snapshot", "snapshot_base64", "photo", "photoBase64", "photo_base64"),
            "snapshotMime": ("snapshot_mime", "photoMime", "photo_mime"),
            "doorId": ("residentId", "resident_id", "homeownerId", "homeowner_id"),
        }
        for canonical, aliases in alias_groups.items():
            if data.get(canonical) not in (None, ""):
                continue
            for alias in aliases:
                if data.get(alias) not in (None, ""):
                    data[canonical] = data[alias]
                    break
        return data


class VisitorRequestResponse(BaseModel):
    sessionId: str
    status: str
