#!/usr/bin/env python3
"""Explicit, idempotent repair for the production `blue berry` pending row."""

import argparse
import json

from db import get_db
from ingredient_dictionary import _bump_inventory_versions, _invalidate_runtime_caches
from ingredient_resolution import ensure_resolution_schema, merge_ingredient, register_alias


SOURCE_ID = "pending_dc553147848b6d2ef22a"
TARGET_ID = "blueberry"
RAW_ALIAS = "blue berry"
EVENT_TYPE = "ingredient_pending_merged"


def repair(conn, apply=False):
    ensure_resolution_schema(conn)
    target = conn.execute(
        "SELECT ingredient_id,name_cn,name_en FROM ingredients WHERE ingredient_id=?", (TARGET_ID,)
    ).fetchone()
    if not target:
        raise ValueError("target ingredient blueberry not found")
    pending = conn.execute(
        "SELECT pending_id,status,resolved_ingredient_id,raw_input FROM pending_ingredients WHERE pending_id=?",
        (SOURCE_ID,),
    ).fetchone()
    before = {}
    for table in ("dish_ingredients", "current_pantry", "inventory_items", "purchase_requests",
                  "ingredient_classifications", "pantry_usage_stats", "consumed_history"):
        exists = conn.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (table,)).fetchone()
        before[table] = conn.execute(
            f"SELECT COUNT(*) FROM {table} WHERE ingredient_id=?", (SOURCE_ID,)
        ).fetchone()[0] if exists else 0
    report = {"source_id": SOURCE_ID, "target_id": TARGET_ID, "pending": dict(pending) if pending else None,
              "references_before": before, "dry_run": not apply}
    if not apply:
        return report

    conn.execute("BEGIN IMMEDIATE")
    try:
        register_alias(conn, TARGET_ID, RAW_ALIAS, "merged_pending")
        if pending and pending["status"] == "pending":
            counts = merge_ingredient(conn, SOURCE_ID, TARGET_ID)
            report["migrated"] = counts
            details = json.dumps({"source_id": SOURCE_ID, "target_id": TARGET_ID,
                                  "raw_input": pending["raw_input"], "counts": counts}, ensure_ascii=False)
            already_logged = conn.execute(
                "SELECT 1 FROM events WHERE event_type=? AND entity_id=? AND details LIKE ? LIMIT 1",
                (EVENT_TYPE, TARGET_ID, f'%"source_id": "{SOURCE_ID}"%'),
            ).fetchone()
            if not already_logged:
                conn.execute("INSERT INTO events(event_type,entity_type,entity_id,details) VALUES(?,?,?,?)",
                             (EVENT_TYPE, "ingredient", TARGET_ID, details))
            _bump_inventory_versions(conn)
        elif pending and pending["resolved_ingredient_id"] not in (None, TARGET_ID):
            raise ValueError(f"pending already resolved to {pending['resolved_ingredient_id']}")
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    _invalidate_runtime_caches()
    report["dry_run"] = False
    report["status"] = "repaired" if pending and pending["status"] == "pending" else "already_repaired"
    return report


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--apply", action="store_true", help="commit the repair; default is dry-run")
    args = parser.parse_args()
    conn = get_db()
    try:
        print(json.dumps(repair(conn, apply=args.apply), ensure_ascii=False, indent=2))
    finally:
        conn.close()


if __name__ == "__main__":
    main()
