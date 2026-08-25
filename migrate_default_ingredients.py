#!/usr/bin/env python3
"""Explicit, idempotent migration for Excel-managed household defaults."""

import argparse
import json

from db import get_db, log_event
from ingredient_resolution import (
    backfill_aliases, ensure_resolution_schema, merge_ingredient, normalize_key,
    register_alias,
)


DEFAULT_INGREDIENTS = (
    ("rice", "大米", "Rice", ("米", "米饭", "白米")),
    ("flour", "面粉", "Flour", ("小麦粉",)),
    ("water", "水", "Water", ()),
    ("cooking_oil", "食用油", "Cooking Oil", ("油",)),
    ("salt", "盐", "Salt", ()),
    ("sugar", "糖", "Sugar", ()),
    ("light_soy_sauce", "生抽", "Light Soy Sauce", ()),
    ("dark_soy_sauce", "老抽", "Dark Soy Sauce", ()),
    ("oyster_sauce", "蚝油", "Oyster Sauce", ()),
    ("vinegar", "醋", "Vinegar", ()),
    ("cooking_wine", "料酒", "Cooking Wine", ()),
    ("scallion", "葱", "Scallion", ("葱花",)),
    ("ginger", "姜", "Ginger", ()),
    ("garlic", "蒜", "Garlic", ("蒜蓉",)),
    ("starch", "淀粉", "Starch", ()),
    ("pepper", "胡椒", "Pepper", ()),
    ("chicken_bouillon", "鸡精", "Chicken Bouillon", ()),
    ("millet", "小米", "Millet", ()),
)


def apply_default_ingredients(conn, dry_run=False):
    ensure_resolution_schema(conn)
    conn.execute("BEGIN IMMEDIATE")
    report = {"created": [], "updated": [], "merged": {}, "defaults": []}
    try:
        backfill_aliases(conn)
        for ingredient_id, name_cn, name_en, aliases in DEFAULT_INGREDIENTS:
            if not conn.execute(
                    "SELECT 1 FROM ingredients WHERE ingredient_id=?", (ingredient_id,)).fetchone():
                conn.execute(
                    "INSERT INTO ingredients(ingredient_id,name_cn,name_en,aliases,category,"
                    "ingredient_group,is_common) VALUES(?,?,?,'[]','','other',0)",
                    (ingredient_id, name_cn, name_en),
                )
                report["created"].append(ingredient_id)

        for ingredient_id, name_cn, name_en, aliases in DEFAULT_INGREDIENTS:
            desired = (ingredient_id, name_cn, name_en, *aliases)
            source_ids = set()
            for text in desired:
                owner = conn.execute(
                    "SELECT ingredient_id FROM ingredient_aliases WHERE alias_key=?",
                    (normalize_key(text),),
                ).fetchone()
                if owner and owner["ingredient_id"] != ingredient_id:
                    source_ids.add(owner["ingredient_id"])
                source_ids.update(row["ingredient_id"] for row in conn.execute(
                    "SELECT ingredient_id FROM ingredients WHERE ingredient_id=? OR name_cn=? OR name_en=?",
                    (text, text, text),
                ).fetchall() if row["ingredient_id"] != ingredient_id)
            for source_id in sorted(source_ids):
                preserved_aliases = [row["alias_text"] for row in conn.execute(
                    "SELECT alias_text FROM ingredient_aliases WHERE ingredient_id=?", (source_id,)
                ).fetchall()]
                report["merged"][source_id] = {
                    "target": ingredient_id,
                    "counts": merge_ingredient(conn, source_id, ingredient_id),
                }
                for alias in preserved_aliases:
                    register_alias(conn, ingredient_id, alias, "merged_default")

            conn.execute(
                "UPDATE ingredients SET name_cn=?,name_en=? WHERE ingredient_id=?",
                (name_cn, name_en, ingredient_id),
            )
            for kind, text in (("id", ingredient_id), ("name_cn", name_cn),
                               ("name_en", name_en)):
                register_alias(conn, ingredient_id, text, kind)
            for alias in aliases:
                register_alias(conn, ingredient_id, alias, "default")
            conn.execute(
                "INSERT INTO ingredient_dictionary_metadata(ingredient_id,status,updated_at) "
                "VALUES(?,'default',datetime('now')) ON CONFLICT(ingredient_id) DO UPDATE SET "
                "status='default',updated_at=excluded.updated_at", (ingredient_id,),
            )
            report["updated"].append(ingredient_id)
            report["defaults"].append({"ingredient_id": ingredient_id, "names": list(desired)})

        for location in ("shenzhen", "hongkong"):
            conn.execute(
                "INSERT INTO config(key,value) VALUES(?, '1') ON CONFLICT(key) DO UPDATE SET "
                "value=CAST(CAST(value AS INTEGER)+1 AS TEXT)",
                (f"inventory_version_{location}",),
            )

        if dry_run:
            conn.rollback()
        else:
            conn.commit()
    except Exception:
        conn.rollback()
        raise
    return report


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()
    conn = get_db()
    try:
        report = apply_default_ingredients(conn, dry_run=args.dry_run)
    finally:
        conn.close()
    if not args.dry_run:
        log_event("default_ingredients_migrated", "ingredient", "dictionary", report)
    print(json.dumps(report, ensure_ascii=False, sort_keys=True))


if __name__ == "__main__":
    main()
