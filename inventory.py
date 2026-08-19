#!/usr/bin/env python3
"""
家庭菜单管家 - 持续库存模块 (V4: Current Pantry 增量维护)

核心原则：库存是持续存在的 Current Pantry，不是每天重新提交一份完整清单。
- 新增项 → INSERT
- 状态变化 → UPDATE
- 用户删除 → REMOVE (is_active = 0)
- 未操作旧项 → 保留不变

流程：保姆维护库存 → 老板点菜 → 缺货检测
"""

import json
import sqlite3
from contextlib import contextmanager
from contextvars import ContextVar
from datetime import date, datetime, timedelta
from db import get_db, log_event, get_config, set_config

# ============================================================
# V10: 食材别名归一化 (Canonical Ingredient ID Normalization)
# ============================================================

# 别名 → 规范 ingredient_id 映射
# 所有 availability 判断前统一归一化
INGREDIENT_ALIASES = {
    # Egg
    "egg": "鸡蛋",
    "Egg": "鸡蛋",
    "Chicken Egg": "鸡蛋",
    "蛋": "鸡蛋",
    "鸡蛋": "鸡蛋",
    # Tofu
    "tofu": "tofu",
    "Tofu": "tofu",
    "豆腐": "tofu",
    "silken_tofu": "silken_tofu",
    "Silken Tofu": "silken_tofu",
    "嫩豆腐": "silken_tofu",
    # Chicken
    "chicken": "chicken",
    "Chicken": "chicken",
    "鸡肉": "chicken",
    # Beef
    "beef": "beef",
    "Beef": "beef",
    "牛肉": "beef",
    # Shrimp
    "shrimp": "shrimp",
    "Shrimp": "shrimp",
    "虾": "shrimp",
    # Fish
    "fish": "fish",
    "Fish": "fish",
    "鱼": "fish",
    # Pork
    "pork": "猪肉",
    "Pork": "猪肉",
    "猪肉": "猪肉",
    # Rice
    "rice": "米饭",
    "Rice": "米饭",
    "米饭": "米饭",
    "白米": "米饭",
    # Household seasoning backend IDs and canonical_map aliases
    "scallion": "葱",
    "Scallion": "葱",
    "葱花": "葱",
    "ginger": "姜",
    "Ginger": "姜",
    "garlic": "蒜",
    "Garlic": "蒜",
    "蒜蓉": "蒜",
    "millet": "小米",
    "Millet": "小米",
    "小米": "小米",
    # Corn
    "corn": "corn",
    "Corn": "corn",
    "玉米": "corn",
    # Sweet potato
    "sweet_potato": "红薯",
    "Sweet Potato": "红薯",
    "红薯": "红薯",
    "番薯": "红薯",
    # Mushroom
    "mushroom": "mushroom",
    "Mushroom": "mushroom",
    "mushroom_generic": "mushroom",
    "菌菇": "mushroom",
    "蘑菇": "mushroom",
    # Chinese yam
    "yam": "yam",
    "Chinese Yam": "yam",
    "山药": "yam",
    "淮山": "yam",
    # Cabbage: strict classes must not merge baby cabbage with ordinary cabbage.
    "白菜": "white_cabbage",
    "娃娃菜": "baby_cabbage",
}

# UI SoT deploy/index.html DEFAULT_PANTRY (20) + REQUIREMENTS_V2 v2.1 millet.
# These are required recipe ingredients, but never need Current Pantry rows.
PANTRY_EXEMPT_SOURCE_NAMES = (
    "大米", "米", "米饭", "面粉", "水", "油", "食用油", "盐", "糖",
    "生抽", "老抽", "蚝油", "醋", "料酒", "葱", "姜", "蒜", "淀粉",
    "胡椒", "鸡精", "小米",
)

PLACEHOLDER_CLASS = {
    "any_available_vegetable": "vegetable",
    "any_available_fish": "fish",
    "any_available_grouper": "grouper",
    "any_available_mushroom": "mushroom",
    "any_available_protein": "protein",
}


_LEGACY_SCHEMA_SAFE = ContextVar("legacy_schema_safe_availability", default=False)


@contextmanager
def legacy_schema_safe_availability():
    """Allow only the caller's read path to tolerate the pre-V10 class table."""
    token = _LEGACY_SCHEMA_SAFE.set(True)
    try:
        yield
    finally:
        _LEGACY_SCHEMA_SAFE.reset(token)


def normalize_ingredient_id(raw_id):
    """V10: 将食材别名归一化为规范 ingredient_id。
    raw ingredient → normalize alias → canonical ingredient_id
    """
    if not raw_id:
        return raw_id
    return INGREDIENT_ALIASES.get(raw_id, raw_id)


PANTRY_EXEMPT_CANONICAL_IDS = frozenset(
    normalize_ingredient_id(name) for name in PANTRY_EXEMPT_SOURCE_NAMES
)


def _ingredient_classes(conn, ingredient_ids):
    """Return strict controlled classes for the requested ingredient IDs."""
    ids = {value for value in ingredient_ids if value}
    if not ids:
        return {}
    placeholders = ",".join("?" for _ in ids)
    try:
        rows = conn.execute(
            f"SELECT ingredient_id, class_id FROM ingredient_classifications "
            f"WHERE ingredient_id IN ({placeholders})",
            tuple(sorted(ids)),
        ).fetchall()
    except sqlite3.OperationalError as exc:
        if (_LEGACY_SCHEMA_SAFE.get()
                and str(exc) == "no such table: ingredient_classifications"):
            return None
        raise
    result = {}
    for row in rows:
        result.setdefault(row["ingredient_id"], set()).add(row["class_id"])
    return result


# ============================================================
# V5: inventory_version 追踪与 availability 缓存
# ============================================================

_availability_cache = {}  # key: "{location}_{version}_{dish_id}" → check_dish_availability result


def get_inventory_version(location):
    """V5: 获取指定 location 的库存版本号"""
    conn = get_db()
    try:
        row = conn.execute(
            "SELECT value FROM config WHERE key = ?", (f"inventory_version_{location}",)
        ).fetchone()
        return int(row["value"]) if row else 0
    finally:
        conn.close()


def _increment_inventory_version(conn, location):
    """V5: 递增库存版本号（在事务内调用）"""
    conn.execute(
        "INSERT INTO config (key, value) VALUES (?, '1') "
        "ON CONFLICT(key) DO UPDATE SET value = CAST(CAST(value AS INTEGER) + 1 AS TEXT)",
        (f"inventory_version_{location}",)
    )


def _invalidate_availability_cache(location):
    """V5: 清除指定 location 的所有 availability 缓存"""
    prefix = f"{location}_"
    keys_to_del = [k for k in _availability_cache if k.startswith(prefix)]
    for k in keys_to_del:
        del _availability_cache[k]


# ============================================================
# V4: Current Pantry 增量维护
# ============================================================

def save_pantry_changes(location, items, submitted_by="nanny"):
    """
    V4: 保存库存变更（增量模式）。
    items: list of {ingredient_id, status, quantity_level}
    - items 中的项 → UPSERT (新增或更新状态)
    - current_pantry 中有但 items 中没有的 → 标记 is_active = 0 (用户已删除)
    返回: {pantry_count, added, updated, removed, snapshot_id}
    """
    conn = get_db()
    try:
        now = datetime.now().isoformat()
        submitted_ids = set()

        # 获取当前活跃库存
        current_rows = conn.execute(
            "SELECT ingredient_id, status, COALESCE(quantity_level, 'enough') AS quantity_level FROM current_pantry "
            "WHERE location = ? AND is_active = 1",
            (location,)
        ).fetchall()
        current_map = {
            r["ingredient_id"]: (r["status"], r["quantity_level"] or "enough")
            for r in current_rows
        }

        added = 0
        updated = 0

        for item in items:
            ing_id = item["ingredient_id"]
            status = item.get("status", "available")
            quantity_level = item.get("quantity_level", "enough")
            if quantity_level not in ("enough", "low"):
                quantity_level = "enough"
            submitted_ids.add(ing_id)

            if ing_id in current_map:
                if current_map[ing_id] != (status, quantity_level):
                    # 状态变化 → UPDATE
                    conn.execute(
                        "UPDATE current_pantry SET status = ?, quantity_level = ?, updated_at = ?, is_active = 1 "
                        "WHERE location = ? AND ingredient_id = ?",
                        (status, quantity_level, now, location, ing_id)
                    )
                    updated += 1
                # else: 未变化，不操作
            else:
                # 新增 → INSERT
                conn.execute(
                    "INSERT INTO current_pantry (location, ingredient_id, status, quantity_level, is_active, created_at, updated_at) "
                    "VALUES (?, ?, ?, ?, 1, ?, ?) "
                    "ON CONFLICT(location, ingredient_id) DO UPDATE SET "
                    "status = excluded.status, quantity_level = excluded.quantity_level, "
                    "is_active = 1, updated_at = excluded.updated_at",
                    (location, ing_id, status, quantity_level, now, now)
                )
                added += 1

        # 用户删除的项 → is_active = 0
        removed_ids = set(current_map.keys()) - submitted_ids
        removed = 0
        for ing_id in removed_ids:
            conn.execute(
                "UPDATE current_pantry SET is_active = 0, updated_at = ? "
                "WHERE location = ? AND ingredient_id = ?",
                (now, location, ing_id)
            )
            removed += 1

        conn.commit()

        # 生成快照
        snapshot_id = _create_snapshot(conn, location)

        # 同步写入旧 inventory 表（兼容）
        _sync_to_legacy_inventory(conn, location, items, submitted_by)

        conn.commit()

        # V5: 递增 inventory_version
        _increment_inventory_version(conn, location)

        # 历史返回字段保持兼容；采购请示运行链已退出。
        auto_purchased = 0

        conn.commit()

        # V5 Section 7: 清除 availability 缓存（库存已变化，旧结果失效）
        _invalidate_availability_cache(location)

        pantry_count = conn.execute(
            "SELECT COUNT(*) as cnt FROM current_pantry WHERE location = ? AND is_active = 1",
            (location,)
        ).fetchone()["cnt"]

        log_event("pantry_changes_saved", "current_pantry", None, {
            "location": location, "added": added, "updated": updated,
            "removed": removed, "pantry_count": pantry_count,
            "auto_purchased": auto_purchased, "submitted_by": submitted_by
        })

        return {
            "pantry_count": pantry_count,
            "added": added,
            "updated": updated,
            "removed": removed,
            "auto_purchased": auto_purchased,
            "snapshot_id": snapshot_id,
        }
    finally:
        conn.close()


def _create_snapshot(conn, location):
    """生成当前库存快照"""
    items = conn.execute(
        "SELECT ingredient_id, status, COALESCE(quantity_level, 'enough') AS quantity_level FROM current_pantry "
        "WHERE location = ? AND is_active = 1",
        (location,)
    ).fetchall()
    items_json = json.dumps([dict(r) for r in items], ensure_ascii=False)
    cur = conn.execute(
        "INSERT INTO inventory_snapshots (location, items_json, created_at) VALUES (?, ?, ?)",
        (location, items_json, datetime.now().isoformat())
    )
    return cur.lastrowid


def _sync_to_legacy_inventory(conn, location, items, submitted_by):
    """同步写入旧 inventory 表（向后兼容）"""
    today = date.today().isoformat()
    now = datetime.now().isoformat()
    conn.execute(
        "INSERT INTO inventory (location, date, submitted_by, submitted_at, status) "
        "VALUES (?, ?, ?, ?, 'submitted') "
        "ON CONFLICT(location, date) DO UPDATE SET "
        "submitted_by=excluded.submitted_by, submitted_at=excluded.submitted_at, status='submitted'",
        (location, today, submitted_by, now)
    )
    row = conn.execute(
        "SELECT id FROM inventory WHERE location = ? AND date = ?",
        (location, today)
    ).fetchone()
    if row:
        inv_id = row["id"]
        conn.execute("DELETE FROM inventory_items WHERE inventory_id = ?", (inv_id,))
        for item in items:
            conn.execute(
                "INSERT INTO inventory_items (inventory_id, ingredient_id, status, quantity_level) VALUES (?, ?, ?, ?)",
                (inv_id, item["ingredient_id"], item.get("status", "available"),
                 item.get("quantity_level", "enough"))
            )


def get_current_pantry(location):
    """
    V4: 获取当前持续库存。
    返回: {location, items: [{ingredient_id, name_cn, name_en, status}], count}
    """
    conn = get_db()
    try:
        rows = conn.execute(
            "SELECT cp.ingredient_id, cp.status, COALESCE(cp.quantity_level, 'enough') AS quantity_level, "
            "i.name_cn, i.name_en "
            "FROM current_pantry cp "
            "JOIN ingredients i ON cp.ingredient_id = i.ingredient_id "
            "WHERE cp.location = ? AND cp.is_active = 1 "
            "ORDER BY i.name_cn",
            (location,)
        ).fetchall()
        return {
            "location": location,
            "items": [dict(r) for r in rows],
            "count": len(rows),
        }
    finally:
        conn.close()


def get_current_pantry_ids(location):
    """V4: 获取当前可用食材ID集合（available + priority_use + expiring）"""
    conn = get_db()
    try:
        rows = conn.execute(
            "SELECT ingredient_id, status FROM current_pantry "
            "WHERE location = ? AND is_active = 1",
            (location,)
        ).fetchall()
        available = set()
        priority = set()
        expiring = set()
        for r in rows:
            if r["status"] in ("available", "priority_use", "expiring"):
                available.add(r["ingredient_id"])
            if r["status"] == "priority_use":
                priority.add(r["ingredient_id"])
            if r["status"] == "expiring":
                expiring.add(r["ingredient_id"])
        return available, priority, expiring
    finally:
        conn.close()


# ============================================================
# V6: Pantry 增量操作（面向保姆简化）
# ============================================================

def add_ingredient_to_pantry(location, ingredient_id, status="available", quantity_level="enough",
                             submitted_by="nanny"):
    """
    V6: 向当前库存添加单项食材（增量，不影响其他食材）。
    如果已存在则更新状态，不重复插入。
    """
    conn = get_db()
    try:
        if quantity_level not in ("enough", "low"):
            quantity_level = "enough"
        now = datetime.now().isoformat()
        conn.execute(
            "INSERT INTO current_pantry (location, ingredient_id, status, quantity_level, is_active, created_at, updated_at) "
            "VALUES (?, ?, ?, ?, 1, ?, ?) "
            "ON CONFLICT(location, ingredient_id) DO UPDATE SET "
            "status = excluded.status, quantity_level = excluded.quantity_level, "
            "is_active = 1, updated_at = excluded.updated_at",
            (location, ingredient_id, status, quantity_level, now, now)
        )
        conn.commit()

        # V5: 递增版本 + 清缓存
        _increment_inventory_version(conn, location)
        _invalidate_availability_cache(location)

        snapshot_id = _create_snapshot(conn, location)
        conn.commit()

        log_event("pantry_item_added", "current_pantry", ingredient_id, {
            "location": location, "ingredient_id": ingredient_id,
            "status": status, "quantity_level": quantity_level, "submitted_by": submitted_by
        })
        return {"ok": True, "ingredient_id": ingredient_id}
    finally:
        conn.close()


def remove_ingredient_from_pantry(location, ingredient_id, submitted_by="nanny"):
    """
    V6: 从当前库存移除单项食材（soft delete: is_active=0）。
    """
    conn = get_db()
    try:
        conn.execute(
            "UPDATE current_pantry SET is_active = 0, updated_at = datetime('now') "
            "WHERE location = ? AND ingredient_id = ?",
            (location, ingredient_id)
        )
        conn.commit()

        _increment_inventory_version(conn, location)
        _invalidate_availability_cache(location)

        snapshot_id = _create_snapshot(conn, location)
        conn.commit()

        log_event("pantry_item_removed", "current_pantry", ingredient_id, {
            "location": location, "ingredient_id": ingredient_id,
            "submitted_by": submitted_by
        })
        return {"ok": True}
    finally:
        conn.close()


def update_ingredient_status(location, ingredient_id, status, submitted_by="nanny"):
    """
    V6: 更新单项食材状态（即时保存）。
    """
    conn = get_db()
    try:
        conn.execute(
            "UPDATE current_pantry SET status = ?, updated_at = datetime('now') "
            "WHERE location = ? AND ingredient_id = ? AND is_active = 1",
            (status, location, ingredient_id)
        )
        conn.commit()

        _increment_inventory_version(conn, location)
        _invalidate_availability_cache(location)
        conn.commit()

        log_event("pantry_status_updated", "current_pantry", ingredient_id, {
            "location": location, "ingredient_id": ingredient_id,
            "status": status, "submitted_by": submitted_by
        })
        return {"ok": True}
    finally:
        conn.close()


def confirm_pantry_unchanged(location, submitted_by="nanny"):
    """
    V6: "和上次一样 Same as Last Update"
    不修改 Current Pantry 内容，只更新 last_confirmed_at + 生成快照。
    """
    conn = get_db()
    try:
        now = datetime.now().isoformat()

        # 记录确认时间（使用当前连接，不另开连接）
        conn.execute(
            "INSERT INTO config (key, value) VALUES (?, ?) "
            "ON CONFLICT(key) DO UPDATE SET value = ?",
            (f"pantry_last_confirmed_{location}", now, now)
        )
        conn.commit()

        # 生成快照（内容不变）
        snapshot_id = _create_snapshot(conn, location)
        conn.commit()

        log_event("pantry_confirmed_unchanged", "current_pantry", None, {
            "location": location, "submitted_by": submitted_by,
            "snapshot_id": snapshot_id
        })
        return {"ok": True, "confirmed_at": now, "snapshot_id": snapshot_id}
    finally:
        conn.close()


def is_ingredient_in_pantry(location, ingredient_id):
    """V6: 检查食材是否已在当前库存中"""
    conn = get_db()
    try:
        row = conn.execute(
            "SELECT 1 FROM current_pantry "
            "WHERE location = ? AND ingredient_id = ? AND is_active = 1",
            (location, ingredient_id)
        ).fetchone()
        return row is not None
    finally:
        conn.close()


def check_dish_availability(dish_id, location, inventory_version=None):
    """
    V5 Section 12-18: 统一菜品可用性检查服务（InventoryService）。
    所有模块（Dishes / Tomorrow / Purchase Request / Add Dish picker / AI scoring）必须调用此方法。
    返回: {status, required, available_required, missing_required, optional, inventory_version}
    status: available / almost_available / missing / incomplete

    生产止血：required_ingredients 为空时不能证明库存可做，返回 "incomplete"。
    这类菜品仍可手动选择，但不会进入 Available Now 或 AI Fill 自动候选。
    """
    if inventory_version is None:
        inventory_version = get_inventory_version(location)

    legacy_schema_safe = _LEGACY_SCHEMA_SAFE.get()

    # Legacy-safe reads are deliberately not cached: a bootstrap fallback must
    # never weaken a later strict availability call in the same process.
    cache_key = f"{location}_{inventory_version}_{dish_id}"
    if not legacy_schema_safe and cache_key in _availability_cache:
        return _availability_cache[cache_key]

    conn = get_db()
    try:
        ings = conn.execute(
            "SELECT di.ingredient_id, di.required, i.name_cn, i.name_en "
            "FROM dish_ingredients di "
            "JOIN ingredients i ON di.ingredient_id = i.ingredient_id "
            "WHERE di.dish_id = ?",
            (dish_id,)
        ).fetchall()

        # 没有必需食材资料时，不能把“未知”误判成“库存可做”。
        required_ings = [r for r in ings if r["required"]]
        if not required_ings:
            result = {
                "status": "incomplete",
                "required": [],
                "available_required": [],
                "missing_required": [],
                "optional": [dict(r) for r in ings if not r["required"]],
                "inventory_version": inventory_version,
                "data_complete": False,
            }
            if not legacy_schema_safe:
                _availability_cache[cache_key] = result
            return result

        available_ings, _, _ = get_current_pantry_ids(location)

        # V10: 归一化库存食材 ID（alias → canonical）
        normalized_pantry = set()
        for pid in available_ings:
            normalized_pantry.add(normalize_ingredient_id(pid))

        class_ids = set(available_ings) | normalized_pantry | {
            normalize_ingredient_id(row["ingredient_id"]) for row in ings
        } | {row["ingredient_id"] for row in ings}
        class_map = _ingredient_classes(conn, class_ids)
        class_mapping_known = class_map is not None
        if class_map is None:
            class_map = {}
        available_classes = set()
        for pantry_id in available_ings | normalized_pantry:
            available_classes.update(class_map.get(pantry_id, set()))

        required = []
        available_required = []
        missing_required = []
        unknown_required = []
        optional = []
        seen_required_ids = set()

        for ing in ings:
            # V10: 归一化菜品食材 ID
            norm_id = normalize_ingredient_id(ing["ingredient_id"])
            ing_data = {"ingredient_id": ing["ingredient_id"],
                        "name_cn": ing["name_cn"],
                        "name_en": ing["name_en"] if ing["name_en"] else ""}
            if ing["required"]:
                # Synonymous required rows represent one canonical ingredient.
                if norm_id in seen_required_ids:
                    continue
                seen_required_ids.add(norm_id)
                required.append(ing_data)
                placeholder_class = PLACEHOLDER_CLASS.get(norm_id)
                required_classes = (
                    class_map.get(ing["ingredient_id"], set())
                    | class_map.get(norm_id, set())
                )
                if placeholder_class:
                    if not class_mapping_known:
                        unknown_required.append(ing_data)
                    elif placeholder_class in available_classes:
                        available_required.append(ing_data)
                    else:
                        missing_required.append(ing_data)
                elif (norm_id in PANTRY_EXEMPT_CANONICAL_IDS
                        or norm_id in normalized_pantry
                        or ing["ingredient_id"] in available_ings):
                    available_required.append(ing_data)
                elif not class_mapping_known:
                    unknown_required.append(ing_data)
                else:
                    missing_required.append(ing_data)
            else:
                optional.append(ing_data)

        # V6 Section 27: 4 种状态判定（重新定义）
        required_count = len(required)
        missing_count = len(missing_required)

        if unknown_required:
            status = "incomplete"
        elif missing_count == 0:
            status = "available"
        elif required_count >= 2 and missing_count == 1:
            status = "almost_available"
        else:
            status = "missing"

        result = {
            "status": status,
            "required": required,
            "available_required": available_required,
            "missing_required": missing_required,
            "optional": optional,
            "inventory_version": inventory_version,
            "data_complete": not unknown_required,
        }
        if not class_mapping_known:
            result["unknown_required"] = unknown_required
        if not legacy_schema_safe:
            _availability_cache[cache_key] = result
        return result
    finally:
        conn.close()


def check_dishes_availability_batch(dish_ids, location):
    """V4: 批量检查菜品可用性。返回 {dish_id: check_dish_availability_result}"""
    inventory_version = get_inventory_version(location)
    result = {}
    for did in dish_ids:
        if did and did.startswith("dish_"):
            result[did] = check_dish_availability(did, location, inventory_version=inventory_version)
    return result


def check_dish_availability_debug(dish_id, location):
    """
    V5 Section 22: Availability Debug API。
    返回完整的可用性调试信息。
    """
    avail = check_dish_availability(dish_id, location)
    conn = get_db()
    try:
        dish = conn.execute("SELECT name_cn, name_en FROM dishes WHERE id = ?", (dish_id,)).fetchone()
    finally:
        conn.close()

    return {
        "dish_id": dish_id,
        "dish": dish["name_cn"] if dish else dish_id,
        "dish_en": dish["name_en"] if dish else "",
        "location": location,
        "inventory_version": avail.get("inventory_version", 0),
        "required": [{"ingredient_id": r["ingredient_id"], "name_cn": r["name_cn"], "name_en": r.get("name_en", "")}
                     for r in avail["required"]],
        "in_stock": [{"ingredient_id": r["ingredient_id"], "name_cn": r["name_cn"], "name_en": r.get("name_en", "")}
                     for r in avail["available_required"]],
        "missing": [{"ingredient_id": r["ingredient_id"], "name_cn": r["name_cn"], "name_en": r.get("name_en", "")}
                    for r in avail["missing_required"]],
        "optional": [{"ingredient_id": r["ingredient_id"], "name_cn": r["name_cn"], "name_en": r.get("name_en", "")}
                     for r in avail["optional"]],
        "status": avail["status"],
    }


def get_common_ingredients_static():
    """V4 Section 13-15: 获取常用食材（is_common 字段，独立于当前库存）"""
    conn = get_db()
    try:
        rows = conn.execute(
            "SELECT ingredient_id, name_cn, name_en FROM ingredients "
            "WHERE is_common = 1 ORDER BY name_cn"
        ).fetchall()
        return [dict(r) for r in rows]
    finally:
        conn.close()


# ============================================================
# 旧接口兼容（内部改为读 current_pantry）
# ============================================================

def submit_inventory(location, inv_date, items, submitted_by="nanny", notes=None, replace=False):
    """
    兼容旧接口。V4 默认 replace=False（增量模式）。
    内部调用 save_pantry_changes() 同步 current_pantry。
    """
    # 同步到 current_pantry（增量）
    save_pantry_changes(location, items, submitted_by=submitted_by)

    # 同时写入旧 inventory 表（快照兼容）
    conn = get_db()
    try:
        conn.execute(
            "INSERT INTO inventory (location, date, submitted_by, submitted_at, status, notes) "
            "VALUES (?, ?, ?, ?, 'submitted', ?) "
            "ON CONFLICT(location, date) DO UPDATE SET "
            "submitted_by=excluded.submitted_by, submitted_at=excluded.submitted_at, "
            "status='submitted', notes=excluded.notes",
            (location, inv_date, submitted_by, datetime.now().isoformat(), notes)
        )
        conn.commit()
        row = conn.execute(
            "SELECT id FROM inventory WHERE location = ? AND date = ?",
            (location, inv_date)
        ).fetchone()
        inv_id = row["id"] if row else 0

        if replace:
            conn.execute("DELETE FROM inventory_items WHERE inventory_id = ?", (inv_id,))
        for item in items:
            conn.execute(
                "INSERT INTO inventory_items (inventory_id, ingredient_id, status, notes, quantity_level) "
                "VALUES (?, ?, ?, ?, ?)",
                (inv_id, item["ingredient_id"], item.get("status", "available"), item.get("notes"),
                 item.get("quantity_level", "enough"))
            )
        conn.commit()

        log_event("inventory_submitted", "inventory", str(inv_id), {
            "location": location, "date": inv_date, "items_count": len(items),
            "submitted_by": submitted_by, "replace": replace
        })
        return inv_id
    finally:
        conn.close()


def get_latest_inventory(location, before_date=None):
    """
    V4: 获取当前库存（从 current_pantry 读取）。
    before_date 参数保留兼容但不再使用（Current Pantry 是持续的）。
    返回: {inventory_id, date, location, items: [{ingredient_id, status, name_cn, name_en}]}
    """
    conn = get_db()
    try:
        rows = conn.execute(
            "SELECT cp.ingredient_id, cp.status, COALESCE(cp.quantity_level, 'enough') AS quantity_level, "
            "i.name_cn, i.name_en "
            "FROM current_pantry cp "
            "JOIN ingredients i ON cp.ingredient_id = i.ingredient_id "
            "WHERE cp.location = ? AND cp.is_active = 1 "
            "ORDER BY i.name_cn",
            (location,)
        ).fetchall()

        if not rows:
            return None

        return {
            "inventory_id": 0,
            "date": date.today().isoformat(),
            "location": location,
            "items": [dict(r) for r in rows]
        }
    finally:
        conn.close()


def get_available_ingredient_ids(location, before_date=None):
    """V4: 获取可用食材ID集合（从 current_pantry 读取）"""
    return get_current_pantry_ids(location)


# ============================================================
# 缺货检测
# ============================================================

def check_shortages(dish_ids, location, target_date=None):
    """
    检查指定菜品列表是否有缺货食材。
    返回: list of {dish_id, dish_name, ingredient_id, ingredient_name, missing: True}
    """
    if not dish_ids:
        return []

    conn = get_db()
    try:
        names = {
            row["id"]: row["name_cn"]
            for row in conn.execute(
                f"SELECT id, name_cn FROM dishes WHERE id IN ({','.join('?' for _ in dish_ids)})",
                dish_ids,
            ).fetchall()
        }
    finally:
        conn.close()

    availability = check_dishes_availability_batch(dish_ids, location)
    shortages = []
    for dish_id in dish_ids:
        for missing in availability.get(dish_id, {}).get("missing_required", []):
            shortages.append({
                "dish_id": dish_id,
                "dish_name": names.get(dish_id, dish_id),
                "ingredient_id": missing["ingredient_id"],
                "ingredient_name": missing["name_cn"],
                "missing": True,
            })
    return shortages


def check_menu_shortages(menu_id, location):
    """检查某天菜单的缺货情况"""
    conn = get_db()
    try:
        menu = conn.execute("SELECT date FROM menus WHERE id = ?", (menu_id,)).fetchone()
        if not menu:
            return []

        items = conn.execute(
            "SELECT dish_id FROM menu_items WHERE menu_id = ?", (menu_id,)
        ).fetchall()

        dish_ids = [r["dish_id"] for r in items if r["dish_id"].startswith("dish_")]
        return check_shortages(dish_ids, location, menu["date"])
    finally:
        conn.close()


# ============================================================
# CLI 测试（仅库存/缺货读写）
# ============================================================

if __name__ == "__main__":
    import argparse

    parser = argparse.ArgumentParser(description="库存管理")
    sub = parser.add_subparsers(dest="command")

    # 提交库存
    p_submit = sub.add_parser("submit", help="提交库存")
    p_submit.add_argument("--location", default="shenzhen")
    p_submit.add_argument("--date", default=date.today().isoformat())
    p_submit.add_argument("--items", type=str, help="JSON格式的食材列表")

    # 查看库存
    p_view = sub.add_parser("view", help="查看最新库存")
    p_view.add_argument("--location", default="shenzhen")

    # 检查缺货
    p_check = sub.add_parser("check", help="检查菜品缺货")
    p_check.add_argument("--dishes", type=str, required=True, help="菜品ID逗号分隔")
    p_check.add_argument("--location", default="shenzhen")

    args = parser.parse_args()

    if args.command == "submit":
        items = json.loads(args.items) if args.items else []
        inv_id = submit_inventory(args.location, args.date, items)
        print(f"[OK] 库存已提交，ID: {inv_id}，{len(items)} 项食材")

    elif args.command == "view":
        inv = get_latest_inventory(args.location)
        if inv:
            print(f"库存ID: {inv['inventory_id']} | 日期: {inv['date']} | 地点: {inv['location']}")
            print(f"食材 {len(inv['items'])} 项:")
            for item in inv["items"]:
                print(f"  {item['name_cn']} ({item['ingredient_id']}): {item['status']}")
        else:
            print("无库存记录")

    elif args.command == "check":
        dish_ids = args.dishes.split(",")
        shortages = check_shortages(dish_ids, args.location)
        if shortages:
            print(f"发现 {len(shortages)} 项缺货:")
            for s in shortages:
                print(f"  {s['dish_name']} → 缺 {s['ingredient_name']}")
        else:
            print("无缺货")

    else:
        parser.print_help()
