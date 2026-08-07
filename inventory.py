#!/usr/bin/env python3
"""
家庭菜单管家 - 库存与采购闭环模块 (V4: Current Pantry 增量维护)

核心原则：库存是持续存在的 Current Pantry，不是每天重新提交一份完整清单。
- 新增项 → INSERT
- 状态变化 → UPDATE
- 用户删除 → REMOVE (is_active = 0)
- 未操作旧项 → 保留不变

流程：保姆维护库存 → 老板点菜 → 缺货检测 → 采购任务 → PushPlus通知 → 采购完成
"""

import os
import json
import re
import unicodedata
from datetime import date, datetime, timedelta
from db import get_db, log_event, get_config, set_config
from push_service import PushPlusClient, PushError

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

def _record_pantry_usage(conn, location, ingredient_id, action_at, added):
    """Persist one real pantry action, independently for each kitchen."""
    conn.execute(
        "INSERT INTO pantry_usage_stats(location,ingredient_id,add_count,last_action_at) "
        "VALUES(?,?,?,?) ON CONFLICT(location,ingredient_id) DO UPDATE SET "
        "add_count=pantry_usage_stats.add_count+excluded.add_count, "
        "last_action_at=excluded.last_action_at",
        (location, ingredient_id, 1 if added else 0, action_at),
    )

def save_pantry_changes(location, items, submitted_by="nanny"):
    """
    V4: 保存库存变更（增量模式）。
    items: list of {ingredient_id, status}
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
            "SELECT ingredient_id, status FROM current_pantry "
            "WHERE location = ? AND is_active = 1",
            (location,)
        ).fetchall()
        current_map = {r["ingredient_id"]: r["status"] for r in current_rows}

        added = 0
        updated = 0

        for item in items:
            ing_id = item["ingredient_id"]
            status = item.get("status", "available")
            submitted_ids.add(ing_id)

            if ing_id in current_map:
                if current_map[ing_id] != status:
                    # 状态变化 → UPDATE
                    conn.execute(
                        "UPDATE current_pantry SET status = ?, updated_at = ?, is_active = 1 "
                        "WHERE location = ? AND ingredient_id = ?",
                        (status, now, location, ing_id)
                    )
                    updated += 1
                    _record_pantry_usage(conn, location, ing_id, now, added=False)
                # else: 未变化，不操作
            else:
                # 新增 → INSERT
                conn.execute(
                    "INSERT INTO current_pantry (location, ingredient_id, status, is_active, created_at, updated_at) "
                    "VALUES (?, ?, ?, 1, ?, ?) "
                    "ON CONFLICT(location, ingredient_id) DO UPDATE SET "
                    "status = excluded.status, is_active = 1, updated_at = excluded.updated_at",
                    (location, ing_id, status, now, now)
                )
                added += 1
                _record_pantry_usage(conn, location, ing_id, now, added=True)

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
            _record_pantry_usage(conn, location, ing_id, now, added=False)

        conn.commit()

        # 生成快照
        snapshot_id = _create_snapshot(conn, location)

        # 同步写入旧 inventory 表（兼容）
        _sync_to_legacy_inventory(conn, location, items, submitted_by)

        conn.commit()

        # V5: 递增 inventory_version
        _increment_inventory_version(conn, location)

        # Purchase Request 联动：自动标记已购买的
        auto_purchased = _auto_mark_purchased(conn, location, submitted_ids)

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
        "SELECT ingredient_id, status FROM current_pantry "
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
                "INSERT INTO inventory_items (inventory_id, ingredient_id, status) VALUES (?, ?, ?)",
                (inv_id, item["ingredient_id"], item.get("status", "available"))
            )


def _auto_mark_purchased(conn, location, available_ingredient_ids):
    """V4 Section 35: 新增库存后自动标记采购任务为已购买"""
    if not available_ingredient_ids:
        return 0
    placeholders = ",".join("?" * len(available_ingredient_ids))
    params = list(available_ingredient_ids) + [location]
    rows = conn.execute(
        f"SELECT id FROM purchase_requests "
        f"WHERE ingredient_id IN ({placeholders}) AND location = ? "
        f"AND status IN ('needed', 'notified')",
        params
    ).fetchall()
    now = datetime.now().isoformat()
    for r in rows:
        conn.execute(
            "UPDATE purchase_requests SET status = 'purchased', resolved_at = ?, resolved_by = 'system_auto' WHERE id = ?",
            (now, r["id"])
        )
    return len(rows)


def get_current_pantry(location):
    """
    V4: 获取当前持续库存。
    返回: {location, items: [{ingredient_id, name_cn, name_en, status}], count}
    """
    conn = get_db()
    try:
        rows = conn.execute(
            "SELECT cp.ingredient_id, cp.status, i.name_cn, i.name_en "
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

def add_ingredient_to_pantry(location, ingredient_id, status="available", submitted_by="nanny"):
    """
    V6: 向当前库存添加单项食材（增量，不影响其他食材）。
    如果已存在则更新状态，不重复插入。
    """
    conn = get_db()
    try:
        now = datetime.now().isoformat()
        was_active = conn.execute(
            "SELECT 1 FROM current_pantry WHERE location=? AND ingredient_id=? AND is_active=1",
            (location, ingredient_id),
        ).fetchone()
        conn.execute(
            "INSERT INTO current_pantry (location, ingredient_id, status, is_active, created_at, updated_at) "
            "VALUES (?, ?, ?, 1, ?, ?) "
            "ON CONFLICT(location, ingredient_id) DO UPDATE SET "
            "status = excluded.status, is_active = 1, updated_at = excluded.updated_at",
            (location, ingredient_id, status, now, now)
        )
        _record_pantry_usage(conn, location, ingredient_id, now, added=not was_active)
        conn.commit()

        # V5: 递增版本 + 清缓存
        _increment_inventory_version(conn, location)
        _invalidate_availability_cache(location)

        snapshot_id = _create_snapshot(conn, location)
        conn.commit()

        log_event("pantry_item_added", "current_pantry", ingredient_id, {
            "location": location, "ingredient_id": ingredient_id,
            "status": status, "submitted_by": submitted_by
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
        now = datetime.now().isoformat()
        conn.execute(
            "UPDATE current_pantry SET is_active = 0, updated_at = ? "
            "WHERE location = ? AND ingredient_id = ?",
            (now, location, ingredient_id)
        )
        _record_pantry_usage(conn, location, ingredient_id, now, added=False)
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
        now = datetime.now().isoformat()
        cursor = conn.execute(
            "UPDATE current_pantry SET status = ?, updated_at = ? "
            "WHERE location = ? AND ingredient_id = ? AND is_active = 1",
            (status, now, location, ingredient_id)
        )
        if cursor.rowcount != 1:
            conn.rollback()
            return {"ok": False, "error": "ingredient not in pantry"}
        _record_pantry_usage(conn, location, ingredient_id, now, added=False)
        _increment_inventory_version(conn, location)
        counts = conn.execute(
            "SELECT COUNT(*) AS pantry_count, "
            "SUM(CASE WHEN status='expiring' THEN 1 ELSE 0 END) AS expiring_count "
            "FROM current_pantry WHERE location=? AND is_active=1",
            (location,),
        ).fetchone()
        conn.execute(
            "INSERT INTO events (event_type,entity_type,entity_id,details) VALUES (?,?,?,?)",
            (
                "pantry_status_updated", "current_pantry", ingredient_id,
                json.dumps({
                    "location": location, "ingredient_id": ingredient_id,
                    "status": status, "submitted_by": submitted_by,
                }, ensure_ascii=False),
            ),
        )
        conn.commit()
        _invalidate_availability_cache(location)
        return {
            "ok": True, "ingredient_id": ingredient_id, "status": status,
            "pantry_count": counts["pantry_count"],
            "expiring_count": counts["expiring_count"] or 0,
        }
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


_ACTIVE_PANTRY_STATUSES = {"available", "priority_use", "expiring"}


def _normalize_exact_ingredient_value(value):
    """Normalize for full-value equality only; never perform substring/fuzzy matching."""
    if value is None:
        return ""
    normalized = unicodedata.normalize("NFKC", str(value)).strip().casefold()
    return re.sub(r"\s+", " ", normalized)


def _parse_alias_values(raw_aliases):
    try:
        values = json.loads(raw_aliases or "[]")
    except (TypeError, ValueError):
        values = []
    if not isinstance(values, list):
        return set()
    return {_normalize_exact_ingredient_value(value) for value in values if value}


def _ingredient_match(required, pantry):
    """Return an explicit match method or None using ID, declared aliases, then exact names."""
    if required["ingredient_id"] == pantry["ingredient_id"]:
        return "ingredient_id"

    required_id = _normalize_exact_ingredient_value(required["ingredient_id"])
    pantry_id = _normalize_exact_ingredient_value(pantry["ingredient_id"])
    required_names = {
        _normalize_exact_ingredient_value(required.get("name_cn")),
        _normalize_exact_ingredient_value(required.get("name_en")),
    } - {""}
    pantry_names = {
        _normalize_exact_ingredient_value(pantry.get("name_cn")),
        _normalize_exact_ingredient_value(pantry.get("name_en")),
    } - {""}
    required_aliases = required.get("alias_values", set())
    pantry_aliases = pantry.get("alias_values", set())

    required_terms = required_names | {required_id}
    pantry_terms = pantry_names | {pantry_id}
    if (required_aliases & pantry_terms) or (pantry_aliases & required_terms) or (required_aliases & pantry_aliases):
        return "explicit_alias"
    if required_names & pantry_names:
        return "exact_name"
    return None


_DEFAULT_STAPLE_RICE_TERMS = {"rice", "米", "米饭", "白米"}


def _is_default_staple_rice(dish_row, ingredient):
    """Treat only the basic rice requirement of a rice staple as always on hand."""
    if not dish_row:
        return False
    if (dish_row["category_id"] != "staple_carb"
            or dish_row["carb_type"] not in ("rice", "coarse_grain")):
        return False
    ingredient_terms = {
        _normalize_exact_ingredient_value(ingredient.get("ingredient_id")),
        _normalize_exact_ingredient_value(ingredient.get("name_cn")),
        _normalize_exact_ingredient_value(ingredient.get("name_en")),
    } - {""}
    ingredient_terms.update(_parse_alias_values(ingredient.get("aliases")))
    default_terms = {
        _normalize_exact_ingredient_value(value)
        for value in _DEFAULT_STAPLE_RICE_TERMS
    }
    return bool(ingredient_terms & default_terms)


def check_dish_availability(dish_id, location, inventory_version=None):
    """
    V5 Section 12-18: 统一菜品可用性检查服务（InventoryService）。
    所有模块（Dishes / Tomorrow / Purchase Request / Add Dish picker / AI scoring）必须调用此方法。
    返回: {status, required, available_required, missing_required, optional, inventory_version}
    status: available / almost_available / missing / incomplete
    A dish is available only when every required ingredient matches the active pantry
    by exact ingredient_id, an explicit alias, or an exact normalized full name.
    """
    if inventory_version is None:
        inventory_version = get_inventory_version(location)

    catalog_version = get_config("catalog_version", "1")
    cache_key = f"{location}_{inventory_version}_{catalog_version}_{dish_id}"
    if cache_key in _availability_cache:
        return _availability_cache[cache_key]

    conn = get_db()
    try:
        dish_row = conn.execute(
            "SELECT category_id,meal_tags,is_active,image,carb_type FROM dishes WHERE id=?", (dish_id,)
        ).fetchone()
        ings = conn.execute(
            "SELECT di.ingredient_id, di.required, i.name_cn, i.name_en, i.aliases "
            "FROM dish_ingredients di "
            "LEFT JOIN ingredients i ON di.ingredient_id = i.ingredient_id "
            "WHERE di.dish_id = ?",
            (dish_id,)
        ).fetchall()

        pantry_rows = conn.execute(
            "SELECT cp.ingredient_id, cp.status, i.name_cn, i.name_en, i.aliases "
            "FROM current_pantry cp JOIN ingredients i ON i.ingredient_id=cp.ingredient_id "
            "WHERE cp.location=? AND cp.is_active=1",
            (location,),
        ).fetchall()
        active_pantry = []
        for row in pantry_rows:
            if row["status"] not in _ACTIVE_PANTRY_STATUSES:
                continue
            item = dict(row)
            item["alias_values"] = _parse_alias_values(item.get("aliases"))
            active_pantry.append(item)

        required = []
        available_required = []
        missing_required = []
        optional = []

        for ing in ings:
            ing_data = {"ingredient_id": ing["ingredient_id"],
                        "name_cn": ing["name_cn"] or ing["ingredient_id"],
                        "name_en": ing["name_en"] if ing["name_en"] else ""}
            if ing["required"]:
                required.append(ing_data)
                required_match = dict(ing)
                required_match["alias_values"] = _parse_alias_values(required_match.get("aliases"))
                if _is_default_staple_rice(dish_row, required_match):
                    match = ({
                        "ingredient_id": ing["ingredient_id"],
                        "name_cn": ing["name_cn"] or ing["ingredient_id"],
                    }, "default_staple")
                else:
                    match = next(
                        ((pantry, method) for pantry in active_pantry
                         if (method := _ingredient_match(required_match, pantry))),
                        None,
                    )
                if match:
                    pantry, method = match
                    matched = dict(ing_data)
                    matched.update({
                        "matched_pantry_id": pantry["ingredient_id"],
                        "matched_pantry_name_cn": pantry.get("name_cn") or pantry["ingredient_id"],
                        "match_method": method,
                    })
                    available_required.append(matched)
                else:
                    missing_required.append(ing_data)
            else:
                optional.append(ing_data)

        # V6 Section 27: 4 种状态判定（重新定义）
        required_count = len(required)
        missing_count = len(missing_required)
        missing_fields = []
        if not dish_row:
            missing_fields.append("dish")
        else:
            if not dish_row["category_id"]:
                missing_fields.append("category")
            try:
                meal_tags = json.loads(dish_row["meal_tags"] or "[]")
            except (json.JSONDecodeError, TypeError):
                meal_tags = []
            if not meal_tags:
                missing_fields.append("meal_tags")
            if not dish_row["image"]:
                missing_fields.append("image")
        if required_count == 0:
            missing_fields.append("required_ingredients")

        if required_count == 0:
            status = "incomplete"
        elif missing_count == 0:
            status = "available"
        elif missing_count <= 2:
            status = "almost_available"
        else:
            status = "missing"

        result = {
            "status": status,
            "required": required,
            "available_required": available_required,
            "missing_required": missing_required,
            "optional": optional,
            "data_complete": required_count > 0,
            "missing_fields": missing_fields,
            "available_now": status == "available",
            "inventory_version": inventory_version,
        }
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


def get_common_ingredients_static(location, limit=15):
    """Return this kitchen's top ingredients by durable pantry usage."""
    conn = get_db()
    try:
        rows = conn.execute(
            "SELECT i.ingredient_id,i.name_cn,i.name_en,s.add_count,s.last_action_at,"
            "CASE WHEN cp.is_active=1 THEN 1 ELSE 0 END AS in_pantry "
            "FROM pantry_usage_stats s JOIN ingredients i ON i.ingredient_id=s.ingredient_id "
            "LEFT JOIN current_pantry cp ON cp.location=s.location "
            "AND cp.ingredient_id=s.ingredient_id "
            "WHERE s.location=? AND s.add_count>0 "
            "ORDER BY s.add_count DESC,in_pantry DESC,s.last_action_at DESC,i.ingredient_id "
            "LIMIT ?",
            (location, max(0, min(int(limit), 15))),
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
                "INSERT INTO inventory_items (inventory_id, ingredient_id, status, notes) "
                "VALUES (?, ?, ?, ?)",
                (inv_id, item["ingredient_id"], item.get("status", "available"), item.get("notes"))
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
            "SELECT cp.ingredient_id, cp.status, i.name_cn, i.name_en "
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

    availability = check_dishes_availability_batch(dish_ids, location)
    conn = get_db()
    try:
        placeholders = ",".join("?" * len(dish_ids))
        names = {
            row["id"]: row["name_cn"]
            for row in conn.execute(
                f"SELECT id,name_cn FROM dishes WHERE id IN ({placeholders})", dish_ids
            )
        }
    finally:
        conn.close()
    shortages = []
    for dish_id, result in availability.items():
        for ingredient in result["missing_required"]:
            shortages.append({
                "dish_id": dish_id,
                "dish_name": names.get(dish_id, dish_id),
                "ingredient_id": ingredient["ingredient_id"],
                "ingredient_name": ingredient["name_cn"],
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
# 采购任务
# ============================================================

def create_purchase_requests(menu_date, location, shortages, dish_id=None):
    """
    根据缺货列表创建采购任务。
    返回: list of purchase_request ids
    """
    if not shortages:
        return []

    conn = get_db()
    try:
        request_ids = []
        for s in shortages:
            # 检查是否已有未解决的采购任务
            existing = conn.execute(
                "SELECT id FROM purchase_requests "
                "WHERE menu_date = ? AND location = ? AND ingredient_id = ? "
                "AND status IN ('needed', 'notified')",
                (menu_date, location, s["ingredient_id"])
            ).fetchone()

            if existing:
                continue  # 已有未解决的任务，跳过

            cur = conn.execute(
                "INSERT INTO purchase_requests "
                "(menu_date, location, dish_id, ingredient_id, status, notes) "
                "VALUES (?, ?, ?, ?, 'needed', ?)",
                (menu_date, location, dish_id or s.get("dish_id"),
                 s["ingredient_id"],
                 f"菜品: {s.get('dish_name', '?')} | 食材: {s.get('ingredient_name', '?')}")
            )
            request_ids.append(cur.lastrowid)

        conn.commit()

        if request_ids:
            log_event("purchase_requests_created", "purchase_requests", None, {
                "menu_date": menu_date, "location": location,
                "count": len(request_ids)
            })

        return request_ids
    finally:
        conn.close()


def get_purchase_requests(menu_date=None, location=None, status=None):
    """查询采购任务"""
    conn = get_db()
    try:
        query = ("SELECT pr.*, i.name_cn as ingredient_name, i.name_en as ingredient_name_en, "
                 "d.name_cn as dish_name "
                 "FROM purchase_requests pr "
                 "LEFT JOIN ingredients i ON pr.ingredient_id = i.ingredient_id "
                 "LEFT JOIN dishes d ON pr.dish_id = d.id "
                 "WHERE 1=1")
        params = []

        if menu_date:
            query += " AND pr.menu_date = ?"
            params.append(menu_date)
        if location:
            query += " AND pr.location = ?"
            params.append(location)
        if status:
            query += " AND pr.status = ?"
            params.append(status)

        query += " ORDER BY pr.created_at DESC"
        rows = conn.execute(query, params).fetchall()
        return [dict(r) for r in rows]
    finally:
        conn.close()


def update_purchase_status(request_id, status, resolved_by=None, notes=None):
    """
    更新采购任务状态。
    status: notified / purchased / unavailable
    """
    conn = get_db()
    try:
        now = datetime.now().isoformat()
        if status in ("purchased", "unavailable"):
            conn.execute(
                "UPDATE purchase_requests SET status = ?, resolved_at = ?, resolved_by = ?, notes = ? "
                "WHERE id = ?",
                (status, now, resolved_by, notes, request_id)
            )
        elif status == "notified":
            conn.execute(
                "UPDATE purchase_requests SET status = ?, notified_at = ? WHERE id = ?",
                (status, now, request_id)
            )
        else:
            conn.execute(
                "UPDATE purchase_requests SET status = ?, notes = ? WHERE id = ?",
                (status, notes, request_id)
            )

        conn.commit()
        log_event("purchase_status_updated", "purchase_requests", str(request_id), {
            "status": status, "resolved_by": resolved_by
        })
        return True
    finally:
        conn.close()


# ============================================================
# PushPlus 通知
# ============================================================

def send_pushplus(token, topic, title, content):
    try:
        PushPlusClient(token, topic).send(title, content)
        return True
    except PushError as e:
        print(f"[ERROR] PushPlus 通知失败: {e}")
        return False


def notify_purchase_requests(request_ids, location="shenzhen"):
    """
    向保姆发送采购通知（PushPlus）。
    将指定采购任务标记为 notified。
    """
    if not request_ids:
        return False

    conn = get_db()
    try:
        # 收集采购任务详情
        placeholders = ",".join("?" * len(request_ids))
        rows = conn.execute(
            f"SELECT pr.*, i.name_cn as ingredient_name, d.name_cn as dish_name "
            f"FROM purchase_requests pr "
            f"LEFT JOIN ingredients i ON pr.ingredient_id = i.ingredient_id "
            f"LEFT JOIN dishes d ON pr.dish_id = d.id "
            f"WHERE pr.id IN ({placeholders})",
            request_ids
        ).fetchall()

        if not rows:
            return False

        # 格式化通知内容
        menu_date = rows[0]["menu_date"]
        lines = [f"## 采购通知 - {menu_date}\n"]
        lines.append(f"**地点**: {location}\n")
        lines.append(f"**需要采购 {len(rows)} 项食材：**\n")

        for i, r in enumerate(rows, 1):
            dish_info = f"（用于：{r['dish_name']}）" if r["dish_name"] else ""
            lines.append(f"{i}. **{r['ingredient_name']}** {dish_info}")

        lines.append(f"\n---\n请在采购完成后回复确认。")

        content = "\n".join(lines)
        title = f"采购通知 - {menu_date} - {len(rows)}项"

        # 发送 PushPlus
        token = os.environ.get("PUSHPLUS_TOKEN", "")
        topic = os.environ.get("PUSHPLUS_TOPIC", "home-menu")

        if not token:
            print("[WARN] 未配置 PUSHPLUS_TOKEN，跳过通知")
            return False

        success = send_pushplus(token, topic, title, content)

        if success:
            # 标记为已通知
            for rid in request_ids:
                update_purchase_status(rid, "notified")

            log_event("purchase_notified", "purchase_requests", None, {
                "request_ids": request_ids, "count": len(request_ids)
            })

        return success
    finally:
        conn.close()


# ============================================================
# 完整闭环：点菜 → 缺货 → 采购 → 通知
# ============================================================

def process_selection_shortages(menu_id, dish_ids, location="shenzhen"):
    """
    完整流程：检查缺货 → 创建采购任务 → 通知保姆。
    返回: {shortages, purchase_request_ids, notified}
    """
    conn = get_db()
    try:
        menu = conn.execute("SELECT date FROM menus WHERE id = ?", (menu_id,)).fetchone()
        if not menu:
            return {"error": "menu not found"}

        menu_date = menu["date"]

        # 1. 检查缺货
        shortages = check_shortages(dish_ids, location, menu_date)

        if not shortages:
            return {
                "shortages": [],
                "purchase_request_ids": [],
                "notified": False,
                "message": "所有食材都有库存，无需采购"
            }

        # 2. 创建采购任务
        request_ids = create_purchase_requests(menu_date, location, shortages)

        # 3. 通知保姆
        notified = False
        if request_ids:
            notified = notify_purchase_requests(request_ids, location)

        return {
            "shortages": shortages,
            "purchase_request_ids": request_ids,
            "notified": notified,
            "message": f"发现 {len(shortages)} 项缺货，已创建 {len(request_ids)} 个采购任务"
                       + ("并通知保姆" if notified else "（通知失败）")
        }
    finally:
        conn.close()


# ============================================================
# CLI 测试
# ============================================================

if __name__ == "__main__":
    import argparse

    parser = argparse.ArgumentParser(description="库存与采购管理")
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

    # 查看采购任务
    p_pr = sub.add_parser("requests", help="查看采购任务")
    p_pr.add_argument("--date", type=str)
    p_pr.add_argument("--status", type=str)

    # 更新采购状态
    p_update = sub.add_parser("update", help="更新采购状态")
    p_update.add_argument("--id", type=int, required=True)
    p_update.add_argument("--status", required=True, choices=["notified", "purchased", "unavailable"])
    p_update.add_argument("--by", default="nanny")

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

    elif args.command == "requests":
        reqs = get_purchase_requests(menu_date=args.date, status=args.status)
        if reqs:
            for r in reqs:
                print(f"  #{r['id']} | {r['menu_date']} | {r.get('ingredient_name','?')} | {r['status']}")
        else:
            print("无采购任务")

    elif args.command == "update":
        update_purchase_status(args.id, args.status, resolved_by=args.by)
        print(f"[OK] 采购任务 #{args.id} 状态更新为 {args.status}")

    else:
        parser.print_help()
