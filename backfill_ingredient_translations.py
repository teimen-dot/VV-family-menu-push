#!/usr/bin/env python3
"""Create a database copy and backfill missing ingredient translations in that copy."""

import argparse
import os
import sqlite3

from ingredient_service import complete_bilingual_names


def backfill_copy(source_path, output_path):
    source_path = os.path.abspath(source_path)
    output_path = os.path.abspath(output_path)
    if source_path == output_path:
        raise ValueError("output must be a separate database copy")
    if os.path.exists(output_path):
        raise ValueError("output already exists")

    source = sqlite3.connect(source_path)
    target = sqlite3.connect(output_path)
    target.row_factory = sqlite3.Row
    try:
        source.backup(target)
        rows = target.execute(
            "SELECT ingredient_id,name_cn,name_en,translation_pending FROM ingredients"
        ).fetchall()
        translated = pending = 0
        for row in rows:
            old_cn, old_en = row["name_cn"] or "", row["name_en"] or ""
            if old_cn and old_en and old_cn.casefold() != old_en.casefold() and not row["translation_pending"]:
                continue
            name_cn, name_en, is_pending = complete_bilingual_names(old_cn, old_en)
            target.execute(
                "UPDATE ingredients SET name_cn=?,name_en=?,translation_pending=?,"
                "updated_at=datetime('now') WHERE ingredient_id=?",
                (name_cn, name_en, is_pending, row["ingredient_id"]),
            )
            if is_pending:
                pending += 1
            else:
                translated += 1
        target.commit()
        if target.execute("PRAGMA quick_check").fetchone()[0] != "ok":
            raise RuntimeError("database copy quick_check failed")
        return {"translated": translated, "pending": pending, "output": output_path}
    finally:
        target.close()
        source.close()


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--source", required=True)
    parser.add_argument("--output", required=True)
    args = parser.parse_args()
    print(backfill_copy(args.source, args.output))


if __name__ == "__main__":
    main()
