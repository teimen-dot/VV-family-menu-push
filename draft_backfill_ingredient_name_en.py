#!/usr/bin/env python3
"""Draft only: list blank ingredient English names; apply only with explicit --apply."""

import argparse
import json
import os
import sqlite3


SUGGESTIONS = {
    "上海青": "Shanghai Bok Choy",
}


def blank_rows(conn):
    rows = conn.execute(
        "SELECT ingredient_id,name_cn,name_en FROM ingredients "
        "WHERE trim(COALESCE(name_en,''))='' ORDER BY ingredient_id"
    ).fetchall()
    return [
        {
            "ingredient_id": row[0],
            "name_cn": row[1],
            "current_name_en": row[2] or "",
            "proposed_name_en": SUGGESTIONS.get(row[1]),
            "status": "proposed" if row[1] in SUGGESTIONS else "manual_review",
        }
        for row in rows
    ]


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--db", required=True, help="Explicit non-production SQLite path")
    parser.add_argument("--list-output", help="Write the read-only audit list as JSON")
    parser.add_argument("--apply", action="store_true", help="Apply non-empty proposals")
    args = parser.parse_args()

    db_path = os.path.abspath(args.db)
    if not os.path.isfile(db_path):
        parser.error(f"database does not exist: {db_path}")
    conn = sqlite3.connect(db_path)
    try:
        rows = blank_rows(conn)
        payload = {
            "database": db_path,
            "blank_count": len(rows),
            "proposed_count": sum(item["status"] == "proposed" for item in rows),
            "applied": bool(args.apply),
            "items": rows,
        }
        if args.list_output:
            with open(args.list_output, "w", encoding="utf-8") as handle:
                json.dump(payload, handle, ensure_ascii=False, indent=2)
                handle.write("\n")
        else:
            print(json.dumps(payload, ensure_ascii=False, indent=2))

        if args.apply:
            with conn:
                for item in rows:
                    proposed = item["proposed_name_en"]
                    if proposed:
                        conn.execute(
                            "UPDATE ingredients SET name_en=? "
                            "WHERE ingredient_id=? AND trim(COALESCE(name_en,''))=''",
                            (proposed, item["ingredient_id"]),
                        )
    finally:
        conn.close()


if __name__ == "__main__":
    main()
