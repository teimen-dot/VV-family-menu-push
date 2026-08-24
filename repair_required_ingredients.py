#!/usr/bin/env python3
"""Apply only the confirmed required-ingredient repairs from TASK_CURRENT.md."""

import argparse
import json
import os
import sqlite3


NEW_INGREDIENTS = {
    "饭团": ("饭团", "Rice Ball", ["小饭团"], "grain", "staple_coarse"),
    "哈密瓜": ("哈密瓜", "Hami Melon", [], "fruit", "fruit"),
    "青提": ("青提", "Green Grapes", [], "fruit", "fruit"),
    "草莓": ("草莓", "Strawberry", [], "fruit", "fruit"),
}

REQUIRED_ADDITIONS = {
    "dish_0064": ("芦笋百合虾", ["shrimp"]),
    "dish_0071": ("丝瓜豆腐鸡蛋汤", ["鸡蛋"]),
    "dish_0080": ("冬瓜松茸肉丸汤", ["冬瓜"]),
    "dish_0099": ("小饭团", ["饭团"]),
    "dish_0117": ("白菜豆腐汤", ["white_cabbage"]),
    "dish_0119": ("火腿松茸味噌汤", ["matsutake"]),
    "dish_0122": ("松茸粥", ["matsutake"]),
    "dish_0124": ("南瓜山药小米粥", ["南瓜"]),
    "dish_0125": ("银鳕鱼淮山红萝卜松茸粥", ["matsutake"]),
    "dish_0134": ("松茸蒸蛋", ["matsutake"]),
    "dish_0154": ("松茸竹荪菌菇鸡汤", ["matsutake"]),
    "dish_0192": ("哈密瓜", ["哈密瓜"]),
    "dish_0193": ("青提", ["青提"]),
    "dish_0194": ("草莓", ["草莓"]),
}

REQUIRED_REMOVALS = {
    "dish_0080": ("冬瓜松茸肉丸汤", ["西葫芦"]),
    "dish_0099": ("小饭团", ["rice"]),
    "dish_0167": ("茴香蘑菇沙拉", ["mushroom_generic"]),
    "dish_0174": ("山药疙瘩汤", ["淮山"]),
}


def _validate_dish(conn, dish_id, expected_name):
    row = conn.execute("SELECT name_cn FROM dishes WHERE id=?", (dish_id,)).fetchone()
    if not row or row[0] != expected_name:
        raise RuntimeError(f"unexpected dish identity: {dish_id} / {expected_name}")


def apply_repairs(db_path):
    conn = sqlite3.connect(db_path)
    conn.execute("PRAGMA foreign_keys=ON")
    try:
        with conn:
            for ingredient_id, (name_cn, name_en, aliases, category, group_name) in NEW_INGREDIENTS.items():
                conn.execute(
                    "INSERT INTO ingredients "
                    "(ingredient_id,name_cn,name_en,aliases,category,ingredient_group,is_common) "
                    "VALUES (?,?,?,?,?,?,0) ON CONFLICT(ingredient_id) DO NOTHING",
                    (ingredient_id, name_cn, name_en, json.dumps(aliases, ensure_ascii=False), category, group_name),
                )

            for dish_id, (expected_name, ingredient_ids) in REQUIRED_ADDITIONS.items():
                _validate_dish(conn, dish_id, expected_name)
                for ingredient_id in ingredient_ids:
                    conn.execute(
                        "INSERT INTO dish_ingredients (dish_id,ingredient_id,required) VALUES (?,?,1) "
                        "ON CONFLICT(dish_id,ingredient_id) DO UPDATE SET required=1",
                        (dish_id, ingredient_id),
                    )

            # Canonical mushroom association is deliberately limited to the confirmed dish.
            _validate_dish(conn, "dish_0167", "茴香蘑菇沙拉")
            conn.execute(
                "INSERT INTO dish_ingredients (dish_id,ingredient_id,required) VALUES ('dish_0167','mushroom',1) "
                "ON CONFLICT(dish_id,ingredient_id) DO UPDATE SET required=1"
            )

            for dish_id, (expected_name, ingredient_ids) in REQUIRED_REMOVALS.items():
                _validate_dish(conn, dish_id, expected_name)
                for ingredient_id in ingredient_ids:
                    conn.execute(
                        "DELETE FROM dish_ingredients WHERE dish_id=? AND ingredient_id=?",
                        (dish_id, ingredient_id),
                    )
    finally:
        conn.close()


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--db", required=True, help="Explicit SQLite database path")
    parser.add_argument("--apply", action="store_true", help="Apply repairs; default is dry-run")
    args = parser.parse_args()
    db_path = os.path.abspath(args.db)
    if not os.path.isfile(db_path):
        parser.error(f"database does not exist: {db_path}")
    if not args.apply:
        dish_count = len(set(REQUIRED_ADDITIONS) | set(REQUIRED_REMOVALS))
        print(f"DRY RUN: {dish_count} dishes; database unchanged: {db_path}")
        return
    apply_repairs(db_path)
    print(f"Applied confirmed required-ingredient repairs: {db_path}")


if __name__ == "__main__":
    main()
