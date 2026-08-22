#!/usr/bin/env python3
"""One-time SQLite migration: menus UNIQUE(date) -> UNIQUE(date, location).

This tool is intentionally not imported by application startup. Run --check first,
take an SQLite backup, stop application writers, then run --apply explicitly.
"""

import argparse
import hashlib
import json
import os
import sqlite3
import sys


MENU_COLUMNS = (
    "id", "date", "location", "status", "auto_confirmed", "confirmed_at",
    "pushed_at", "diners_count", "diners", "notes_zh", "notes_en",
    "created_at", "updated_at", "inventory_snapshot_id", "meal_mode",
    "banquet_total_diners", "push_status", "push_error",
    "confirmed_revision", "meal_notes",
)

SHENZHEN_TABLES = {
    "menus": "SELECT * FROM menus WHERE location='shenzhen' ORDER BY id",
    "menu_items": (
        "SELECT mi.* FROM menu_items mi JOIN menus m ON m.id=mi.menu_id "
        "WHERE m.location='shenzhen' ORDER BY mi.id"
    ),
    "menu_meal_settings": (
        "SELECT s.* FROM menu_meal_settings s JOIN menus m ON m.id=s.menu_id "
        "WHERE m.location='shenzhen' ORDER BY s.menu_id,s.meal_type"
    ),
    "selections": (
        "SELECT s.* FROM selections s JOIN menus m ON m.id=s.menu_id "
        "WHERE m.location='shenzhen' ORDER BY s.id"
    ),
    "push_logs": (
        "SELECT p.* FROM push_logs p JOIN menus m ON m.id=p.menu_id "
        "WHERE m.location='shenzhen' ORDER BY p.id"
    ),
    "current_pantry": (
        "SELECT * FROM current_pantry WHERE location='shenzhen' ORDER BY id"
    ),
    "dishes": "SELECT * FROM dishes ORDER BY id",
    "ingredients": "SELECT * FROM ingredients ORDER BY ingredient_id",
    "dish_ingredients": "SELECT * FROM dish_ingredients ORDER BY id",
}


def connect(path):
    conn = sqlite3.connect(path)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA busy_timeout=5000")
    return conn


def unique_column_sets(conn):
    result = []
    for index in conn.execute("PRAGMA index_list(menus)").fetchall():
        if not index["unique"]:
            continue
        result.append(tuple(
            row["name"] for row in conn.execute(
                f"PRAGMA index_info('{index['name']}')"
            ).fetchall()
        ))
    return result


def schema_state(conn):
    constraints = unique_column_sets(conn)
    if ("date", "location") in constraints:
        return "composite"
    if ("date",) in constraints:
        return "legacy"
    return "unsupported"


def canonical_rows(conn, query):
    return [dict(row) for row in conn.execute(query).fetchall()]


def fingerprint(conn):
    hashes = {}
    for name, query in SHENZHEN_TABLES.items():
        rows = canonical_rows(conn, query)
        payload = json.dumps(
            rows, ensure_ascii=False, sort_keys=True, separators=(",", ":")
        ).encode("utf-8")
        hashes[name] = {
            "rows": len(rows),
            "sha256": hashlib.sha256(payload).hexdigest(),
        }
    return hashes


def verify_database(conn):
    quick = conn.execute("PRAGMA quick_check").fetchone()[0]
    foreign_keys = [tuple(row) for row in conn.execute("PRAGMA foreign_key_check")]
    if quick != "ok" or foreign_keys:
        raise RuntimeError(
            f"database integrity failed: quick_check={quick}, foreign_keys={foreign_keys}"
        )


def migrate(path):
    conn = connect(path)
    try:
        state = schema_state(conn)
        before = fingerprint(conn)
        verify_database(conn)
        if state == "composite":
            return {"changed": False, "state": state, "fingerprint": before}
        if state != "legacy":
            raise RuntimeError("unsupported menus uniqueness schema")

        actual_columns = tuple(
            row["name"] for row in conn.execute("PRAGMA table_info(menus)")
        )
        if actual_columns != MENU_COLUMNS:
            raise RuntimeError(
                f"unexpected menus columns; refusing migration: {actual_columns}"
            )
        user_indexes = conn.execute(
            "SELECT name FROM sqlite_master WHERE type='index' AND tbl_name='menus' "
            "AND sql IS NOT NULL"
        ).fetchall()
        triggers = conn.execute(
            "SELECT name FROM sqlite_master WHERE type='trigger' AND tbl_name='menus'"
        ).fetchall()
        if user_indexes or triggers:
            raise RuntimeError("unexpected menus indexes/triggers; refusing migration")

        conn.commit()
        conn.execute("PRAGMA foreign_keys=OFF")
        conn.execute("PRAGMA legacy_alter_table=ON")
        conn.execute("BEGIN IMMEDIATE")
        conn.execute("""
            CREATE TABLE menus_location_v2 (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                date TEXT NOT NULL,
                location TEXT NOT NULL,
                status TEXT DEFAULT 'draft',
                auto_confirmed INTEGER DEFAULT 0,
                confirmed_at TEXT,
                pushed_at TEXT,
                diners_count INTEGER DEFAULT 4,
                diners TEXT DEFAULT '[]',
                notes_zh TEXT,
                notes_en TEXT,
                created_at TEXT DEFAULT (datetime('now')),
                updated_at TEXT DEFAULT (datetime('now')),
                inventory_snapshot_id INTEGER,
                meal_mode TEXT DEFAULT 'daily',
                banquet_total_diners INTEGER,
                push_status TEXT DEFAULT 'not_sent',
                push_error TEXT,
                confirmed_revision TEXT,
                meal_notes TEXT DEFAULT '{}',
                UNIQUE(date, location)
            )
        """)
        columns = ",".join(MENU_COLUMNS)
        conn.execute(
            f"INSERT INTO menus_location_v2 ({columns}) SELECT {columns} FROM menus"
        )
        conn.execute("DROP TABLE menus")
        conn.execute("ALTER TABLE menus_location_v2 RENAME TO menus")
        conn.execute(
            "CREATE INDEX idx_menus_location_date ON menus(location, date)"
        )
        conn.commit()
        conn.execute("PRAGMA foreign_keys=ON")
        conn.execute("PRAGMA legacy_alter_table=OFF")

        if schema_state(conn) != "composite":
            raise RuntimeError("composite uniqueness was not created")
        verify_database(conn)
        after = fingerprint(conn)
        if after != before:
            raise RuntimeError(
                "protected Shenzhen/shared-data fingerprint changed; restore backup"
            )
        return {
            "changed": True,
            "state": "composite",
            "fingerprint": after,
        }
    except Exception:
        if conn.in_transaction:
            conn.rollback()
        raise
    finally:
        conn.close()


def check(path):
    conn = connect(path)
    try:
        verify_database(conn)
        return {
            "state": schema_state(conn),
            "fingerprint": fingerprint(conn),
        }
    finally:
        conn.close()


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("database", help="SQLite database path")
    group = parser.add_mutually_exclusive_group(required=True)
    group.add_argument("--check", action="store_true")
    group.add_argument("--apply", action="store_true")
    args = parser.parse_args()
    if not os.path.isfile(args.database):
        raise SystemExit(f"database does not exist: {args.database}")
    result = check(args.database) if args.check else migrate(args.database)
    json.dump(result, sys.stdout, ensure_ascii=False, indent=2, sort_keys=True)
    print()


if __name__ == "__main__":
    main()
