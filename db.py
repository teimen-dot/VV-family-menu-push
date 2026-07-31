#!/usr/bin/env python3
"""
家庭菜单管家 - SQLite 数据库层
统一数据模型，替代多 JSON 文件方案。
"""

import json
import sqlite3
import os
from datetime import datetime

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
DB_PATH = os.path.join(BASE_DIR, "family_menu.db")


def get_db():
    """获取数据库连接（启用外键约束）"""
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    conn.execute("PRAGMA journal_mode = WAL")
    return conn


def _safe_add_column(cursor, table, column, col_type):
    """安全地为已存在的表添加列（幂等，列已存在则跳过）"""
    try:
        cursor.execute(f"ALTER TABLE {table} ADD COLUMN {column} {col_type}")
    except sqlite3.OperationalError:
        pass  # 列已存在


def init_db():
    """创建所有表（幂等操作，已存在则跳过）"""
    conn = get_db()
    c = conn.cursor()

    # ========== 1. categories - 菜品分类 ==========
    c.execute("""
        CREATE TABLE IF NOT EXISTS categories (
            id          TEXT PRIMARY KEY,
            label_cn    TEXT NOT NULL,
            label_en    TEXT,
            sort_order  INTEGER DEFAULT 0,
            active      INTEGER DEFAULT 1
        )
    """)

    # ========== 2. dishes - 菜品库 ==========
    c.execute("""
        CREATE TABLE IF NOT EXISTS dishes (
            id                      TEXT PRIMARY KEY,
            name_cn                 TEXT NOT NULL,
            name_en                 TEXT,
            category_id             TEXT,
            meal_tags               TEXT DEFAULT '[]',
            banquet                 INTEGER DEFAULT 0,
            protein_types           TEXT DEFAULT '[]',
            vegetables              TEXT DEFAULT '[]',
            vegetable_count         INTEGER DEFAULT 0,
            carb_type               TEXT,
            breakfast_staple_type   TEXT,
            meal_components         TEXT DEFAULT '[]',
            taste                   TEXT,
            cooking_methods         TEXT DEFAULT '[]',
            can_serve_warm          INTEGER DEFAULT 0,
            custom_tags             TEXT DEFAULT '[]',
            allergens               TEXT DEFAULT '[]',  -- DEPRECATED: 不再用于过敏判断，保留旧数据
            dietary_tags            TEXT DEFAULT '[]',
            quick_soup              INTEGER DEFAULT 0,
            slow_soup               INTEGER DEFAULT 0,
            manual_only_for_breakfast INTEGER DEFAULT 0,
            meal_roles               TEXT DEFAULT '[]',
            image                   TEXT,
            image_uploaded          INTEGER DEFAULT 0,
            needs_review            INTEGER DEFAULT 0,
            old_category            TEXT,
            old_tags                TEXT DEFAULT '[]',
            created_at              TEXT DEFAULT (datetime('now')),
            updated_at              TEXT DEFAULT (datetime('now')),
            FOREIGN KEY (category_id) REFERENCES categories(id)
        )
    """)

    # V3 迁移：为已存在的 dishes 表添加新列（幂等）
    _safe_add_column(c, "dishes", "quick_soup", "INTEGER DEFAULT 0")
    _safe_add_column(c, "dishes", "slow_soup", "INTEGER DEFAULT 0")
    _safe_add_column(c, "dishes", "manual_only_for_breakfast", "INTEGER DEFAULT 0")

    # V6 迁移：dishes 表增加 is_active + deleted_at（Soft Delete）
    _safe_add_column(c, "dishes", "is_active", "INTEGER DEFAULT 1")
    _safe_add_column(c, "dishes", "deleted_at", "TEXT")

    # V8 迁移：dishes 表增加 meal_roles（多选角色字段）
    _safe_add_column(c, "dishes", "meal_roles", "TEXT DEFAULT '[]'")

    # ========== V11: dish_preference_stats - VV 常选菜统计 ==========
    c.execute("""
        CREATE TABLE IF NOT EXISTS dish_preference_stats (
            dish_id                 TEXT PRIMARY KEY,
            vv_confirm_count        INTEGER DEFAULT 0,
            vv_confirm_count_30d    INTEGER DEFAULT 0,
            last_confirmed_at       TEXT,
            last_selected_at        TEXT,
            FOREIGN KEY (dish_id) REFERENCES dishes(id)
        )
    """)

    # V11: menus 表增加 meal_mode + banquet_total_diners
    _safe_add_column(c, "menus", "meal_mode", "TEXT DEFAULT 'daily'")
    _safe_add_column(c, "menus", "banquet_total_diners", "INTEGER")

    # ========== 3. ingredients - 食材库 ==========
    c.execute("""
        CREATE TABLE IF NOT EXISTS ingredients (
            ingredient_id   TEXT PRIMARY KEY,
            name_cn         TEXT NOT NULL,
            name_en         TEXT,
            aliases         TEXT DEFAULT '[]',
            category        TEXT,
            is_common       INTEGER DEFAULT 0,
            created_at      TEXT DEFAULT (datetime('now'))
        )
    """)

    # V4 迁移：为已存在的 ingredients 表添加 is_common 列（幂等）
    _safe_add_column(c, "ingredients", "is_common", "INTEGER DEFAULT 0")

    # ========== 4. dish_ingredients - 菜品-食材关联 ==========
    c.execute("""
        CREATE TABLE IF NOT EXISTS dish_ingredients (
            id              INTEGER PRIMARY KEY AUTOINCREMENT,
            dish_id         TEXT NOT NULL,
            ingredient_id   TEXT NOT NULL,
            required        INTEGER DEFAULT 1,
            FOREIGN KEY (dish_id) REFERENCES dishes(id),
            FOREIGN KEY (ingredient_id) REFERENCES ingredients(ingredient_id),
            UNIQUE(dish_id, ingredient_id)
        )
    """)

    # ========== 5. custom_tags_def - 自定义标签定义 ==========
    c.execute("""
        CREATE TABLE IF NOT EXISTS custom_tags_def (
            id      INTEGER PRIMARY KEY AUTOINCREMENT,
            label   TEXT NOT NULL UNIQUE
        )
    """)

    # ========== 6. inventory - 库存记录（按地点+日期） ==========
    c.execute("""
        CREATE TABLE IF NOT EXISTS inventory (
            id              INTEGER PRIMARY KEY AUTOINCREMENT,
            location        TEXT NOT NULL,
            date            TEXT NOT NULL,
            submitted_by    TEXT,
            submitted_at    TEXT,
            status          TEXT DEFAULT 'pending',
            notes           TEXT,
            created_at      TEXT DEFAULT (datetime('now')),
            UNIQUE(location, date)
        )
    """)

    # ========== 7. inventory_items - 库存条目（历史快照明细） ==========
    c.execute("""
        CREATE TABLE IF NOT EXISTS inventory_items (
            id              INTEGER PRIMARY KEY AUTOINCREMENT,
            inventory_id    INTEGER NOT NULL,
            ingredient_id   TEXT NOT NULL,
            status          TEXT DEFAULT 'available',
            notes           TEXT,
            FOREIGN KEY (inventory_id) REFERENCES inventory(id),
            FOREIGN KEY (ingredient_id) REFERENCES ingredients(ingredient_id)
        )
    """)

    # ========== 7a. current_pantry - V4 当前持续库存（增量维护） ==========
    c.execute("""
        CREATE TABLE IF NOT EXISTS current_pantry (
            id              INTEGER PRIMARY KEY AUTOINCREMENT,
            location        TEXT NOT NULL,
            ingredient_id   TEXT NOT NULL,
            status          TEXT DEFAULT 'available',
            is_active       INTEGER DEFAULT 1,
            created_at      TEXT DEFAULT (datetime('now')),
            updated_at      TEXT DEFAULT (datetime('now')),
            UNIQUE(location, ingredient_id)
        )
    """)

    # ========== 7b. inventory_snapshots - V4 库存快照（审计追溯） ==========
    c.execute("""
        CREATE TABLE IF NOT EXISTS inventory_snapshots (
            id              INTEGER PRIMARY KEY AUTOINCREMENT,
            location        TEXT NOT NULL,
            items_json      TEXT,
            created_at      TEXT DEFAULT (datetime('now'))
        )
    """)

    # V4: menus 表增加 inventory_snapshot_id 列
    _safe_add_column(c, "menus", "inventory_snapshot_id", "INTEGER")

    # ========== 8. menus - 每日菜单（按真实日期） ==========
    c.execute("""
        CREATE TABLE IF NOT EXISTS menus (
            id              INTEGER PRIMARY KEY AUTOINCREMENT,
            date            TEXT NOT NULL UNIQUE,
            location        TEXT NOT NULL,
            status          TEXT DEFAULT 'draft',
            auto_confirmed  INTEGER DEFAULT 0,
            confirmed_at    TEXT,
            pushed_at       TEXT,
            diners_count    INTEGER DEFAULT 4,
            diners          TEXT DEFAULT '[]',
            notes_zh        TEXT,
            notes_en        TEXT,
            created_at      TEXT DEFAULT (datetime('now')),
            updated_at      TEXT DEFAULT (datetime('now'))
        )
    """)

    # ========== 9. menu_items - 菜单中的菜品 ==========
    # dish_id: 新数据存 dishes.id；历史迁移数据存原始文本（无 FK 约束以兼容）
    # V6: custom_name 用于手动添加的非菜品库菜品；dish_id 或 custom_name 至少一个非空
    c.execute("""
        CREATE TABLE IF NOT EXISTS menu_items (
            id              INTEGER PRIMARY KEY AUTOINCREMENT,
            menu_id         INTEGER NOT NULL,
            dish_id         TEXT,
            custom_name     TEXT,
            meal_type       TEXT NOT NULL,
            is_locked       INTEGER DEFAULT 0,
            locked_by       TEXT,
            locked_at       TEXT,
            sort_order      INTEGER DEFAULT 0,
            source          TEXT DEFAULT 'ai',
            FOREIGN KEY (menu_id) REFERENCES menus(id)
        )
    """)

    # V6 迁移：为已存在的 menu_items 表添加新列（幂等）
    _safe_add_column(c, "menu_items", "custom_name", "TEXT")
    _safe_add_column(c, "menu_items", "source", "TEXT DEFAULT 'ai'")

    # ========== 10. selections - 老板点菜记录 ==========
    c.execute("""
        CREATE TABLE IF NOT EXISTS selections (
            id                  INTEGER PRIMARY KEY AUTOINCREMENT,
            menu_id             INTEGER NOT NULL,
            dish_id             TEXT NOT NULL,
            meal_type           TEXT,
            selected_by         TEXT,
            selected_at         TEXT DEFAULT (datetime('now')),
            shortage_handled    INTEGER DEFAULT 0,
            purchase_approved   INTEGER DEFAULT 0,
            FOREIGN KEY (menu_id) REFERENCES menus(id)
        )
    """)

    # ========== 11. purchase_requests - 采购任务 ==========
    c.execute("""
        CREATE TABLE IF NOT EXISTS purchase_requests (
            id              INTEGER PRIMARY KEY AUTOINCREMENT,
            menu_date       TEXT NOT NULL,
            location        TEXT NOT NULL,
            dish_id         TEXT,
            ingredient_id   TEXT NOT NULL,
            status          TEXT DEFAULT 'needed',
            created_at      TEXT DEFAULT (datetime('now')),
            notified_at     TEXT,
            resolved_at     TEXT,
            resolved_by     TEXT,
            notes           TEXT,
            FOREIGN KEY (ingredient_id) REFERENCES ingredients(ingredient_id)
        )
    """)

    # ========== 12. diners - 用餐成员 ==========
    c.execute("""
        CREATE TABLE IF NOT EXISTS diners (
            id              TEXT PRIMARY KEY,
            name_cn         TEXT NOT NULL,
            name_en         TEXT,
            role            TEXT,
            default_attends INTEGER DEFAULT 1,
            sort_order      INTEGER DEFAULT 0
        )
    """)

    # ========== 13. dietary_alerts - DEPRECATED: 忌口/过敏（保留旧数据，Runtime/API/UI/AI 不再读取） ==========
    c.execute("""
        CREATE TABLE IF NOT EXISTS dietary_alerts (
            id          INTEGER PRIMARY KEY AUTOINCREMENT,
            diner_id    TEXT NOT NULL,
            allergen    TEXT NOT NULL,
            severity    TEXT DEFAULT 'avoid',
            notes       TEXT,
            FOREIGN KEY (diner_id) REFERENCES diners(id)
        )
    """)

    # ========== 14. events - 审计日志 ==========
    c.execute("""
        CREATE TABLE IF NOT EXISTS events (
            id          INTEGER PRIMARY KEY AUTOINCREMENT,
            event_type  TEXT NOT NULL,
            entity_type TEXT,
            entity_id   TEXT,
            details     TEXT,
            created_at  TEXT DEFAULT (datetime('now'))
        )
    """)

    # ========== 15. config - 系统配置 ==========
    c.execute("""
        CREATE TABLE IF NOT EXISTS config (
            key     TEXT PRIMARY KEY,
            value   TEXT,
            notes   TEXT
        )
    """)

    conn.commit()
    conn.close()
    print("[OK] 数据库初始化完成")


def log_event(event_type, entity_type=None, entity_id=None, details=None):
    """记录审计日志"""
    conn = get_db()
    conn.execute(
        "INSERT INTO events (event_type, entity_type, entity_id, details) VALUES (?, ?, ?, ?)",
        (event_type, entity_type, entity_id, json.dumps(details, ensure_ascii=False) if details else None)
    )
    conn.commit()
    conn.close()


def get_config(key, default=None):
    """读取配置项"""
    conn = get_db()
    row = conn.execute("SELECT value FROM config WHERE key = ?", (key,)).fetchone()
    conn.close()
    return row["value"] if row else default


def set_config(key, value, notes=None):
    """写入配置项"""
    conn = get_db()
    conn.execute(
        "INSERT INTO config (key, value, notes) VALUES (?, ?, ?) "
        "ON CONFLICT(key) DO UPDATE SET value = ?, notes = ?",
        (key, value, notes, value, notes)
    )
    conn.commit()
    conn.close()


# ========== 食材别名归一化表 ==========
# 将中文同义词映射到统一 ingredient_id
INGREDIENT_ALIASES = {
    # 红薯/番薯/地瓜
    "红薯": "sweet_potato", "番薯": "sweet_potato", "地瓜": "sweet_potato",
    # 松茸
    "松茸": "matsutake", "鲜松茸": "matsutake", "新鲜松茸": "matsutake",
    # 米饭/白米
    "白米": "rice", "米饭": "rice",
    # 鸡肉/鸡腿肉
    "鸡肉": "chicken", "鸡腿肉": "chicken_leg",
    # 牛肉/牛排/和牛
    "牛肉": "beef", "牛排": "beef_steak", "和牛": "wagyu",
    # 豆腐/嫩豆腐
    "豆腐": "tofu", "嫩豆腐": "silken_tofu",
    # 菌菇/口蘑/蟹味菇/舞茸
    "菌菇": "mushroom", "口蘑": "button_mushroom", "蟹味菇": "buna_mushroom", "舞茸": "maitake",
    # 虾/黑虎虾/虾滑
    "虾": "shrimp", "黑虎虾": "black_tiger_shrimp", "虾滑": "shrimp_paste",
    # 鱼/银鳕鱼/青花鱼
    "鱼": "fish", "银鳕鱼": "cod", "青花鱼": "mackerel",
    # 葱/蒜/姜 (调味类)
    "葱": "scallion", "蒜": "garlic", "姜": "ginger",
    # 蓝莓/黑莓
    "蓝莓": "blueberry", "黑莓": "blackberry",
    # 橙子/橘子
    "橙子": "orange", "橘子": "tangerine",
}


def normalize_ingredient(name_cn):
    """将中文食材名归一化为 ingredient_id"""
    if name_cn in INGREDIENT_ALIASES:
        return INGREDIENT_ALIASES[name_cn]
    # 不在别名表中的，用拼音或直接用中文名生成 ID
    slug = name_cn.lower().strip()
    slug = slug.replace(" ", "_")
    return slug if slug else "unknown"


# ========== 食材中文名 → 英文名映射 ==========
INGREDIENT_EN_NAMES = {
    "sweet_potato": "Sweet Potato", "matsutake": "Fresh Matsutake",
    "rice": "Rice", "chicken": "Chicken", "chicken_leg": "Chicken Leg",
    "beef": "Beef", "beef_steak": "Beef Steak", "wagyu": "Wagyu",
    "tofu": "Tofu", "silken_tofu": "Silken Tofu",
    "mushroom": "Mushroom", "button_mushroom": "Button Mushroom",
    "buna_mushroom": "Buna Mushroom", "maitake": "Maitake",
    "shrimp": "Shrimp", "black_tiger_shrimp": "Black Tiger Shrimp",
    "shrimp_paste": "Shrimp Paste", "fish": "Fish", "cod": "Cod",
    "mackerel": "Mackerel", "scallion": "Scallion", "garlic": "Garlic",
    "ginger": "Ginger", "blueberry": "Blueberry", "blackberry": "Blackberry",
    "orange": "Orange", "tangerine": "Tangerine",
    "16谷米": "16-Grain Rice", "XO酱": "XO Sauce", "三文鱼籽": "Salmon Roe",
    "丝瓜": "Luffa", "云南小瓜": "Zucchini", "冬瓜": "Winter Melon",
    "南瓜": "Pumpkin", "味噌": "Miso", "土豆": "Potato",
    "奇异果": "Kiwi", "嫩豆腐": "Silken Tofu", "小白菜": "Bok Choy",
    "小米": "Millet", "巧克力": "Chocolate", "带子": "Scallop",
    "彩椒": "Bell Pepper", "排骨": "Pork Ribs", "时蔬": "Seasonal Vegetable",
    "枸杞叶": "Goji Leaves", "枸杞芽": "Goji Sprouts", "柠檬": "Lemon",
    "核桃": "Walnut", "水蜜桃": "Peach", "沙拉菜": "Salad Greens",
    "淮山": "Chinese Yam", "火腿": "Ham", "牛油果": "Avocado",
    "番茄": "Tomato", "白萝卜": "White Radish", "百合": "Lily Bulb",
    "皮蛋": "Century Egg", "秋葵": "Okra", "空心菜": "Water Spinach",
    "紫菜": "Seaweed", "红椒": "Red Pepper", "红苋菜": "Red Amaranth",
    "肉": "Meat", "肉丸": "Meatball", "肉末": "Minced Meat",
    "腐竹": "Tofu Skin", "芥蓝": "Chinese Broccoli", "芦笋": "Asparagus",
    "芹菜": "Celery", "苋菜": "Amaranth", "苹果": "Apple",
    "茄子": "Eggplant", "茴香": "Fennel", "莲子": "Lotus Seeds",
    "莲藕": "Lotus Root", "莴笋": "Celtuce", "面包": "Bread",
    "面条": "Noodles", "香蕉": "Banana", "鸡蛋": "Egg",
    "黄油": "Butter", "车厘子": "Cherries", "酸奶": "Yogurt",
    "黑鱼子酱": "Black Caviar", "蚕豆": "Broad Bean", "豆苗": "Pea Shoots",
    "蔬菜": "Vegetables", "藜麦": "Quinoa", "蛤蜊": "Clam",
    "西兰花": "Broccoli", "西葫芦": "Zucchini",
}


def get_ingredient_en(ingredient_id, name_cn=None):
    """获取食材英文名"""
    if ingredient_id in INGREDIENT_EN_NAMES:
        return INGREDIENT_EN_NAMES[ingredient_id]
    if name_cn and name_cn in INGREDIENT_EN_NAMES:
        return INGREDIENT_EN_NAMES[name_cn]
    return ingredient_id.replace("_", " ").title()


if __name__ == "__main__":
    init_db()
    print(f"数据库路径: {DB_PATH}")
