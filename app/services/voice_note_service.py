import base64
import json
import uuid
from datetime import datetime, timedelta
from pathlib import Path
from threading import Lock

from app.core.config import get_settings
from app.core.exceptions import AppException

settings = get_settings()

ALLOWED_EXTENSIONS = {".webm", ".ogg", ".wav", ".mp3", ".m4a"}
MAX_BYTES = 2 * 1024 * 1024
SIGNED_URL_TTL_DAYS = 7

_firebase_lock = Lock()
_firebase_app = None

try:
    import firebase_admin
    from firebase_admin import credentials as firebase_credentials
    from firebase_admin import storage as firebase_storage
except ImportError:
    firebase_admin = None
    firebase_credentials = None
    firebase_storage = None


def _resolve_extension(filename_hint: str, content_type: str | None = None) -> str:
    ext = Path(filename_hint or "").suffix.lower()
    if not ext and content_type:
        if "ogg" in content_type:
            ext = ".ogg"
        elif "wav" in content_type:
            ext = ".wav"
        elif "mpeg" in content_type or "mp3" in content_type:
            ext = ".mp3"
        elif "mp4" in content_type or "m4a" in content_type:
            ext = ".m4a"
        else:
            ext = ".webm"
    if ext not in ALLOWED_EXTENSIONS:
        ext = ".webm"
    return ext


def _load_firebase_credentials() -> dict | None:
    raw_json = (settings.FIREBASE_SERVICE_ACCOUNT_JSON or "").strip()
    if raw_json:
        return json.loads(raw_json)
    raw_base64 = (settings.FIREBASE_SERVICE_ACCOUNT_BASE64 or "").strip()
    if raw_base64:
        decoded = base64.b64decode(raw_base64).decode("utf-8")
        return json.loads(decoded)
    return None


def _resolve_storage_bucket() -> str | None:
    bucket = (settings.FIREBASE_STORAGE_BUCKET or "").strip()
    if bucket:
        return bucket
    project_id = (settings.FIREBASE_PROJECT_ID or "").strip()
    if project_id:
        return f"{project_id}.appspot.com"
    return None


def _get_firebase_app():
    global _firebase_app
    if firebase_admin is None or firebase_credentials is None or firebase_storage is None:
        return None
    if _firebase_app is not None:
        return _firebase_app
    with _firebase_lock:
        if _firebase_app is not None:
            return _firebase_app
        bucket_name = _resolve_storage_bucket()
        if not bucket_name:
            return None
        if firebase_admin._apps:
            _firebase_app = firebase_admin.get_app()
            return _firebase_app
        creds = _load_firebase_credentials()
        if creds:
            _firebase_app = firebase_admin.initialize_app(
                credential=firebase_credentials.Certificate(creds),
                options={"projectId": settings.FIREBASE_PROJECT_ID, "storageBucket": bucket_name},
            )
            return _firebase_app
        _firebase_app = firebase_admin.initialize_app(
            options={"projectId": settings.FIREBASE_PROJECT_ID, "storageBucket": bucket_name}
        )
        return _firebase_app


def _get_storage_bucket():
    if firebase_storage is None:
        return None
    bucket_name = _resolve_storage_bucket()
    if not bucket_name:
        return None
    app = _get_firebase_app()
    if app is None:
        return None
    return firebase_storage.bucket(bucket_name, app=app)


def _voice_note_base_dir() -> Path:
    raw = (settings.MEDIA_STORAGE_PATH or "").strip()
    if raw:
        base = Path(raw)
    else:
        base = Path(__file__).resolve().parents[2] / "uploads" / "voice-notes"
    base.mkdir(parents=True, exist_ok=True)
    return base


def save_voice_note(
    *,
    media_bytes: bytes,
    filename_hint: str,
    content_type: str | None,
    session_id: str,
) -> dict:
    if not media_bytes:
        raise AppException("Empty voice note upload.", status_code=400)
    if len(media_bytes) > MAX_BYTES:
        raise AppException("Voice note is too large.", status_code=400)

    bucket = _get_storage_bucket()
    if bucket is None:
        raise AppException("Firebase Storage is not configured.", status_code=503)

    ext = _resolve_extension(filename_hint, content_type)
    note_id = str(uuid.uuid4())
    filename = f"{session_id}-{note_id}{ext}"
    storage_path = f"voice-notes/{session_id}/{filename}"
    blob = bucket.blob(storage_path)
    blob.cache_control = "private, max-age=0, no-transform"
    blob.metadata = {
        "sessionId": session_id,
        "noteId": note_id,
    }
    blob.upload_from_string(media_bytes, content_type=content_type or "application/octet-stream")

    expires_at = datetime.utcnow() + timedelta(days=SIGNED_URL_TTL_DAYS)
    try:
        signed_url = blob.generate_signed_url(
            expiration=expires_at,
            method="GET",
            version="v4",
        )
    except Exception as exc:
        raise AppException("Unable to generate voice note URL.", status_code=500) from exc

    return {
        "id": note_id,
        "filename": filename,
        "path": storage_path,
        "contentType": content_type or "application/octet-stream",
        "url": signed_url,
    }


def load_voice_note(filename: str) -> tuple[bytes, str]:
    safe_name = Path(filename).name
    if not safe_name:
        raise AppException("Voice note not found.", status_code=404)
    path = _voice_note_base_dir() / safe_name
    if not path.exists():
        raise AppException("Voice note not found.", status_code=404)
    ext = path.suffix.lower()
    content_type = {
        ".webm": "audio/webm",
        ".ogg": "audio/ogg",
        ".wav": "audio/wav",
        ".mp3": "audio/mpeg",
        ".m4a": "audio/mp4",
    }.get(ext, "application/octet-stream")
    return path.read_bytes(), content_type
