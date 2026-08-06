#!/usr/bin/env python3
"""Run the Tomorrow page against an isolated copy of the real SQLite database."""

import os
import re
import sqlite3
from datetime import date, timedelta
from pathlib import Path


BASE_DIR = Path(__file__).resolve().parent
SOURCE_DB = BASE_DIR / "family_menu.db"
PREVIEW_DIR = BASE_DIR / ".local-preview"
PREVIEW_DB = PREVIEW_DIR / "family_menu_preview.db"


def copy_database():
    if not SOURCE_DB.is_file():
        raise FileNotFoundError(f"Source database not found: {SOURCE_DB}")
    PREVIEW_DIR.mkdir(exist_ok=True)
    with sqlite3.connect(f"file:{SOURCE_DB}?mode=ro", uri=True) as source:
        with sqlite3.connect(PREVIEW_DB) as target:
            source.backup(target)


def normalize_tomorrow_legacy_combos():
    """Expand legacy whole-meal labels into canonical dish rows in the copy."""
    aliases = {
        "16谷米饭": "dish_0090",
        "16谷粗粮饭": "dish_0090",
        "香煎澳洲和牛片": "dish_0005",
        "香港红薯": "dish_0208",
        "黑鱼子酱配嫩豆腐": "dish_0001",
        "松茸蒸鸡蛋": "dish_0131",
        "二米饭": "dish_0093",
    }
    tomorrow = (date.today() + timedelta(days=1)).isoformat()
    with sqlite3.connect(PREVIEW_DB) as conn:
        conn.row_factory = sqlite3.Row
        menu = conn.execute("SELECT id FROM menus WHERE date = ?", (tomorrow,)).fetchone()
        if not menu:
            return
        dishes = {
            row["name_cn"]: row["id"]
            for row in conn.execute("SELECT id, name_cn FROM dishes WHERE is_active = 1")
        }
        rows = conn.execute(
            "SELECT id, dish_id, meal_type, is_locked, sort_order, source "
            "FROM menu_items WHERE menu_id = ? AND dish_id NOT LIKE 'dish_%'",
            (menu["id"],),
        ).fetchall()
        for row in rows:
            # The target design intentionally uses an empty afternoon-tea state.
            if row["meal_type"] == "afternoon_snack":
                conn.execute("DELETE FROM menu_items WHERE id = ?", (row["id"],))
                continue
            resolved = []
            components = [part.strip() for part in re.split(r"\s*＋\s*", row["dish_id"]) if part.strip()]
            for component in components:
                alternatives = [part.strip() for part in re.split(r"\s*/\s*", component) if part.strip()]
                dish_id = next((dishes[name] for name in alternatives if name in dishes), None)
                if not dish_id:
                    dish_id = next((aliases[name] for name in alternatives if name in aliases), None)
                if dish_id and dish_id not in resolved:
                    resolved.append(dish_id)
            if not resolved:
                conn.execute("DELETE FROM menu_items WHERE id = ?", (row["id"],))
                continue
            conn.execute(
                "UPDATE menu_items SET dish_id = ?, source = 'legacy_preview' WHERE id = ?",
                (resolved[0], row["id"]),
            )
            for offset, dish_id in enumerate(resolved[1:], start=1):
                conn.execute(
                    "INSERT INTO menu_items "
                    "(menu_id, dish_id, meal_type, is_locked, sort_order, source) "
                    "VALUES (?, ?, ?, ?, ?, 'legacy_preview')",
                    (menu["id"], dish_id, row["meal_type"], row["is_locked"], (row["sort_order"] or 0) + offset),
                )
        conn.commit()


def main():
    copy_database()
    normalize_tomorrow_legacy_combos()

    # These must be set before importing the application modules.
    os.environ["APP_ENV"] = "development"
    os.environ["PUSH_ENABLED"] = "false"
    os.environ["FAMILY_MENU_DB_PATH"] = str(PREVIEW_DB)
    os.environ["HOST"] = "127.0.0.1"
    os.environ["LOCAL_PREVIEW_UI"] = "true"

    import app

    class LocalOwnerHandler(app.AppHandler):
        def request_role(self):
            return "owner"

    app.ensure_tomorrow_menu("shenzhen")
    server = app.ThreadingHTTPServer(("127.0.0.1", 8091), LocalOwnerHandler)
    print("[OK] Local owner preview: http://127.0.0.1:8091/tomorrow", flush=True)
    print(f"[OK] Database copy: {PREVIEW_DB}", flush=True)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()


if __name__ == "__main__":
    main()
