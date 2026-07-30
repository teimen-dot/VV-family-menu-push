#!/usr/bin/env python3
"""
迁移脚本：从 JSON 文件迁移到 SQLite
- dish_pool.json → dishes + categories + ingredients + dish_ingredients + custom_tags_def
- menu_data.json → menus + menu_items（标记为历史数据）
- photo_manifest.json → dishes.image / image_uploaded
- 预设 diners + dietary_alerts + config
不破坏旧 JSON 文件。
"""

import json
import os
from datetime import datetime
from db import (
    get_db, init_db, log_event,
    normalize_ingredient, get_ingredient_en,
    INGREDIENT_ALIASES, INGREDIENT_EN_NAMES
)

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
DISH_POOL_FILE = os.path.join(BASE_DIR, "dish_pool.json")
MENU_DATA_FILE = os.path.join(BASE_DIR, "menu_data.json")
MANIFEST_FILE = os.path.join(BASE_DIR, "photo_manifest.json")


def to_json_str(value):
    """Python list/dict → JSON string for SQLite"""
    if value is None:
        return "[]"
    if isinstance(value, str):
        return value
    return json.dumps(value, ensure_ascii=False)


def migrate_categories(pool_data):
    """迁移分类"""
    conn = get_db()
    count = 0
    for cat in pool_data.get("categories", []):
        conn.execute(
            "INSERT OR REPLACE INTO categories (id, label_cn, label_en, sort_order, active) "
            "VALUES (?, ?, ?, ?, ?)",
            (cat["id"], cat["label_cn"], cat.get("label_en", ""),
             cat.get("order", 0), 1 if cat.get("active", True) else 0)
        )
        count += 1
    conn.commit()
    conn.close()
    print(f"  分类迁移: {count} 条")


def migrate_custom_tags(pool_data):
    """迁移自定义标签"""
    conn = get_db()
    count = 0
    for tag in pool_data.get("custom_tags_def", []):
        label = tag.get("label", "")
        if label:
            try:
                conn.execute("INSERT INTO custom_tags_def (label) VALUES (?)", (label,))
                count += 1
            except sqlite3.IntegrityError:
                pass  # 已存在跳过
    conn.commit()
    conn.close()
    print(f"  自定义标签迁移: {count} 条")


def migrate_ingredients_and_dishes(pool_data, manifest):
    """迁移食材库 + 菜品 + 菜品-食材关联"""
    conn = get_db()

    # 第一遍：收集所有食材并写入 ingredients 表
    ingredient_registry = {}  # ingredient_id → {name_cn, name_en, aliases, category}
    dish_ingredient_links = []  # [(dish_id, ingredient_id, required)]

    for dish in pool_data.get("dishes", []):
        raw_ingredients = dish.get("ingredients", [])
        for ing_name in raw_ingredients:
            ing_id = normalize_ingredient(ing_name)
            if ing_id not in ingredient_registry:
                ingredient_registry[ing_id] = {
                    "name_cn": ing_name,
                    "name_en": get_ingredient_en(ing_id, ing_name),
                    "aliases": [],
                    "category": classify_ingredient(ing_name)
                }
            # 收集别名（如果 ingredient_id 已存在但 name_cn 不同，加入 aliases）
            if ingredient_registry[ing_id]["name_cn"] != ing_name:
                if ing_name not in ingredient_registry[ing_id]["aliases"]:
                    ingredient_registry[ing_id]["aliases"].append(ing_name)

    # 写入 ingredients 表
    for ing_id, info in ingredient_registry.items():
        conn.execute(
            "INSERT OR REPLACE INTO ingredients "
            "(ingredient_id, name_cn, name_en, aliases, category) "
            "VALUES (?, ?, ?, ?, ?)",
            (ing_id, info["name_cn"], info["name_en"],
             to_json_str(info["aliases"]), info.get("category"))
        )
    print(f"  食材迁移: {len(ingredient_registry)} 条")

    # 第二遍：写入菜品 + dish_ingredients
    dish_count = 0
    link_count = 0
    for dish in pool_data.get("dishes", []):
        dish_id = dish["id"]
        name_cn = dish["name_cn"]
        image_file = ""
        image_uploaded = 0
        if name_cn in manifest:
            image_file = manifest[name_cn].get("file", "")
            image_uploaded = 1 if image_file else 0

        conn.execute(
            "INSERT OR REPLACE INTO dishes "
            "(id, name_cn, name_en, category_id, meal_tags, banquet, "
            " protein_types, vegetables, vegetable_count, carb_type, "
            " breakfast_staple_type, meal_components, taste, cooking_methods, "
            " can_serve_warm, custom_tags, allergens, dietary_tags, "
            " image, image_uploaded, needs_review, old_category, old_tags) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                dish_id,
                name_cn,
                dish.get("name_en", ""),
                dish.get("category_id"),
                to_json_str(dish.get("meal_tags", [])),
                1 if dish.get("banquet") else 0,
                to_json_str(dish.get("protein_types", [])),
                to_json_str(dish.get("vegetables", [])),
                dish.get("vegetable_count", 0),
                dish.get("carb_type"),
                dish.get("breakfast_staple_type"),
                to_json_str(dish.get("meal_components", [])),
                dish.get("taste"),
                to_json_str(dish.get("cooking_methods", [])),
                1 if dish.get("can_serve_warm") else 0,
                to_json_str(dish.get("custom_tags", [])),
                to_json_str(dish.get("allergens", [])),
                to_json_str(dish.get("dietary_tags", [])),
                image_file,
                image_uploaded,
                1 if dish.get("needs_review") else 0,
                dish.get("old_category"),
                to_json_str(dish.get("old_tags", [])),
            )
        )
        dish_count += 1

        # 写入 dish_ingredients 关联
        for ing_name in dish.get("ingredients", []):
            ing_id = normalize_ingredient(ing_name)
            try:
                conn.execute(
                    "INSERT OR IGNORE INTO dish_ingredients (dish_id, ingredient_id, required) "
                    "VALUES (?, ?, ?)",
                    (dish_id, ing_id, 1)
                )
                link_count += 1
            except Exception as e:
                print(f"    [WARN] dish_ingredient 关联失败: {dish_id} → {ing_id}: {e}")

    conn.commit()
    conn.close()
    print(f"  菜品迁移: {dish_count} 条")
    print(f"  菜品-食材关联: {link_count} 条")


def classify_ingredient(name_cn):
    """粗略分类食材"""
    protein_words = ["牛肉", "鸡肉", "鸡腿", "猪肉", "排骨", "肉", "和牛", "牛排",
                     "虾", "鱼", "鳕鱼", "青花", "带子", "蛤蜊", "黑虎虾", "虾滑",
                     "三文鱼", "黑鱼子酱", "火腿", "肉丸", "肉末", "皮蛋"]
    vegetable_words = ["菜", "瓜", "笋", "菇", "菌", "葱", "蒜", "姜", "萝卜",
                       "薯", "淮山", "百合", "秋葵", "芹菜", "芦笋", "芥蓝",
                       "豆苗", "苋菜", "茄子", "茴香", "莲", "椒", "西兰花",
                       "豆芽", "空心菜", "枸杞", "紫菜", "腐竹", "蚕豆"]
    grain_words = ["米", "小米", "面条", "面包", "藜麦", "莲子", "16谷"]
    fruit_words = ["蓝莓", "黑莓", "橙子", "橘子", "苹果", "香蕉", "奇异果",
                   "车厘子", "水蜜桃", "柠檬", "牛油果"]
    seasoning_words = ["酱", "味噌", "黄油", "巧克力", "酸奶", "XO"]

    for w in protein_words:
        if w in name_cn:
            return "protein"
    for w in vegetable_words:
        if w in name_cn:
            return "vegetable"
    for w in grain_words:
        if w in name_cn:
            return "grain"
    for w in fruit_words:
        if w in name_cn:
            return "fruit"
    for w in seasoning_words:
        if w in name_cn:
            return "seasoning"
    return "other"


def migrate_menu_history(menu_data, default_location="shenzhen"):
    """迁移旧 20 天菜单为历史数据（status=pushed）"""
    conn = get_db()
    cycle_start = menu_data.get("cycle_start", "2026-07-28")
    menu_list = menu_data.get("menu", [])
    count = 0

    from datetime import date as date_cls, timedelta
    start_date = datetime.strptime(cycle_start, "%Y-%m-%d").date()

    for i, entry in enumerate(menu_list):
        day_date = (start_date + timedelta(days=i)).isoformat()
        today = date_cls.today().isoformat()
        status = "pushed" if day_date < today else "draft"

        notes = entry.get("notes", {})
        notes_zh = notes.get("zh", "")
        notes_en = notes.get("en", "")

        conn.execute(
            "INSERT OR REPLACE INTO menus "
            "(date, location, status, auto_confirmed, notes_zh, notes_en) "
            "VALUES (?, ?, ?, 0, ?, ?)",
            (day_date, default_location, status, notes_zh, notes_en)
        )
        menu_row = conn.execute(
            "SELECT id FROM menus WHERE date = ?", (day_date,)
        ).fetchone()
        menu_id = menu_row["id"]

        meal_map = {
            "breakfast": "breakfast",
            "lunch": "lunch",
            "afternoon_snack": "afternoon_snack",
            "dinner": "dinner",
        }
        sort_order = 0
        for meal_key, meal_type in meal_map.items():
            meal = entry.get(meal_key, {})
            zh = meal.get("zh", "")
            if not zh or zh == "无需安排":
                continue
            # 旧菜单存的是文本，不是 dish_id，无法直接关联
            # 暂存为 note，后续需要时再人工/自动关联
            conn.execute(
                "INSERT INTO menu_items (menu_id, dish_id, meal_type, is_locked, sort_order) "
                "VALUES (?, ?, ?, 0, ?)",
                (menu_id, zh, meal_type, sort_order)
            )
            sort_order += 1

        count += 1

    conn.commit()
    conn.close()
    print(f"  历史菜单迁移: {count} 天（旧文本格式，dish_id 待关联）")


def seed_diners():
    """预设家庭成员"""
    conn = get_db()
    diners = [
        ("vivian", "Vivian", "Vivian", "owner", 1, 0),
        ("sir", "老板", "Sir", "owner", 1, 1),
        ("toby", "Toby", "Toby", "family", 1, 2),
        ("ben", "Ben", "Ben", "family", 0, 3),  # 默认不在家吃
    ]
    for d in diners:
        conn.execute(
            "INSERT OR REPLACE INTO diners "
            "(id, name_cn, name_en, role, default_attends, sort_order) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            d
        )

    # Ben 的过敏信息
    conn.execute(
        "INSERT OR REPLACE INTO dietary_alerts (diner_id, allergen, severity, notes) "
        "VALUES (?, ?, ?, ?)",
        ("ben", "shellfish", "avoid", "海鲜/甲壳类过敏")
    )
    conn.execute(
        "INSERT OR REPLACE INTO dietary_alerts (diner_id, allergen, severity, notes) "
        "VALUES (?, ?, ?, ?)",
        ("ben", "seafood", "avoid", "海鲜过敏")
    )

    conn.commit()
    conn.close()
    print(f"  用餐成员: 4 人（Ben 标记海鲜/甲壳类过敏）")


def seed_config():
    """预设系统配置"""
    from db import set_config
    set_config("timezone", "Asia/Hong_Kong", "统一时区")
    set_config("inventory_deadline", "20:00", "保姆库存提交截止时间")
    set_config("owner_reminder_time", "21:30", "主人未确认提醒时间")
    set_config("auto_fallback_time", "06:00", "自动兜底时间")
    set_config("push_time", "10:30", "常规推送时间")
    set_config("default_location", "shenzhen", "默认地点")
    set_config("github_raw_base",
               "https://raw.githubusercontent.com/teimen-dot/VV-family-menu-push/main",
               "GitHub raw URL 基地址")
    set_config("pushplus_topic", "home-menu", "PushPlus 群组 topic")
    print("  系统配置: 7 项")


def verify_migration():
    """验证迁移结果"""
    conn = get_db()
    tables = [
        "categories", "dishes", "ingredients", "dish_ingredients",
        "custom_tags_def", "menus", "menu_items", "diners",
        "dietary_alerts", "config", "events"
    ]
    print("\n=== 迁移验证 ===")
    all_good = True
    for table in tables:
        count = conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
        print(f"  {table}: {count} 条")
    print("=== 验证完成 ===\n")
    conn.close()


def main():
    print("=" * 50)
    print("JSON → SQLite 迁移")
    print(f"时间: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
    print("=" * 50)

    # 0. 初始化数据库
    print("\n[1] 初始化数据库...")
    init_db()

    # 1. 加载 JSON 数据
    print("\n[2] 加载 JSON 数据...")
    with open(DISH_POOL_FILE, "r", encoding="utf-8") as f:
        pool_data = json.load(f)
    with open(MENU_DATA_FILE, "r", encoding="utf-8") as f:
        menu_data = json.load(f)
    manifest = {}
    if os.path.exists(MANIFEST_FILE):
        with open(MANIFEST_FILE, "r", encoding="utf-8") as f:
            manifest = json.load(f)
    print(f"  dish_pool.json: {len(pool_data['dishes'])} 道菜")
    print(f"  menu_data.json: {len(menu_data['menu'])} 天菜单")
    print(f"  photo_manifest.json: {len(manifest)} 条映射")

    # 2. 迁移
    print("\n[3] 迁移分类...")
    migrate_categories(pool_data)

    print("\n[4] 迁移自定义标签...")
    migrate_custom_tags(pool_data)

    print("\n[5] 迁移食材+菜品+关联...")
    migrate_ingredients_and_dishes(pool_data, manifest)

    print("\n[6] 迁移历史菜单...")
    migrate_menu_history(menu_data)

    print("\n[7] 预设用餐成员...")
    seed_diners()

    print("\n[8] 预设系统配置...")
    seed_config()

    # 3. 验证
    verify_migration()

    # 4. 记录审计日志
    log_event("migration_complete", "system", None, {
        "source": "JSON files",
        "target": "SQLite",
        "dishes": len(pool_data["dishes"]),
        "menus": len(menu_data["menu"]),
    })

    print("迁移完成！旧 JSON 文件保留不变。")


if __name__ == "__main__":
    main()
