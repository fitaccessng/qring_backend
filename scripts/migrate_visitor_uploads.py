from __future__ import annotations

import argparse
import json
import shutil
import sys
from pathlib import Path
from typing import Any

from app.core.config import get_settings
from app.db.models import Notification, VisitorSession, VisitorSnapshotAudit
from app.services.cloudinary_service import upload_snapshot_to_cloudinary
from app.db.session import SessionLocal

settings = get_settings()


def _storage_root(explicit_root: str | None = None) -> Path:
    raw = str(explicit_root or settings.MEDIA_STORAGE_PATH or "").strip()
    if raw:
        root = Path(raw)
    else:
        root = Path(__file__).resolve().parents[1] / "uploads"
    root.mkdir(parents=True, exist_ok=True)
    return root.resolve()


def _normalize_relative_path(value: str | None) -> str:
    return str(value or "").strip().lstrip("/")


def _is_legacy_upload_path(path: str) -> bool:
    clean = _normalize_relative_path(path)
    return bool(clean) and not clean.startswith("visitor-media/") and not clean.startswith(("firebase:", "data:"))


def _public_upload_url(relative_path: str) -> str:
    clean = _normalize_relative_path(relative_path)
    if not clean:
        return ""
    return f"/uploads/{clean}"


def _migrate_relative_path(relative_path: str) -> str:
    clean = _normalize_relative_path(relative_path)
    if not clean:
        return ""
    if clean.startswith("visitor-media/"):
        return clean
    return f"visitor-media/{clean}"


def _rewrite_upload_url(value: Any) -> Any:
    if not isinstance(value, str):
        return value
    clean = value.strip()
    if not clean.startswith("/uploads/") or clean.startswith("/uploads/visitor-media/"):
        return value
    return f"/uploads/visitor-media/{clean[len('/uploads/'):]}"


def _rewrite_upload_urls(value: Any) -> Any:
    if isinstance(value, dict):
        return {key: _rewrite_upload_urls(item) for key, item in value.items()}
    if isinstance(value, list):
        return [_rewrite_upload_urls(item) for item in value]
    return _rewrite_upload_url(value)


def _replace_with_cloudinary_urls(value: Any, cloud_map: dict[str, tuple[str, str]] | None) -> Any:
    if not cloud_map:
        return value
    if isinstance(value, dict):
        return {key: _replace_with_cloudinary_urls(item, cloud_map) for key, item in value.items()}
    if isinstance(value, list):
        return [_replace_with_cloudinary_urls(item, cloud_map) for item in value]
    if isinstance(value, str):
        mapped = cloud_map.get(value.strip())
        return mapped[0] if mapped else value
    return value


def _migrate_snapshot_audits(db, storage_root: Path, dry_run: bool, upload_to_cloud: bool = False) -> tuple[dict[str, int], dict[str, tuple[str, str]]]:
    stats = {
        "snapshot_rows_seen": 0,
        "snapshot_rows_updated": 0,
        "snapshot_files_moved": 0,
        "snapshot_files_missing": 0,
        "snapshot_files_already_migrated": 0,
        "snapshot_files_uploaded": 0,
        "snapshot_rows_cloud_migrated": 0,
    }

    rows = db.query(VisitorSnapshotAudit).all()
    # mapping from old public url -> (secure_url, public_id)
    cloud_map: dict[str, tuple[str, str]] = {}
    for row in rows:
        stats["snapshot_rows_seen"] += 1
        current_rel = _normalize_relative_path(row.media_path)
        if not _is_legacy_upload_path(current_rel):
            continue

        new_rel = _migrate_relative_path(current_rel)
        old_path = storage_root / current_rel
        # Attempt to upload the existing local file to Cloudinary when possible
        if old_path.exists() and old_path.is_file():
            try:
                if upload_to_cloud and not dry_run:
                    media_bytes = old_path.read_bytes()
                    mime = None
                    # best-effort mime type from extension
                    if str(old_path).lower().endswith(".png"):
                        mime = "image/png"
                    elif str(old_path).lower().endswith(('.jpg', '.jpeg')):
                        mime = "image/jpeg"
                    elif str(old_path).lower().endswith('.webp'):
                        mime = "image/webp"
                    result = upload_snapshot_to_cloudinary(
                        media_bytes=media_bytes,
                        mime_type=mime or "application/octet-stream",
                        filename_hint=old_path.name,
                        public_id_prefix=None,
                    )
                    if result is not None:
                        secure, pid = result.secure_url, result.public_id
                        cloud_map[_public_upload_url(current_rel)] = (secure, pid)
                        row.media_path = f"cloudinary:{pid}"
                        row.media_url = secure
                        row.cloudinary_public_id = pid
                        stats["snapshot_files_uploaded"] += 1
                        stats["snapshot_rows_cloud_migrated"] += 1
                    else:
                        # Cloudinary not configured or upload skipped; move file under visitor-media path like legacy script
                        new_path = storage_root / new_rel
                        new_path.parent.mkdir(parents=True, exist_ok=True)
                        if not new_path.exists():
                            shutil.move(str(old_path), str(new_path))
                            stats["snapshot_files_moved"] += 1
                        else:
                            stats["snapshot_files_already_migrated"] += 1
                        row.media_path = new_rel
                        row.media_url = _public_upload_url(new_rel)
                        stats["snapshot_rows_updated"] += 1
                else:
                    # dry-run path: report as would-upload
                    stats["snapshot_files_moved"] += 1
            except Exception:
                stats["snapshot_files_missing"] += 1
        else:
            stats["snapshot_files_missing"] += 1

        # If not migrated to cloud above and row still has legacy path, ensure DB reflects visitor-media layout
        if not row.media_url:
            row.media_path = new_rel
            row.media_url = _public_upload_url(new_rel)
            stats["snapshot_rows_updated"] += 1

    return stats, cloud_map


def _migrate_sessions(db, dry_run: bool, cloud_map: dict[str, tuple[str, str]] | None = None) -> dict[str, int]:
    stats = {
        "session_rows_seen": 0,
        "session_rows_updated": 0,
    }

    rows = db.query(VisitorSession).all()
    for row in rows:
        stats["session_rows_seen"] += 1
        changed = False
        for field_name in ("photo_url", "snapshot_url"):
            current_value = getattr(row, field_name)
            rewritten = _rewrite_upload_url(current_value)
            # If cloud_map is provided and current value maps to a cloud entry, prefer cloud secure_url
            if cloud_map and isinstance(current_value, str):
                mapped = cloud_map.get(str(current_value).strip())
                if mapped:
                    rewritten = mapped[0]
            if rewritten != current_value:
                setattr(row, field_name, rewritten)
                changed = True
        if changed:
            stats["session_rows_updated"] += 1

    return stats


def _migrate_notifications(db, dry_run: bool, cloud_map: dict[str, tuple[str, str]] | None = None) -> dict[str, int]:
    stats = {
        "notification_rows_seen": 0,
        "notification_rows_updated": 0,
    }

    rows = db.query(Notification).all()
    for row in rows:
        stats["notification_rows_seen"] += 1
        try:
            payload = json.loads(row.payload or "{}")
        except Exception:
            continue
        if not isinstance(payload, (dict, list)):
            continue
        rewritten = _rewrite_upload_urls(payload)
        replaced = _replace_with_cloudinary_urls(rewritten, cloud_map)
        if replaced != payload:
            row.payload = json.dumps(replaced, separators=(",", ":"))
            stats["notification_rows_updated"] += 1

    return stats


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Move legacy visitor upload files into uploads/visitor-media/... and rewrite persisted paths."
    )
    parser.add_argument(
        "--storage-root",
        default="",
        help="Override the media storage root. Defaults to MEDIA_STORAGE_PATH or backend/uploads.",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Print what would change without moving files or writing database updates.",
    )
    parser.add_argument(
        "--to-cloudinary",
        action="store_true",
        help="Upload found files to Cloudinary and update DB with secure_url/public_id. Requires Cloudinary env configured.",
    )
    args = parser.parse_args()

    storage_root = _storage_root(args.storage_root)
    db = SessionLocal()
    try:
        stats = {}
        snap_stats, cloud_map = _migrate_snapshot_audits(db, storage_root, dry_run=args.dry_run, upload_to_cloud=bool(args.to_cloudinary))
        stats.update(snap_stats)
        stats.update(_migrate_sessions(db, dry_run=args.dry_run, cloud_map=cloud_map))
        stats.update(_migrate_notifications(db, dry_run=args.dry_run, cloud_map=cloud_map))

        if args.dry_run:
            db.rollback()
            print(json.dumps({"dryRun": True, "storageRoot": str(storage_root), "stats": stats}, indent=2))
            return 0

        db.commit()
        print(json.dumps({"dryRun": False, "storageRoot": str(storage_root), "stats": stats}, indent=2))
        return 0
    except Exception as exc:
        db.rollback()
        print(f"Migration failed: {exc}", file=sys.stderr)
        return 1
    finally:
        db.close()


if __name__ == "__main__":
    sys.exit(main())
