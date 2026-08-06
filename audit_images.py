#!/usr/bin/env python3
"""Read-only SQLite/filesystem image consistency audit."""

import json
import os
import sqlite3

from db import DB_PATH
from runtime_config import photo_dir

BASE_DIR = os.path.dirname(os.path.abspath(__file__))


def audit(db_path=DB_PATH, photos_path=None):
    photos_path = photos_path or photo_dir(BASE_DIR)
    conn = sqlite3.connect(f"file:{os.path.abspath(db_path)}?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row
    try:
        rows = conn.execute("SELECT id,name_cn,is_active,image FROM dishes ORDER BY id").fetchall()
    finally:
        conn.close()
    active_missing_image = [dict(r) for r in rows if r["is_active"] == 1 and not r["image"]]
    missing_files = [dict(r) for r in rows if r["image"] and not os.path.isfile(os.path.join(photos_path, r["image"]))]
    referenced = {r["image"] for r in rows if r["image"]}
    files = sorted(
        name for name in os.listdir(photos_path)
        if os.path.isfile(os.path.join(photos_path, name)) and not name.startswith(".")
    )
    return {
        "active_missing_image": active_missing_image,
        "missing_files": missing_files,
        "orphan_files": [name for name in files if name not in referenced],
        "inactive_with_image": [dict(r) for r in rows if r["is_active"] == 0 and r["image"]],
        "photo_file_count": len(files),
        "db_nonempty_image_count": sum(1 for r in rows if r["image"]),
        "active_image_count": sum(1 for r in rows if r["is_active"] == 1 and r["image"]),
        "active_count": sum(1 for r in rows if r["is_active"] == 1),
    }


if __name__ == "__main__":
    print(json.dumps(audit(), ensure_ascii=False, indent=2))
