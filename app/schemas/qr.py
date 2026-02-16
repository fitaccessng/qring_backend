from pydantic import BaseModel


class QRResolveResponse(BaseModel):
    qr_id: str
    plan: str
    home_id: str
    doors: list[str]
    mode: str
    estate_id: str | None
    active: bool
