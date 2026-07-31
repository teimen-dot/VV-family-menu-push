#!/usr/bin/env python3
"""
V11 迁移脚本:
1. 初始化 dish_preference_stats 表
2. 添加「婆婆 Grandma」到 diners 表
3. 初始化 catalog_version config key
4. 确保 menus 表有 meal_mode / banquet_total_diners 列
"""

import sys
import os
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from db import init_db, get_db, set_config, get_config


def migrate():
    # 1. Run init_db (idempotent — creates new tables/columns)
    init_db()

    conn = get_db()
    c = conn.cursor()

    # 2. Add「婆婆 Grandma」diner if not exists
    existing = c.execute("SELECT id FROM diners WHERE id = 'grandma'").fetchone()
    if not existing:
        c.execute(
            "INSERT INTO diners (id, name_cn, name_en, role, default_attends, sort_order) "
            "VALUES ('grandma', '婆婆', 'Grandma', 'family', 0, 4)"
        )
        print("[OK] Added diner: 婆婆 Grandma")
    else:
        print("[SKIP] Diner 婆婆 Grandma already exists")

    # 3. Initialize catalog_version if not exists (use same connection)
    cv_row = c.execute("SELECT value FROM config WHERE key = 'catalog_version'").fetchone()
    if cv_row is None:
        c.execute(
            "INSERT INTO config (key, value, notes) VALUES (?, ?, ?)",
            ("catalog_version", "1", "V11: dish catalog version for cache invalidation")
        )
        print("[OK] Initialized catalog_version = 1")
    else:
        print(f"[SKIP] catalog_version already exists: {cv_row['value']}")

    # 4. Verify meal_mode column exists on menus
    cols = c.execute("PRAGMA table_info(menus)").fetchall()
    col_names = [col["name"] for col in cols]
    if "meal_mode" not in col_names:
        c.execute("ALTER TABLE menus ADD COLUMN meal_mode TEXT DEFAULT 'daily'")
        print("[OK] Added meal_mode column to menus")
    else:
        print("[SKIP] meal_mode column already exists")

    if "banquet_total_diners" not in col_names:
        c.execute("ALTER TABLE menus ADD COLUMN banquet_total_diners INTEGER")
        print("[OK] Added banquet_total_diners column to menus")
    else:
        print("[SKIP] banquet_total_diners column already exists")

    # 5. Set default meal_mode for existing menus
    c.execute("UPDATE menus SET meal_mode = 'daily' WHERE meal_mode IS NULL OR meal_mode = ''")
    updated = c.rowcount
    if updated > 0:
        print(f"[OK] Set meal_mode='daily' for {updated} existing menus")

    conn.commit()
    conn.close()
    print("\n[OK] V11 migration complete")


if __name__ == "__main__":
    migrate()
