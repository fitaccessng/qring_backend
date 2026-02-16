from pydantic import BaseModel


class VisitorRequestCreate(BaseModel):
    qrId: str
    doorId: str | None = None
    name: str | None = None
    purpose: str | None = None


class VisitorRequestResponse(BaseModel):
    sessionId: str
    status: str
