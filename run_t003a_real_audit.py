#!/usr/bin/env python3
"""Run the frozen-rule auditor against a seven-day real preview-data copy."""

import argparse
import json
import os
import sqlite3
import tempfile
from datetime import date, timedelta

import db
import inventory
import menu_service
from menu_rule_auditor import audit_menu_sequence


DEFAULT_REAL_DB = (
    "/Users/heymen/workbuddy/"
    "claw-family-ui-phase2-assets-20260819/test-family_menu.db"
)
DINERS_CYCLE = (2, 3, 4, 5, 2, 3, 4)
LOCATIONS = ("shenzhen", "hongkong")


def _quick_check(path):
    conn = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
    try:
        return conn.execute("PRAGMA quick_check").fetchone()[0]
    finally:
        conn.close()


def _backup_database(source_path, destination_path):
    source = sqlite3.connect(f"file:{source_path}?mode=ro", uri=True)
    destination = sqlite3.connect(destination_path)
    try:
        source.backup(destination)
        destination.execute("PRAGMA journal_mode=DELETE").fetchone()
    finally:
        destination.close()
        source.close()


def _menu_189_snapshot(path):
    conn = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row
    try:
        menu = conn.execute(
            "SELECT id,date,location,status,diners_count,updated_at "
            "FROM menus WHERE id=189"
        ).fetchone()
        items = conn.execute(
            "SELECT id,menu_id,dish_id,meal_type,source,is_locked,sort_order "
            "FROM menu_items WHERE menu_id=189 ORDER BY id"
        ).fetchall()
        return {
            "menu": dict(menu) if menu else None,
            "items": [dict(row) for row in items],
        }
    finally:
        conn.close()


def _push_event_count(path):
    conn = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
    try:
        return conn.execute(
            "SELECT COUNT(*) FROM events WHERE lower(event_type) LIKE '%push%'"
        ).fetchone()[0]
    finally:
        conn.close()


def _insert_draft(date_str, location, diners_count):
    conn = db.get_db()
    try:
        existing = conn.execute(
            "SELECT id,status FROM menus WHERE date=? AND location=?",
            (date_str, location),
        ).fetchone()
        if existing:
            if existing["status"] in {"confirmed", "pushed"}:
                raise RuntimeError(
                    f"refusing to touch confirmed menu {existing['id']}"
                )
            conn.execute(
                "UPDATE menus SET diners_count=? WHERE id=?",
                (diners_count, existing["id"]),
            )
        else:
            conn.execute(
                "INSERT INTO menus(date,location,status,diners_count) "
                "VALUES (?,?,'draft',?)",
                (date_str, location, diners_count),
            )
        conn.commit()
    finally:
        conn.close()


def _meal_summary(day_audit):
    checks = {item["id"]: item for item in day_audit["checks"]}
    matrix = checks["A03"]["details"]
    meat = checks["A04"]["details"]["non_tofu_meat_counts"]
    breakfast = checks["A01"]["details"]
    return {
        "breakfast": {
            "slots": breakfast["current"],
            "assignment_valid": breakfast["assignment_valid"],
            "dish_count": breakfast["dish_count_informational"],
        },
        "lunch": {
            **matrix["lunch"],
            "non_tofu_meat": meat["lunch"],
        },
        "dinner": {
            **matrix["dinner"],
            "non_tofu_meat": meat["dinner"],
        },
    }


def run_real_data_audit(source_db=DEFAULT_REAL_DB, start_date="2026-08-21"):
    """Copy the preview DB, generate draft menus, audit final stored outputs."""
    if not os.path.isfile(source_db):
        raise FileNotFoundError(source_db)
    source_before = _menu_189_snapshot(source_db)
    source_stat_before = os.stat(source_db)
    source_quick_check = _quick_check(source_db)
    start = date.fromisoformat(start_date)

    with tempfile.TemporaryDirectory(prefix="t003a-final-audit-") as tempdir:
        copied_db = os.path.join(tempdir, "test-family_menu.audit-copy.db")
        _backup_database(source_db, copied_db)
        copy_before = _menu_189_snapshot(copied_db)
        copy_quick_check_before = _quick_check(copied_db)
        push_events_before = _push_event_count(copied_db)

        original_db_path = db.DB_PATH
        records = []
        try:
            db.DB_PATH = copied_db
            inventory._availability_cache.clear()
            menu_service.invalidate_catalog_cache()

            for location_index, location in enumerate(LOCATIONS):
                for day_index, diners_count in enumerate(DINERS_CYCLE):
                    date_str = (start + timedelta(days=day_index)).isoformat()
                    _insert_draft(date_str, location, diners_count)
                    _, review = menu_service.generate_and_store_menu(
                        date_str,
                        location=location,
                        seed=3000 + location_index * 100 + day_index,
                    )
                    final_menu = menu_service.get_menu_with_dishes(
                        date_str, location
                    )
                    evidence = {
                        **review,
                        "rotation_context_location": location,
                        "inventory_context_location": location,
                    }
                    records.append({"menu": final_menu, "evidence": evidence})

            audit = audit_menu_sequence(records)
            copy_after = _menu_189_snapshot(copied_db)
            copy_quick_check_after = _quick_check(copied_db)
            push_events_after = _push_event_count(copied_db)
            generated_statuses = [record["menu"]["status"] for record in records]
        finally:
            inventory._availability_cache.clear()
            menu_service.invalidate_catalog_cache()
            db.DB_PATH = original_db_path

    source_after = _menu_189_snapshot(source_db)
    source_stat_after = os.stat(source_db)
    days = []
    for day_audit in audit["days"]:
        checks = {item["id"]: item for item in day_audit["checks"]}
        days.append({
            "date": day_audit["date"],
            "location": day_audit["location"],
            "diners_count": day_audit["diners_count"],
            "status": day_audit["status"],
            "rule_compliant": day_audit["rule_compliant"],
            "menu_complete": day_audit["menu_complete"],
            "meals": _meal_summary(day_audit),
            "degradation_warnings": day_audit["degradation_warnings"],
            "degradation_events": day_audit["degradation_events"],
            "rotation_repeats": checks["A07"]["details"]["repeats"],
            "hard_warnings": day_audit["hard_warnings"],
            "violation_ids": day_audit["violation_ids"],
            "hard_shortage_ids": day_audit["hard_shortage_ids"],
        })

    return {
        "source_db": source_db,
        "start_date": start_date,
        "days_per_location": len(DINERS_CYCLE),
        "diners_cycle": list(DINERS_CYCLE),
        "locations": list(LOCATIONS),
        "source_quick_check": source_quick_check,
        "copy_quick_check_before": copy_quick_check_before,
        "copy_quick_check_after": copy_quick_check_after,
        "source_unchanged": (
            source_before == source_after
            and source_stat_before.st_size == source_stat_after.st_size
            and source_stat_before.st_mtime_ns == source_stat_after.st_mtime_ns
        ),
        "menu_189_unchanged_in_copy": copy_before == copy_after,
        "generated_only_draft": all(
            status == "draft" for status in generated_statuses
        ),
        "push_attempts": push_events_after - push_events_before,
        "audit_status_counts": audit["status_counts"],
        "all_rule_compliant": audit["rule_compliant"],
        "all_complete": audit["passed"],
        "rules": audit["rules"],
        "days": days,
    }


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--source-db", default=DEFAULT_REAL_DB)
    parser.add_argument("--start-date", default="2026-08-21")
    args = parser.parse_args()
    report = run_real_data_audit(args.source_db, args.start_date)
    print(json.dumps(report, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
