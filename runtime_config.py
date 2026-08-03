"""Environment-backed runtime configuration shared by production services."""

import os
from urllib.parse import urlparse


def app_env():
    return os.environ.get("APP_ENV", "development").strip().lower()


def push_enabled():
    return app_env() == "production" and os.environ.get("PUSH_ENABLED", "false").strip().lower() == "true"


def server_host():
    return os.environ.get("HOST", "127.0.0.1").strip() or "127.0.0.1"


def photo_dir(base_dir):
    return os.path.abspath(
        os.environ.get("PHOTO_DIR")
        or os.environ.get("FAMILY_MENU_PHOTOS_DIR")
        or os.path.join(base_dir, "photos")
    )


def max_upload_bytes():
    try:
        value = int(os.environ.get("MAX_UPLOAD_BYTES", str(8 * 1024 * 1024)))
    except ValueError:
        value = 8 * 1024 * 1024
    return max(1024, value)


def validate_production_h5_url(value):
    value = (value or "").strip().rstrip("/")
    if app_env() != "production":
        return value
    if not value:
        raise ValueError("生产环境缺少 H5_BASE_URL")
    parsed = urlparse(value)
    if parsed.scheme != "https":
        raise ValueError("生产环境 H5_BASE_URL 必须使用 https://")
    hostname = (parsed.hostname or "").lower()
    if hostname in {"localhost", "127.0.0.1", "::1"}:
        raise ValueError("生产环境 H5_BASE_URL 不得指向 localhost")
    if not hostname:
        raise ValueError("生产环境 H5_BASE_URL 域名无效")
    return value


def validate_app_startup():
    if app_env() == "production":
        validate_production_h5_url(os.environ.get("H5_BASE_URL", ""))
