#!/usr/bin/env python3
"""Manually deliver one confirmed SQLite menu through PushPlus."""

import argparse
import os
from datetime import date, timedelta

from db import get_db
from push_service import format_menu, load_menu_for_push, push_confirmed_menu


def resolve_menu_id(menu_id=None, menu_date=None):
    if menu_id is not None:
        return menu_id
    target = menu_date or (date.today() + timedelta(days=1)).isoformat()
    conn = get_db()
    try:
        row = conn.execute("SELECT id FROM menus WHERE date=?", (target,)).fetchone()
        return row["id"] if row else None
    finally:
        conn.close()


def main():
    parser = argparse.ArgumentParser(description="推送 SQLite 中已确认的菜单")
    parser.add_argument("--menu-id", type=int)
    parser.add_argument("--date", help="YYYY-MM-DD；默认明日")
    parser.add_argument(
        "--preview", nargs="?", const="preview_push_sqlite.html", metavar="PATH",
        help="只渲染正式 HTML 到文件，不调用 PushPlus",
    )
    args = parser.parse_args()
    menu_id = resolve_menu_id(args.menu_id, args.date)
    if menu_id is None:
        print("[ERROR] 找不到目标菜单")
        return 1
    if args.preview:
        menu = load_menu_for_push(menu_id)
        if menu["status"] != "confirmed":
            print(f"[ERROR] 菜单状态为 {menu['status']}，仅预览 confirmed 菜单")
            return 1
        output = os.path.abspath(args.preview)
        with open(output, "w", encoding="utf-8") as preview_file:
            preview_file.write("<!doctype html><meta charset=\"utf-8\"><title>Menu Push Preview</title>")
            preview_file.write(format_menu(menu))
        print(f"[OK] Preview 已生成: {output}")
        return 0
    ok, message = push_confirmed_menu(menu_id)
    print(("[OK] " if ok else "[ERROR] ") + message)
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
