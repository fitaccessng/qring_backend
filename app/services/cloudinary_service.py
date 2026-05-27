from __future__ import annotations

import hashlib
import hmac
import time
from dataclasses import dataclass
from typing import Any

import httpx

from app.core.config import get_settings
from app.core.exceptions import AppException

settings = get_settings()


@dataclass(slots=True)
class CloudinaryUploadResult:
    secure_url: str
    public_id: str
    asset_id: str | None = None
    resource_type: str = "image"
    bytes: int | None = None


def _cloudinary_configured() -> bool:
    return all(
        str(value or "").strip()
        for value in (
            settings.CLOUDINARY_CLOUD_NAME,
            settings.CLOUDINARY_API_KEY,
            settings.CLOUDINARY_API_SECRET,
        )
    )


def _normalize_folder(folder: str | None) -> str:
    value = str(folder or settings.CLOUDINARY_UPLOAD_FOLDER or "").strip().strip("/")
    return value or "qring/visitor-snapshots"


def _sign_payload(params: dict[str, Any]) -> str:
    payload = "&".join(
        f"{key}={value}"
        for key, value in sorted(params.items())
        if value not in (None, "", [])
    )
    secret = str(settings.CLOUDINARY_API_SECRET or "").strip()
    return hmac.new(secret.encode("utf-8"), payload.encode("utf-8"), hashlib.sha1).hexdigest()


def upload_snapshot_to_cloudinary(
    *,
    media_bytes: bytes,
    mime_type: str,
    filename_hint: str,
    folder: str | None = None,
    public_id_prefix: str | None = None,
) -> CloudinaryUploadResult | None:
    if not _cloudinary_configured():
        return None

    folder_name = _normalize_folder(folder)
    timestamp = int(time.time())
    public_id = f"{str(public_id_prefix or 'visitor').strip().replace(' ', '_')}_{timestamp}"
    params = {
        "timestamp": timestamp,
        "folder": folder_name,
        "public_id": public_id,
        "overwrite": "false",
        "unique_filename": "true",
    }
    signature = _sign_payload(params)
    upload_url = f"https://api.cloudinary.com/v1_1/{settings.CLOUDINARY_CLOUD_NAME}/image/upload"

    try:
        with httpx.Client(timeout=httpx.Timeout(20.0, connect=5.0)) as client:
            response = client.post(
                upload_url,
                data={
                    **{key: str(value) for key, value in params.items()},
                    "api_key": settings.CLOUDINARY_API_KEY,
                    "signature": signature,
                },
                files={
                    "file": (
                        filename_hint or "snapshot.jpg",
                        media_bytes,
                        mime_type or "application/octet-stream",
                    )
                },
            )
    except Exception as exc:
        raise AppException("Snapshot upload to Cloudinary failed.", status_code=502) from exc

    if response.status_code >= 400:
        raise AppException("Snapshot upload to Cloudinary failed.", status_code=502)

    payload = response.json()
    secure_url = str(payload.get("secure_url") or "").strip()
    response_public_id = str(payload.get("public_id") or "").strip()
    if not secure_url or not response_public_id:
        raise AppException("Snapshot upload to Cloudinary failed.", status_code=502)

    return CloudinaryUploadResult(
        secure_url=secure_url,
        public_id=response_public_id,
        asset_id=str(payload.get("asset_id") or "").strip() or None,
        resource_type=str(payload.get("resource_type") or "image").strip() or "image",
        bytes=int(payload.get("bytes") or len(media_bytes)),
    )


def destroy_cloudinary_asset(public_id: str) -> bool:
    if not _cloudinary_configured():
        return False

    normalized_public_id = str(public_id or "").strip()
    if not normalized_public_id:
        return False

    timestamp = int(time.time())
    params = {"public_id": normalized_public_id, "timestamp": timestamp}
    signature = _sign_payload(params)
    destroy_url = f"https://api.cloudinary.com/v1_1/{settings.CLOUDINARY_CLOUD_NAME}/image/destroy"
    try:
        with httpx.Client(timeout=httpx.Timeout(20.0, connect=5.0)) as client:
            response = client.post(
                destroy_url,
                data={
                    "public_id": normalized_public_id,
                    "timestamp": str(timestamp),
                    "api_key": settings.CLOUDINARY_API_KEY,
                    "signature": signature,
                },
            )
    except Exception:
        return False

    if response.status_code >= 400:
        return False
    try:
        return response.json().get("result") in {"ok", "not found"}
    except Exception:
        return False
