from app.core.exceptions import AppException


def select_door(doors: list[str], mode: str, requested_door: str | None = None) -> str:
    if not doors:
        raise AppException("No doors configured for QR", status_code=404)

    if mode == "direct":
        return doors[0]

    if requested_door and requested_door in doors:
        return requested_door

    if mode == "selector":
        raise AppException("Door selection required", status_code=400)

    return doors[0]
