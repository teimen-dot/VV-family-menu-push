#!/usr/bin/env python3
"""Apply the frozen 2026-08-17 adjudication to one explicitly selected SQLite DB.

Dry-run is the default.  Production is never selected implicitly.
"""

import argparse
import json
import os
import re
import sqlite3


PROTECTED_PENDING_IDS = (
    "dish_0097", "dish_0122", "dish_0183",
    "dish_0188", "dish_0189", "dish_0202",
)

PROTEIN_ROLE_BACKFILL = (
    "蛋炒虾仁", "排骨焖土豆", "蒸排骨", "鲜松茸清炖去皮鸡肉", "红烧鸡肉",
    "葱姜生抽鸡丁", "葱香鸡肉", "葱烧鸡", "柠檬煎鸡腿肉", "姜葱炒鸡",
    "香煎银鳕鱼", "清炒虾仁", "辣椒炒肉", "蒜苔腊肉", "黄瓜虾饼",
    "虾饼", "煎鸡翅", "红烧肉", "榨菜肉丝",
)

VEGETABLE_ROLE_BACKFILL = (
    "蒜蓉红苋菜", "蒜蓉菜心", "清炒菜心", "蒜蓉青菜",
    "蒜蓉上海青", "清炒生菜", "清炒上海青", "清炒红苋菜",
)

QUICK_SOUPS = ("番茄菌菇小白菜汤", "番茄菌菇豆腐汤", "肉饼汤")
SLOW_SOUPS = ("莲藕松茸芹菜粒鸡汤", "白萝卜炖鸡汤")

PLACEHOLDERS = {
    "any_available_vegetable": ("任意可用蔬菜", "Any Available Vegetable", "vegetable"),
    "any_available_fish": ("任意可用鱼", "Any Available Fish", "fish"),
    "any_available_grouper": ("任意可用石斑鱼", "Any Available Grouper", "grouper"),
    "any_available_mushroom": ("任意可用菌菇", "Any Available Mushroom", "mushroom"),
    "any_available_protein": ("任意可用蛋白质", "Any Available Protein", "protein"),
}


def _columns(conn, table):
    return [row["name"] for row in conn.execute(f"PRAGMA table_info({table})")]


def _ensure_column(conn, table, name, declaration):
    if name not in _columns(conn, table):
        conn.execute(f"ALTER TABLE {table} ADD COLUMN {name} {declaration}")


def _ensure_menu_location_key(conn):
    unique_sets = []
    for index in conn.execute("PRAGMA index_list(menus)"):
        if index["unique"]:
            unique_sets.append(tuple(
                row["name"] for row in conn.execute(
                    f"PRAGMA index_info({json.dumps(index['name'])})"
                )
            ))
    if ("date", "location") in unique_sets and ("date",) not in unique_sets:
        return

    row = conn.execute(
        "SELECT sql FROM sqlite_master WHERE type='table' AND name='menus'"
    ).fetchone()
    if not row or not row["sql"]:
        raise RuntimeError("menus schema not found")
    original_sql = row["sql"]
    new_sql = re.sub(
        r"CREATE\s+TABLE\s+(?:IF\s+NOT\s+EXISTS\s+)?[\"']?menus[\"']?",
        "CREATE TABLE menus_adjudication_new",
        original_sql,
        count=1,
        flags=re.IGNORECASE,
    )
    new_sql = re.sub(
        r"(\bdate\s+TEXT\s+NOT\s+NULL)\s+UNIQUE\b",
        r"\1",
        new_sql,
        count=1,
        flags=re.IGNORECASE,
    )
    close = new_sql.rfind(")")
    if close < 0:
        raise RuntimeError("unrecognized menus schema")
    new_sql = new_sql[:close].rstrip() + ", UNIQUE(date, location)" + new_sql[close:]
    names = _columns(conn, "menus")
    quoted = ", ".join(f'"{name}"' for name in names)
    conn.execute(new_sql)
    conn.execute(
        f"INSERT INTO menus_adjudication_new ({quoted}) SELECT {quoted} FROM menus"
    )
    conn.execute("DROP TABLE menus")
    conn.execute("ALTER TABLE menus_adjudication_new RENAME TO menus")


def ensure_schema(conn):
    _ensure_column(conn, "dishes", "drink", "TEXT")
    _ensure_column(conn, "dishes", "ingredients_pending", "INTEGER DEFAULT 0")
    _ensure_column(conn, "dishes", "pending_review", "TEXT")
    conn.execute("""
        CREATE TABLE IF NOT EXISTS ingredient_classifications (
            ingredient_id TEXT NOT NULL,
            class_id TEXT NOT NULL,
            created_at TEXT DEFAULT (datetime('now')),
            PRIMARY KEY (ingredient_id, class_id),
            FOREIGN KEY (ingredient_id) REFERENCES ingredients(ingredient_id)
        )
    """)
    _ensure_menu_location_key(conn)


def _snapshot_pending(conn):
    result = {}
    for dish_id in PROTECTED_PENDING_IDS:
        dish = conn.execute("SELECT * FROM dishes WHERE id=?", (dish_id,)).fetchone()
        required = conn.execute(
            "SELECT ingredient_id, required FROM dish_ingredients "
            "WHERE dish_id=? ORDER BY ingredient_id", (dish_id,),
        ).fetchall()
        result[dish_id] = (
            tuple(dish) if dish else None,
            tuple((row["ingredient_id"], row["required"]) for row in required),
        )
    return result


def _json_list(value):
    try:
        parsed = json.loads(value or "[]")
        return parsed if isinstance(parsed, list) else []
    except (TypeError, json.JSONDecodeError):
        return []


def _merge_json_field(conn, name, field, add=(), remove=()):
    rows = conn.execute(
        f"SELECT id, {field} FROM dishes WHERE name_cn=?", (name,)
    ).fetchall()
    for row in rows:
        values = [value for value in _json_list(row[field]) if value not in set(remove)]
        for value in add:
            if value not in values:
                values.append(value)
        conn.execute(
            f"UPDATE dishes SET {field}=?, updated_at=datetime('now') WHERE id=?",
            (json.dumps(values, ensure_ascii=False), row["id"]),
        )


def _dish_ids(conn, name):
    return [row["id"] for row in conn.execute("SELECT id FROM dishes WHERE name_cn=?", (name,))]


def _ensure_ingredient(conn, ingredient_id, name_cn, name_en="", category=""):
    conn.execute(
        "INSERT INTO ingredients (ingredient_id,name_cn,name_en,aliases,category) "
        "VALUES (?,?,?,'[]',?) ON CONFLICT(ingredient_id) DO NOTHING",
        (ingredient_id, name_cn, name_en, category),
    )


def _set_required(conn, dish_name, required_ids):
    for dish_id in _dish_ids(conn, dish_name):
        conn.execute("DELETE FROM dish_ingredients WHERE dish_id=?", (dish_id,))
        for ingredient_id in required_ids:
            conn.execute(
                "INSERT INTO dish_ingredients (dish_id,ingredient_id,required) "
                "VALUES (?,?,1)", (dish_id, ingredient_id),
            )


def _replace_required(conn, dish_name, remove_ids, add_ids):
    for dish_id in _dish_ids(conn, dish_name):
        if remove_ids:
            marks = ",".join("?" for _ in remove_ids)
            conn.execute(
                f"DELETE FROM dish_ingredients WHERE dish_id=? AND ingredient_id IN ({marks})",
                (dish_id, *remove_ids),
            )
        for ingredient_id in add_ids:
            conn.execute(
                "INSERT INTO dish_ingredients (dish_id,ingredient_id,required) VALUES (?,?,1) "
                "ON CONFLICT(dish_id,ingredient_id) DO UPDATE SET required=1",
                (dish_id, ingredient_id),
            )


def _classify_existing_ingredients(conn):
    rows = conn.execute(
        "SELECT ingredient_id,name_cn,category,ingredient_group FROM ingredients"
    ).fetchall()
    mushroom_tokens = ("mushroom", "matsutake", "enoki", "菌", "菇", "松茸", "舞茸")
    grouper_tokens = ("grouper", "石斑", "红斑")
    fish_ids = {"fish", "cod", "mackerel"}
    explicit_protein = {
        "beef", "chicken", "wagyu", "beef_steak", "pork", "排骨", "chicken_leg",
        "cod", "fish", "mackerel", "shrimp", "black_tiger_shrimp", "shrimp_paste",
        "肉末", "火腿", "肉丸", "蛤蜊", "带子", "鸡蛋", "egg", "tofu",
        "silken_tofu", "tofu_skin", "皮蛋", "黑鱼子酱", "三文鱼籽", "grouper",
        "red_grouper",
    }
    noodle_tokens = ("面条", "荞麦面", "意面", "米粉", "河粉", "noodle", "pasta")
    excluded_vegetables = {"corn", "yam", "淮山", "南瓜", "土豆"}
    for row in rows:
        ingredient_id = row["ingredient_id"]
        text = f"{ingredient_id} {row['name_cn']}".lower()
        classes = set()
        if ingredient_id in fish_ids or any(token in text for token in grouper_tokens):
            classes.add("fish")
        if any(token in text for token in grouper_tokens):
            classes.add("grouper")
        if any(token in text for token in mushroom_tokens):
            classes.add("mushroom")
        if row["category"] == "protein" or ingredient_id in explicit_protein:
            classes.add("protein")
        if (row["category"] == "vegetable" or row["category"] == "vegetable_mushroom") \
                and ingredient_id not in excluded_vegetables and "mushroom" not in classes:
            classes.add("vegetable")
        if any(token in text for token in noodle_tokens):
            classes.add("noodle")
        for class_id in classes:
            conn.execute(
                "INSERT OR IGNORE INTO ingredient_classifications (ingredient_id,class_id) "
                "VALUES (?,?)", (ingredient_id, class_id),
            )


def _assert_strict_source_records(conn):
    exact_names = {
        "汤饺", "水饺", "牛油果洋葱酱三文鱼籽", "酱油凉拌豆腐牛油果",
        "鸡丝青瓜丝嫩豆腐", "豆腐蒸蛋", "鱼籽寿司卷", "水煮鱼",
        "清蒸红斑鱼", "姜葱鱼焖饭", "番茄菌菇小白菜汤", "番茄菌菇豆腐汤",
        "杂蔬虾仁藜麦炒饭", "酸辣娃娃菜", "沙拉菜", "日式溏心蛋",
        "金银蛋炒虾仁", "酸种面包", "牛肉焖饭", "黑鱼子酱配豆腐",
        "四味豆腐沙拉", "火腿松茸豆腐汤",
        *QUICK_SOUPS, *SLOW_SOUPS, *VEGETABLE_ROLE_BACKFILL, *PROTEIN_ROLE_BACKFILL,
    }
    found = {
        row["name_cn"] for row in conn.execute(
            "SELECT name_cn FROM dishes WHERE name_cn IN ({})".format(
                ",".join("?" for _ in exact_names)
            ),
            tuple(sorted(exact_names)),
        )
    }
    missing = sorted(exact_names - found)
    if missing:
        raise AssertionError(f"strict source is missing adjudicated dishes: {missing}")
    if not conn.execute(
        "SELECT 1 FROM dishes WHERE name_cn LIKE '牛油果%早餐盘%' LIMIT 1"
    ).fetchone():
        raise AssertionError("strict source is missing 牛油果早餐盘")
    if not conn.execute(
        "SELECT 1 FROM dishes WHERE name_cn IN ('番茄蘑菇蛋汤','番茄（西红柿）蘑菇蛋汤') LIMIT 1"
    ).fetchone():
        raise AssertionError("strict source is missing 番茄蘑菇蛋汤")
    if not conn.execute(
        "SELECT 1 FROM dishes WHERE name_cn IN ('凉拌虾仁荞麦面','营养拌面（蛋白质+蔬菜）') LIMIT 1"
    ).fetchone():
        raise AssertionError("strict source is missing 营养拌面")
    if not conn.execute(
        "SELECT 1 FROM dishes WHERE name_cn IN "
        "('日式拌豆腐（木鱼花、柴鱼片）','日式拌豆腐（木鱼花）','日式拌豆腐（柴鱼片）') LIMIT 1"
    ).fetchone():
        raise AssertionError("strict source is missing 日式拌豆腐")


def _update_dishes(conn):
    # Five real placeholders and the controlled category map.
    for ingredient_id, (cn, en, _) in PLACEHOLDERS.items():
        _ensure_ingredient(conn, ingredient_id, cn, en, "placeholder")
    _ensure_ingredient(conn, "baby_cabbage", "娃娃菜", "Baby Chinese Cabbage", "vegetable")
    _ensure_ingredient(conn, "三文鱼籽", "三文鱼籽", "Salmon Roe", "protein")
    _classify_existing_ingredients(conn)

    # Onepot and source-dependent meal coverage.
    conn.execute(
        "UPDATE dishes SET category_id='staple_carb', meal_tags='[\"breakfast\", \"lunch\"]', "
        "carb_type='dim_sum', breakfast_staple_type='bao', "
        "meal_roles='[\"one_pot_meal\", \"staple\"]', updated_at=datetime('now') "
        "WHERE name_cn='汤饺'"
    )

    # Breakfast plate: fruit and bread are selectable extras, not required structure.
    breakfast_rows = conn.execute(
        "SELECT id,protein_types,vegetables FROM dishes "
        "WHERE name_cn LIKE '牛油果%早餐盘%'"
    ).fetchall()
    for row in breakfast_rows:
        proteins = [v for v in _json_list(row["protein_types"]) if v not in ("none", "无")]
        vegetables = [v for v in _json_list(row["vegetables"]) if v not in ("火腿", "水果")]
        conn.execute(
            "UPDATE dishes SET protein_types=?,vegetables=?,vegetable_count=?,updated_at=datetime('now') "
            "WHERE id=?",
            (json.dumps(proteins, ensure_ascii=False), json.dumps(vegetables, ensure_ascii=False),
             len(vegetables), row["id"]),
        )
        conn.execute(
            "DELETE FROM dish_ingredients WHERE dish_id=? AND ingredient_id IN ('面包','bread')",
            (row["id"],),
        )

    _merge_json_field(conn, "牛油果洋葱酱三文鱼籽", "meal_roles", add=("protein_main",), remove=("vegetable_dish",))
    for name in ("酱油凉拌豆腐牛油果", "鸡丝青瓜丝嫩豆腐", "豆腐蒸蛋"):
        _merge_json_field(conn, name, "meal_roles", add=("tofu_dish", "protein_main"))
        _merge_json_field(conn, name, "protein_types", add=("tofu",), remove=("none", "无"))

    conn.execute(
        "UPDATE dishes SET category_id='soup',quick_soup=1,slow_soup=0,"
        "meal_roles='[\"quick_soup\"]',updated_at=datetime('now') "
        "WHERE name_cn IN ('番茄蘑菇蛋汤','番茄（西红柿）蘑菇蛋汤')"
    )

    # Renamed noodle onepot: its only hard ingredients are the two placeholders.
    noodle_ids = _dish_ids(conn, "凉拌虾仁荞麦面") + _dish_ids(conn, "营养拌面（蛋白质+蔬菜）")
    for dish_id in set(noodle_ids):
        conn.execute(
            "UPDATE dishes SET name_cn='营养拌面（蛋白质+蔬菜）',"
            "name_en='Nutritious Mixed Noodles (Protein + Vegetables)',"
            "protein_types='[]',vegetables='[]',vegetable_count=0,image=NULL,image_uploaded=0,"
            "meal_roles='[\"one_pot_meal\"]',updated_at=datetime('now') WHERE id=?",
            (dish_id,),
        )
        conn.execute("DELETE FROM dish_ingredients WHERE dish_id=?", (dish_id,))
        for ingredient_id in ("any_available_protein", "any_available_vegetable"):
            conn.execute(
                "INSERT INTO dish_ingredients (dish_id,ingredient_id,required) VALUES (?,?,1)",
                (dish_id, ingredient_id),
            )

    # Required-ingredient adjudications.
    _replace_required(conn, "鱼籽寿司卷", ("fish", "鱼籽", "黑鱼子酱"), ("三文鱼籽",))
    for name in ("水煮鱼", "姜葱鱼焖饭"):
        _replace_required(conn, name, ("fish", "cod", "mackerel"), ("any_available_fish",))
    _replace_required(conn, "清蒸红斑鱼", ("fish", "cod", "mackerel"), ("any_available_grouper",))
    for name in ("番茄菌菇小白菜汤", "番茄菌菇豆腐汤"):
        _replace_required(
            conn, name,
            ("mushroom", "mushroom_generic", "button_mushroom", "buna_mushroom", "maitake"),
            ("any_available_mushroom",),
        )
    _replace_required(
        conn, "杂蔬虾仁藜麦炒饭", ("西兰花", "broccoli", "mixed_veg", "蔬菜"),
        ("any_available_vegetable",),
    )
    _replace_required(conn, "酸辣娃娃菜", ("white_cabbage", "白菜"), ("baby_cabbage",))

    # Role backfills explicitly confirmed by Vivian.
    _merge_json_field(conn, "沙拉菜", "meal_roles", add=("vegetable_dish",))
    _merge_json_field(conn, "日式拌豆腐（木鱼花、柴鱼片）", "meal_roles", add=("tofu_dish", "protein_main"), remove=("vegetable_dish",))
    for name in ("日式拌豆腐（木鱼花）", "日式拌豆腐（柴鱼片）"):
        _merge_json_field(conn, name, "meal_roles", add=("tofu_dish", "protein_main"), remove=("vegetable_dish",))
    for name in ("日式溏心蛋", "金银蛋炒虾仁"):
        _merge_json_field(conn, name, "meal_roles", add=("egg_dish",))
    for name in QUICK_SOUPS:
        _merge_json_field(conn, name, "meal_roles", add=("quick_soup",))
    for name in SLOW_SOUPS:
        _merge_json_field(conn, name, "meal_roles", add=("slow_soup",))
    for name in VEGETABLE_ROLE_BACKFILL:
        _merge_json_field(conn, name, "meal_roles", add=("vegetable_dish",))
    for name in PROTEIN_ROLE_BACKFILL:
        _merge_json_field(conn, name, "meal_roles", add=("protein_main",))

    conn.execute(
        "UPDATE dishes SET category_id='staple_carb',carb_type='other',"
        "breakfast_staple_type='sourdough',meal_roles='[\"staple\"]',"
        "updated_at=datetime('now') WHERE name_cn='酸种面包'"
    )
    conn.execute(
        "UPDATE dishes SET name_en='Beef Braised Rice',image=NULL,image_uploaded=0,"
        "updated_at=datetime('now') WHERE name_cn='牛肉焖饭'"
    )
    conn.execute(
        "UPDATE dishes SET category_id='cold_dish',updated_at=datetime('now') "
        "WHERE name_cn IN ('黑鱼子酱配豆腐','四味豆腐沙拉')"
    )
    _merge_json_field(conn, "火腿松茸豆腐汤", "protein_types", add=("pork",), remove=("none", "无"))


def _assert_adjudication(conn, pending_before, legacy_protein_pool):
    if _snapshot_pending(conn) != pending_before:
        raise AssertionError("protected pending_review dishes changed")

    tang = conn.execute(
        "SELECT meal_tags,meal_roles,breakfast_staple_type FROM dishes WHERE name_cn='汤饺'"
    ).fetchone()
    if tang and (set(_json_list(tang["meal_tags"])) != {"breakfast", "lunch"}
                 or "one_pot_meal" not in _json_list(tang["meal_roles"])
                 or tang["breakfast_staple_type"] != "bao"):
        raise AssertionError("汤饺 adjudication incomplete")
    water = conn.execute(
        "SELECT meal_roles FROM dishes WHERE name_cn='水饺'"
    ).fetchone()
    if water and "one_pot_meal" in _json_list(water["meal_roles"]):
        raise AssertionError("水饺 must remain non-onepot")

    for name in PROTEIN_ROLE_BACKFILL:
        row = conn.execute("SELECT meal_roles FROM dishes WHERE name_cn=?", (name,)).fetchone()
        if row and "protein_main" not in _json_list(row["meal_roles"]):
            raise AssertionError(f"protein role missing: {name}")
    role_rows = conn.execute(
        "SELECT id,meal_roles FROM dishes WHERE id IN ({})".format(
            ",".join("?" for _ in legacy_protein_pool)
        ),
        tuple(sorted(legacy_protein_pool)),
    ).fetchall() if legacy_protein_pool else []
    role_pool = {
        row["id"] for row in role_rows
        if "protein_main" in _json_list(row["meal_roles"])
    }
    if role_pool != legacy_protein_pool:
        missing = sorted(legacy_protein_pool - role_pool)
        raise AssertionError(f"protein role switch would drop legacy members: {missing}")
    for name in VEGETABLE_ROLE_BACKFILL:
        row = conn.execute("SELECT meal_roles FROM dishes WHERE name_cn=?", (name,)).fetchone()
        if row and "vegetable_dish" not in _json_list(row["meal_roles"]):
            raise AssertionError(f"vegetable role missing: {name}")

    placeholder_count = conn.execute(
        "SELECT COUNT(*) FROM ingredients WHERE ingredient_id IN (?,?,?,?,?)",
        tuple(PLACEHOLDERS),
    ).fetchone()[0]
    if placeholder_count != 5:
        raise AssertionError("five-placeholder system incomplete")
    noodle_class = conn.execute(
        "SELECT COUNT(*) FROM ingredient_classifications WHERE class_id='noodle'"
    ).fetchone()[0]
    if noodle_class < 1:
        raise AssertionError("noodle pantry exemption has no strict class members")


def apply_adjudication(conn, strict=True):
    conn.row_factory = sqlite3.Row
    ensure_schema(conn)
    pending_before = _snapshot_pending(conn)
    if strict:
        _assert_strict_source_records(conn)
    protected_marks = ",".join("?" for _ in PROTECTED_PENDING_IDS)
    conn.execute(
        "UPDATE dishes SET ingredients_pending=1,updated_at=datetime('now') "
        "WHERE is_active=1 AND id NOT IN ({}) AND NOT EXISTS ("
        "SELECT 1 FROM dish_ingredients di WHERE di.dish_id=dishes.id AND di.required=1)".format(
            protected_marks
        ),
        PROTECTED_PENDING_IDS,
    )
    legacy_protein_pool = {
        row["id"] for row in conn.execute(
            "SELECT id FROM dishes WHERE is_active=1 AND category_id='protein_main' "
            "AND COALESCE(banquet,0)=0 AND COALESCE(ingredients_pending,0)=0 "
            "AND COALESCE(pending_review,'')=''"
        )
        if row["id"] not in PROTECTED_PENDING_IDS
    }
    _update_dishes(conn)
    _assert_adjudication(conn, pending_before, legacy_protein_pool)
    row = conn.execute("SELECT value FROM config WHERE key='catalog_version'").fetchone()
    version = str(int(row["value"] or 0) + 1) if row else "1"
    conn.execute(
        "INSERT INTO config(key,value) VALUES ('catalog_version',?) "
        "ON CONFLICT(key) DO UPDATE SET value=excluded.value", (version,),
    )


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--db", required=True, help="explicit SQLite database path")
    parser.add_argument("--apply", action="store_true", help="commit changes (default: rollback)")
    args = parser.parse_args()
    db_path = os.path.abspath(args.db)
    if not os.path.isfile(db_path):
        raise SystemExit(f"database not found: {db_path}")

    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    try:
        conn.execute("PRAGMA foreign_keys=OFF")
        conn.execute("BEGIN IMMEDIATE")
        apply_adjudication(conn)
        if args.apply:
            conn.commit()
            print(f"APPLIED {db_path}")
        else:
            conn.rollback()
            print(f"DRY-RUN OK (rolled back) {db_path}")
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.execute("PRAGMA foreign_keys=ON")
        conn.close()


if __name__ == "__main__":
    main()
