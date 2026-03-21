from __future__ import annotations

import json
import re
from typing import Any

CONTROL_CHAR_RE = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]")
HTML_TAG_RE = re.compile(r"<[^>]+>")
SPACE_RE = re.compile(r"[ \t]{2,}")

SENSITIVE_FIELD_NAMES = {
    "password",
    "currentpassword",
    "newpassword",
    "confirmpassword",
    "token",
    "idtoken",
    "accesstoken",
    "refreshtoken",
    "authorization",
    "signature",
    "photourl",
}


def sanitize_text(value: str) -> str:
    sanitized = CONTROL_CHAR_RE.sub("", value)
    sanitized = HTML_TAG_RE.sub("", sanitized)
    sanitized = SPACE_RE.sub(" ", sanitized)
    return sanitized.strip()


def _is_sensitive_field(path: tuple[str, ...]) -> bool:
    if not path:
        return False
    return path[-1].lower() in SENSITIVE_FIELD_NAMES


def sanitize_payload(value: Any, path: tuple[str, ...] = ()) -> Any:
    if isinstance(value, str):
        if _is_sensitive_field(path):
            return value
        return sanitize_text(value)

    if isinstance(value, list):
        return [sanitize_payload(item, path + (str(index),)) for index, item in enumerate(value)]

    if isinstance(value, dict):
        return {
            str(key): sanitize_payload(item, path + (str(key),))
            for key, item in value.items()
        }

    return value


def sanitize_json_bytes(raw: bytes) -> bytes:
    if not raw:
        return raw
    try:
        payload = json.loads(raw.decode("utf-8"))
    except (json.JSONDecodeError, UnicodeDecodeError):
        return raw

    sanitized = sanitize_payload(payload)
    return json.dumps(sanitized, ensure_ascii=False).encode("utf-8")
