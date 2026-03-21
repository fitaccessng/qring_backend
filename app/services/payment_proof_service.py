from __future__ import annotations

import uuid
from pathlib import Path

from app.core.config import get_settings
from app.core.exceptions import AppException

settings = get_settings()

ALLOWED_EXTENSIONS = {".png", ".jpg", ".jpeg", ".pdf"}
MAX_BYTES = 4 * 1024 * 1024


def _proof_base_dir() -> Path:
    raw = (settings.MEDIA_STORAGE_PATH or "").strip()
    if raw:
        base = Path(raw)
    else:
        base = Path(__file__).resolve().parents[2] / "uploads" / "payment-proofs"
    base.mkdir(parents=True, exist_ok=True)
    return base


def _resolve_extension(filename_hint: str, content_type: str | None = None) -> str:
    ext = Path(filename_hint or "").suffix.lower()
    if ext in ALLOWED_EXTENSIONS:
        return ext
    if content_type:
        if "png" in content_type:
            return ".png"
        if "jpg" in content_type or "jpeg" in content_type:
            return ".jpg"
        if "pdf" in content_type:
            return ".pdf"
    return ".jpg"


def save_payment_proof(
    *,
    media_bytes: bytes,
    filename_hint: str,
    content_type: str | None,
    alert_id: str,
    homeowner_id: str,
) -> dict:
    if not media_bytes:
        raise AppException("Empty payment proof upload.", status_code=400)
    if len(media_bytes) > MAX_BYTES:
        raise AppException("Payment proof is too large (max 4MB).", status_code=400)

    ext = _resolve_extension(filename_hint, content_type)
    if ext not in ALLOWED_EXTENSIONS:
        raise AppException("Unsupported proof file type.", status_code=400)

    proof_id = uuid.uuid4().hex[:12]
    filename = f"{alert_id}-{homeowner_id}-{proof_id}{ext}"
    path = _proof_base_dir() / filename
    path.write_bytes(media_bytes)

    return {
        "filename": filename,
        "contentType": content_type or "application/octet-stream",
        "url": f"/media/payment-proofs/{filename}",
    }


def load_payment_proof(filename: str) -> tuple[bytes, str]:
    safe_name = Path(filename).name
    if not safe_name:
        raise AppException("Payment proof not found.", status_code=404)
    path = _proof_base_dir() / safe_name
    if not path.exists():
        raise AppException("Payment proof not found.", status_code=404)
    ext = path.suffix.lower()
    content_type = {
        ".png": "image/png",
        ".jpg": "image/jpeg",
        ".jpeg": "image/jpeg",
        ".pdf": "application/pdf",
    }.get(ext, "application/octet-stream")
    return path.read_bytes(), content_type
