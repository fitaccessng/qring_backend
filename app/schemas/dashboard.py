from pydantic import BaseModel


class DashboardOverviewResponse(BaseModel):
    metrics: dict
    activity: list
    waitingRoom: list
    session: dict | None
    messages: list
    traffic: list[int]
    callControls: dict
