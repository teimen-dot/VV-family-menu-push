"""Photo path and upload validation helpers."""

import os
import re
from urllib.parse import unquote


class PhotoValidationError(ValueError):
    pass


def resolve_photo_path(photo_root, requested_name):
    decoded = unquote(requested_name or "")
    if not decoded or decoded in {".", ".."}:
        raise PhotoValidationError("invalid photo path")
    if decoded != os.path.basename(decoded) or "/" in decoded or "\\" in decoded or "\x00" in decoded:
        raise PhotoValidationError("invalid photo path")
    root = os.path.realpath(photo_root)
    target = os.path.realpath(os.path.join(root, decoded))
    if os.path.commonpath([root, target]) != root:
        raise PhotoValidationError("invalid photo path")
    return target


def safe_slug(value):
    slug = re.sub(r"[^a-z0-9_]+", "_", (value or "").strip().lower()).strip("_")
    if not slug:
        raise PhotoValidationError("invalid image name")
    return slug[:120]


def validate_image_bytes(data, max_bytes):
    if not data:
        raise PhotoValidationError("empty image")
    if len(data) > max_bytes:
        raise PhotoValidationError(f"image exceeds {max_bytes} bytes")
    if data.startswith(b"\xff\xd8\xff"):
        return ".jpg"
    if data.startswith(b"\x89PNG\r\n\x1a\n"):
        return ".png"
    raise PhotoValidationError("only JPEG and PNG images are allowed")
