from __future__ import annotations

from pydantic import BaseModel
from typing import Optional


class DashboardOverviewResponse(BaseModel):
    metrics: dict
    activity: list
    waitingRoom: list
    session: Optional[dict]
    messages: list
    traffic: list[int]
    callControls: dict
