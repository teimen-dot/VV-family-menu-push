#!/usr/bin/env python3
"""Explicit, idempotent ingredient identity migration. Never run at startup."""

import argparse
import json
from db import get_db, log_event
from ingredient_resolution import backfill_aliases, ensure_resolution_schema, merge_ingredient


def migrate(dry_run=False):
    conn = get_db()
    try:
        conn.execute("BEGIN IMMEDIATE")
        ensure_resolution_schema(conn)
        source = conn.execute("SELECT 1 FROM ingredients WHERE ingredient_id='salad'").fetchone()
        target = conn.execute("SELECT 1 FROM ingredients WHERE ingredient_id='沙拉菜'").fetchone()
        counts = merge_ingredient(conn, "salad", "沙拉菜") if source and target else {"already_merged": True}
        backfill_aliases(conn)
        if dry_run:
            conn.rollback()
        else:
            conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()
    if not dry_run:
        log_event("ingredient_merged", "ingredient", "沙拉菜",
                  {"source_id": "salad", "target_id": "沙拉菜", "counts": counts})
    return counts


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()
    print(json.dumps(migrate(dry_run=args.dry_run), ensure_ascii=False, sort_keys=True))
