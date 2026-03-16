from pydantic import BaseModel


class VisitorRequestCreate(BaseModel):
    qrId: str
    doorId: str | None = None
    name: str | None = None
    phoneNumber: str | None = None
    purpose: str | None = None
    snapshotBase64: str | None = None
    snapshotMime: str | None = None
    deviceId: str | None = None


class VisitorRequestResponse(BaseModel):
    sessionId: str
    status: str
