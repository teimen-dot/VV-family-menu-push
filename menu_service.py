#!/usr/bin/env python3
"""
家庭菜单管家 - 菜单服务层
连接 rule_engine.py (配餐引擎) 和 SQLite (数据存储)。
提供：生成菜单、增删改查菜品、AI补齐、重新搭配、锁定、确认。
"""

import json
import random
from datetime import date, datetime, timedelta
from db import get_db, log_event, get_config
from rule_engine import (
    GapFiller, RuleEngine, NutritionAnalyzer, MealState,
    generate_afternoon_snack, get_dish_ingredients_map,
    get_inventory_ingredients,
    get_history_3day, get_history_7day,
    analyze_meal_slots, filter_candidates_for_slot,
    BREAKFAST_COMPANION_STAPLES,
)
from inventory import check_shortages, get_available_ingredient_ids, check_dishes_availability_batch
from preference_service import get_preference_scores, record_vv_confirm


# V11: Catalog cache — invalidates when catalog_version changes
_catalog_cache = {"version": None, "pool": None}


def _get_effective_diners_count(menu_id=None, menu_row=None):
    """V11: 获取有效用餐人数，支持 banquet 模式。
    banquet 模式下使用 banquet_total_diners；daily 模式下使用 diners 数组长度。
    """
    if menu_row is None and menu_id:
        conn = get_db()
        try:
            menu_row = conn.execute(
                "SELECT diners, meal_mode, banquet_total_diners FROM menus WHERE id = ?",
                (menu_id,)
            ).fetchone()
        finally:
            conn.close()

    if not menu_row:
        return 4

    meal_mode = menu_row["meal_mode"] if "meal_mode" in menu_row.keys() else "daily"
    if meal_mode == "banquet":
        banquet_total = menu_row["banquet_total_diners"] if "banquet_total_diners" in menu_row.keys() else None
        if banquet_total and banquet_total > 0:
            return banquet_total

    diners_json = menu_row["diners"]
    if diners_json:
        try:
            diner_ids = json.loads(diners_json)
            return max(len(diner_ids), 1)
        except (json.JSONDecodeError, TypeError):
            pass

    return menu_row["diners_count"] if menu_row["diners_count"] else 4


def _load_pool():
    """V6: 从 SQLite 加载菜品池（Single Source of Truth）。
    只加载 is_active=1 的菜品。
    V11: 使用 catalog_version 缓存，菜品管理器变更时自动失效。"""
    catalog_version = get_config("catalog_version") or "1"
    if _catalog_cache["version"] == catalog_version and _catalog_cache["pool"] is not None:
        return _catalog_cache["pool"]

    conn = get_db()
    try:
        rows = conn.execute(
            "SELECT * FROM dishes WHERE is_active = 1 OR is_active IS NULL ORDER BY id"
        ).fetchall()
        dishes = []
        for r in rows:
            d = dict(r)
            # 解析 JSON 字段
            for field in ["protein_types", "vegetables", "meal_tags", "cooking_methods", "custom_tags", "meal_roles"]:
                if d.get(field):
                    try:
                        d[field] = json.loads(d[field])
                    except (json.JSONDecodeError, TypeError):
                        d[field] = []
                else:
                    d[field] = []
            dishes.append(d)
        pool = {"dishes": dishes}
        _catalog_cache["version"] = catalog_version
        _catalog_cache["pool"] = pool
        return pool
    finally:
        conn.close()


def invalidate_catalog_cache():
    """V11: 手动失效 catalog cache（菜品管理器调用）"""
    _catalog_cache["version"] = None
    _catalog_cache["pool"] = None


def _store_menu_items(conn, menu_id, result):
    """将 rule_engine 生成结果存入 menu_items（每道菜一行）
    V6: 跳过 dish_id 为 None/空的候选，不写入 null menu_item"""
    # 先清除旧条目
    conn.execute("DELETE FROM menu_items WHERE menu_id = ?", (menu_id,))

    sort = 0
    for meal_type in ["breakfast", "lunch", "afternoon_snack", "dinner"]:
        dishes = result.get(meal_type, {}).get("dishes", [])
        for d in dishes:
            # V6 Section 14: 没有 dish 就不能创建 menu_item
            dish_id = d.get("id")
            if not dish_id or dish_id == "None":
                continue
            conn.execute(
                "INSERT INTO menu_items (menu_id, dish_id, meal_type, is_locked, sort_order, source) "
                "VALUES (?, ?, ?, ?, ?, ?)",
                (menu_id, dish_id, meal_type, 0, sort, "ai")
            )
            sort += 1
    conn.commit()


def generate_and_store_menu(date_str, location="shenzhen", seed=None, locked=None):
    """
    用 rule_engine 生成一天菜单并存入 SQLite。
    locked: {"breakfast": ["dish_0001"], "dinner": ["dish_0010"]}
    返回: menu_id
    """
    pool = _load_pool()
    locked = locked or {}
    dish_ings = get_dish_ingredients_map()

    # 库存上下文
    inv_avail, inv_pri, inv_exp = get_available_ingredient_ids(location)

    # V10: 读取已有菜单的 diners_count（如果存在），确保晚餐按人数生成
    # V11: 使用 _get_effective_diners_count 支持 banquet 模式
    diners_count = 4
    meal_mode = "daily"
    conn_pre = get_db()
    try:
        existing = conn_pre.execute(
            "SELECT diners, meal_mode, banquet_total_diners FROM menus WHERE date = ?",
            (date_str,)
        ).fetchone()
        if existing:
            diners_count = _get_effective_diners_count(menu_row=existing)
            meal_mode = existing["meal_mode"] if existing["meal_mode"] else "daily"
    finally:
        conn_pre.close()

    # V11: 获取 VV preference scores
    all_dish_ids = [d["id"] for d in pool["dishes"]]
    vv_prefs = get_preference_scores(all_dish_ids)
    dish_availability = check_dishes_availability_batch(all_dish_ids, location)

    context = {
        "history_3day": get_history_3day(),
        "history_7day": get_history_7day(),
        "inventory_ingredients": inv_avail,
        "priority_ingredients": inv_pri,
        "expiring_ingredients": inv_exp,
        "dish_ingredients": dish_ings,
        "dish_availability": {dish_id: value["status"] for dish_id, value in dish_availability.items()},
        "vv_preferences": vv_prefs,  # V11: VV confirm-based preference
        "is_banquet": meal_mode == "banquet",  # V11: banquet mode flag
    }

    # 生成三餐
    gf = GapFiller(pool, seed=seed, dish_ingredients=dish_ings)
    result, logs = gf.generate_day(locked=locked, context=context, diners_count=diners_count)

    # 生成下午茶
    rng = random.Random((seed or 42) + 1000)
    snacks = generate_afternoon_snack(pool, rng=rng)
    result["afternoon_snack"] = {"dishes": snacks, "state": None}

    # Final Review
    review = logs.get("review", {})

    # 存入数据库
    conn = get_db()
    try:
        # UPSERT menu
        conn.execute(
            "INSERT INTO menus (date, location, status, notes_zh, notes_en) "
            "VALUES (?, ?, 'draft', ?, ?) "
            "ON CONFLICT(date) DO UPDATE SET "
            "location=excluded.location, status='draft', "
            "notes_zh=excluded.notes_zh, notes_en=excluded.notes_en",
            (date_str, location,
             "；".join(review.get("issues", [])) if not review["passed"] else "",
             "; ".join(review.get("issues", [])) if not review["passed"] else "")
        )
        conn.commit()

        row = conn.execute("SELECT id FROM menus WHERE date = ?", (date_str,)).fetchone()
        menu_id = row["id"]

        _store_menu_items(conn, menu_id, result)

        # 存 LOCKED 标记
        for meal_type, dish_ids in locked.items():
            for did in dish_ids:
                conn.execute(
                    "UPDATE menu_items SET is_locked = 1, locked_by = 'owner', "
                    "locked_at = ? WHERE menu_id = ? AND dish_id = ? AND meal_type = ?",
                    (datetime.now().isoformat(), menu_id, did, meal_type)
                )
        conn.commit()

        log_event("menu_generated", "menu", str(menu_id), {
            "date": date_str, "location": location, "seed": seed,
            "review_passed": review["passed"],
            "dish_count": sum(len(result.get(mt, {}).get("dishes", []))
                              for mt in ["breakfast", "lunch", "afternoon_snack", "dinner"])
        })

        return menu_id, review
    finally:
        conn.close()


def get_menu_with_dishes(date_str):
    """
    获取某天菜单，带完整菜品信息。
    返回: {date, exists, menu_id, status, location, meals: {breakfast: [...], ...}, review}
    """
    conn = get_db()
    try:
        menu = conn.execute("SELECT * FROM menus WHERE date = ?", (date_str,)).fetchone()
        if not menu:
            return {"date": date_str, "exists": False}

        items = conn.execute(
            "SELECT mi.id as menu_item_id, mi.dish_id, mi.custom_name, mi.meal_type, mi.is_locked, "
            "mi.locked_by, mi.sort_order, mi.source, "
            "d.name_cn, d.name_en, d.category_id, d.carb_type, d.protein_types, "
            "d.vegetables, d.vegetable_count, d.image, d.meal_tags, d.cooking_methods, "
            "d.taste, d.banquet, d.custom_tags, "
            "d.quick_soup, d.slow_soup, d.manual_only_for_breakfast, "
            "d.meal_roles, "
            "d.is_active as dish_is_active "
            "FROM menu_items mi "
            "LEFT JOIN dishes d ON mi.dish_id = d.id "
            "WHERE mi.menu_id = ? ORDER BY mi.meal_type, mi.sort_order",
            (menu["id"],)
        ).fetchall()

        meals = {"breakfast": [], "lunch": [], "afternoon_snack": [], "dinner": []}
        for item in items:
            mt = item["meal_type"]
            if mt not in meals:
                continue

            # V7 Section: 过滤 null/orphan menu items
            dish_id = item["dish_id"]
            custom_name = item["custom_name"] if "custom_name" in item.keys() else None

            # V7: 历史数据兼容 — dish_id 可能是 V3 时代的"组合菜名"（中文），
            # 视为 custom_name 保留显示，不当作脏数据过滤
            is_historical_name = (
                dish_id and not dish_id.startswith("dish_") and dish_id != "None"
            )

            # 仅过滤真正的脏数据：dish_id 为空/null/None 且无 custom_name
            if (not dish_id or dish_id == "None" or dish_id == "") and not custom_name:
                log_event("menu_item_filtered_null", "menu_items", str(item["menu_item_id"]), {
                    "menu_id": menu["id"], "dish_id": str(dish_id)
                })
                continue

            d = dict(item)

            # V7: 历史数据回填 — 把 dish_id 中文菜名填到 name_cn，让前端正常显示
            if is_historical_name and not d.get("name_cn"):
                d["name_cn"] = dish_id
                d["name_en"] = "(历史组合菜单 Historical Combo)"
                d["is_historical_combo"] = True
                if not custom_name:
                    d["custom_name"] = dish_id

            # V6: 如果菜品已被删除（is_active=0），标记为已下架
            d["dish_archived"] = (d.get("dish_is_active") == 0)

            # 解析 JSON 字段
            for field in ["protein_types", "vegetables", "meal_tags", "cooking_methods", "custom_tags", "meal_roles"]:
                if d.get(field):
                    try:
                        d[field] = json.loads(d[field])
                    except (json.JSONDecodeError, TypeError):
                        d[field] = []
                else:
                    d[field] = []
            d["is_locked"] = bool(d.get("is_locked"))
            meals[mt].append(d)

        # V5: 使用统一 InventoryService 检查可用性（只看 required ingredients）
        location = menu["location"]
        dish_ids = [item["dish_id"] for item in items
                    if item["dish_id"] and item["dish_id"].startswith("dish_")]
        avail_batch = check_dishes_availability_batch(dish_ids, location) if dish_ids else {}

        # 按菜品分组缺货（只看 required ingredients）
        shortage_map = {}
        for did, avail in avail_batch.items():
            missing_names = [m["name_cn"] for m in avail["missing_required"]]
            if missing_names:
                shortage_map[did] = missing_names

        return {
            "date": date_str,
            "exists": True,
            "menu_id": menu["id"],
            "status": menu["status"],
            "confirmed_at": menu["confirmed_at"],
            "pushed_at": menu["pushed_at"],
            "push_status": menu["push_status"] if "push_status" in menu.keys() else "not_sent",
            "push_error": menu["push_error"] if "push_error" in menu.keys() else None,
            "location": location,
            "meals": meals,
            "shortages": shortage_map,
            "review_issues": menu["notes_zh"] or "",
        }
    finally:
        conn.close()


def add_dish_to_menu(menu_id, dish_id, meal_type):
    """添加一道菜到菜单的指定餐次。Owner 添加的菜自动锁定。"""
    conn = get_db()
    try:
        active = conn.execute(
            "SELECT 1 FROM dishes WHERE id=? AND is_active=1", (dish_id,)
        ).fetchone()
        if not active:
            return False
        # 获取当前最大 sort_order
        row = conn.execute(
            "SELECT MAX(sort_order) as max_sort FROM menu_items WHERE menu_id = ? AND meal_type = ?",
            (menu_id, meal_type)
        ).fetchone()
        sort = (row["max_sort"] or 0) + 1

        # Owner 添加 = 自动锁定 + source=owner
        conn.execute(
            "INSERT INTO menu_items (menu_id, dish_id, meal_type, is_locked, locked_by, locked_at, sort_order, source) "
            "VALUES (?, ?, ?, 1, 'owner', ?, ?, 'owner')",
            (menu_id, dish_id, meal_type, datetime.now().isoformat(), sort)
        )
        conn.commit()

        dish = conn.execute("SELECT name_cn, name_en FROM dishes WHERE id = ?", (dish_id,)).fetchone()
        log_event("dish_added", "menu_item", dish_id, {
            "menu_id": menu_id, "meal_type": meal_type,
            "dish_name": dish["name_cn"] if dish else dish_id,
            "source": "owner", "auto_locked": True
        })
        return True
    finally:
        conn.close()


def remove_dish_from_menu(menu_id, menu_item_id):
    """从菜单删除一道菜（owner 和 AI 菜均可删除）"""
    conn = get_db()
    try:
        item = conn.execute(
            "SELECT is_locked, dish_id FROM menu_items WHERE id = ? AND menu_id = ?",
            (menu_item_id, menu_id)
        ).fetchone()
        if not item:
            return False, "菜品不存在"

        conn.execute("DELETE FROM menu_items WHERE id = ?", (menu_item_id,))
        conn.commit()
        log_event("dish_removed", "menu_item", str(menu_item_id), {"menu_id": menu_id})
        return True, "已删除"
    finally:
        conn.close()


def replace_dish_in_menu(menu_id, menu_item_id, new_dish_id):
    """替换菜单中的一道菜。替换后的菜自动锁定为 owner 选择。"""
    conn = get_db()
    try:
        active = conn.execute(
            "SELECT 1 FROM dishes WHERE id=? AND is_active=1", (new_dish_id,)
        ).fetchone()
        if not active:
            return False, "目标菜品不存在或已下架"
        item = conn.execute(
            "SELECT is_locked, meal_type, sort_order FROM menu_items WHERE id = ? AND menu_id = ?",
            (menu_item_id, menu_id)
        ).fetchone()
        if not item:
            return False, "菜品不存在"

        # 替换菜品，新菜自动锁定为 owner 选择
        conn.execute(
            "UPDATE menu_items SET dish_id = ?, is_locked = 1, locked_by = 'owner', "
            "locked_at = ? WHERE id = ?",
            (new_dish_id, datetime.now().isoformat(), menu_item_id)
        )
        conn.commit()

        dish = conn.execute("SELECT name_cn FROM dishes WHERE id = ?", (new_dish_id,)).fetchone()
        log_event("dish_replaced", "menu_item", str(menu_item_id), {
            "menu_id": menu_id, "new_dish_id": new_dish_id,
            "new_dish_name": dish["name_cn"] if dish else new_dish_id,
            "source": "owner", "auto_locked": True
        })
        return True, "已替换"
    finally:
        conn.close()


def lock_dish(menu_item_id, locked=True):
    """锁定/解锁一道菜"""
    conn = get_db()
    try:
        conn.execute(
            "UPDATE menu_items SET is_locked = ?, locked_by = ?, locked_at = ? WHERE id = ?",
            (1 if locked else 0, "owner" if locked else None,
             datetime.now().isoformat() if locked else None, menu_item_id)
        )
        conn.commit()
        return True
    finally:
        conn.close()


def ai_fill_menu(menu_id, location="shenzhen", seed=None, meal_type=None):
    """
    V11: AI 补充缺少菜品 (AI Fill Gaps)：
    保留当前所有菜（owner + AI），先分析槽位缺口，只补 missing_min > 0 的槽位。
    Available Now 优先；无 Available 不自动加入缺货菜；无候选返回 unmet_slot with reason。
    meal_type: 指定餐次，None = 所有餐次。

    V11 关键修复:
    - 不再跳过空餐次（existing_ids 为空时也从零开始补齐）
    - 返回 mutation results: {added, removed, unmet_slots, slot_analysis_before/after}
    - INSERT 前重新验证 is_active
    - 使用 _get_effective_diners_count 支持 banquet
    - 使用 VV preference 排序
    """
    conn = get_db()
    try:
        menu = conn.execute(
            "SELECT date, location, diners, meal_mode, banquet_total_diners FROM menus WHERE id = ?",
            (menu_id,)
        ).fetchone()
        if not menu:
            return False, "菜单不存在", None

        date_str = menu["date"]
        loc = menu["location"] or location
        pool = _load_pool()
        dish_ings = get_dish_ingredients_map()
        inv_avail, inv_pri, inv_exp = get_inventory_ingredients(loc)

        # V11: 获取有效人数 + VV preferences
        diners_count = _get_effective_diners_count(menu_row=menu)
        all_dish_ids = [d["id"] for d in pool["dishes"]]
        vv_prefs = get_preference_scores(all_dish_ids)

        context = {
            "history_3day": get_history_3day(),
            "history_7day": get_history_7day(),
            "inventory_ingredients": inv_avail,
            "priority_ingredients": inv_pri,
            "expiring_ingredients": inv_exp,
            "dish_ingredients": dish_ings,
            "vv_preferences": vv_prefs,
            "is_banquet": (menu["meal_mode"] == "banquet") if menu["meal_mode"] else False,
        }

        gf = GapFiller(pool, seed=seed or 42, dish_ingredients=dish_ings)
        dish_map = {d["id"]: d for d in pool["dishes"]}

        # V11: active dish IDs set for re-validation
        active_dish_ids = set(dish_map.keys())

        # 获取当前菜单所有菜品
        all_items = conn.execute(
            "SELECT id, dish_id, meal_type, is_locked FROM menu_items WHERE menu_id = ?",
            (menu_id,)
        ).fetchall()

        # 按餐次分组
        meals_existing = {"breakfast": [], "lunch": [], "dinner": []}
        for item in all_items:
            mt = item["meal_type"]
            if mt in meals_existing:
                meals_existing[mt].append(item["dish_id"])

        # 确定要处理的餐次
        target_meals = [meal_type] if meal_type else ["breakfast", "lunch", "dinner"]

        day_history = set()
        day_proteins = set()
        added_dishes = []  # V11: track mutations
        unmet_slots = []
        seen_unmet = set()

        for mt in ["breakfast", "lunch", "dinner"]:
            for did in meals_existing[mt]:
                if did in dish_map:
                    day_history.add(did)
                    day_proteins.update(dish_map[did].get("protein_types", []))

        for mt in target_meals:
            if mt not in meals_existing:
                continue

            # V11 FIX: 不再跳过空餐次 — 从空 state 开始补齐
            existing_ids = [did for did in meals_existing[mt] if did in dish_map]

            # 构建 MealState (空也 OK)
            state = MealState()
            for did in existing_ids:
                analysis = NutritionAnalyzer.analyze(dish_map[did])
                state.add_dish(analysis, is_locked=True)

            # V11: 记录 slot analysis before
            slots_before = analyze_meal_slots(mt, state, diners_count)

            added = _fill_missing_slots_v8(
                conn, menu_id, mt, state, gf, dish_map, context,
                day_history, day_proteins, diners_count, loc,
                unmet_slots, seen_unmet, active_dish_ids, added_dishes
            )

        conn.commit()

        # V11: 重新分析 slot analysis after
        slot_analysis_after = {}
        for mt in target_meals:
            state_after = MealState()
            items_after = conn.execute(
                "SELECT dish_id FROM menu_items WHERE menu_id = ? AND meal_type = ?",
                (menu_id, mt)
            ).fetchall()
            for r in items_after:
                did = r["dish_id"]
                if did in dish_map:
                    analysis = NutritionAnalyzer.analyze(dish_map[did])
                    state_after.add_dish(analysis, is_locked=True)
            slot_analysis_after[mt] = analyze_meal_slots(mt, state_after, diners_count)

        # Final Review
        menu_data = get_menu_with_dishes(date_str)
        day_result = {}
        for mt in ["breakfast", "lunch", "dinner"]:
            state = MealState()
            for item in menu_data["meals"].get(mt, []):
                did = item["dish_id"]
                if did in dish_map:
                    analysis = NutritionAnalyzer.analyze(dish_map[did])
                    state.add_dish(analysis, is_locked=item["is_locked"])
            day_result[mt] = {"state": state}

        review = RuleEngine.final_review(day_result, diners_count)
        review["unmet_slots"] = unmet_slots
        review["added"] = [d["dish_id"] for d in added_dishes]
        review["added_details"] = added_dishes
        review["removed"] = []
        review["slot_analysis_after"] = slot_analysis_after

        log_event("ai_fill_menu", "menu", str(menu_id), {
            "added": review["added"],
            "unmet_slots": unmet_slots,
            "review_passed": review["passed"],
        })

        return True, f"AI 补充了 {len(added_dishes)} 道菜", review
    finally:
        conn.close()


def _fill_missing_slots_v8(conn, menu_id, meal_type, state, gf, dish_map, context,
                            day_history, day_proteins, diners_count, location,
                            unmet_slots, seen_unmet=None,
                            active_dish_ids=None, added_dishes=None):
    """
    V8/V9: 槽位分析 + Available Now 优先补齐。
    用于 breakfast / lunch / dinner。
    V9: 早餐按固定顺序补齐 (porridge → companion_staple → egg_dish → tofu → vegetable → protein → coarse_grain)。
    V9: 晚餐按人数精确 target 补齐 (不再只补 minimum)。
    V10: unmet_slots 去重 — 同一个 (meal, slot) 只记录一次。
    V10: 幂等 — 所有槽位已满足时 0 change。
    V11: INSERT 前重新验证 is_active；记录 added_dishes mutation。
    V11: 无候选时返回 reason = no_available_candidate。
    返回: items_added (int)
    """
    items_added = 0
    max_rounds = 8

    if seen_unmet is None:
        seen_unmet = set()
    if added_dishes is None:
        added_dishes = []

    BREAKFAST_SLOT_ORDER = [
        "porridge", "companion_staple", "egg", "tofu",
        "vegetable", "protein_main", "coarse_grain"
    ]

    for round_i in range(max_rounds):
        # Step 1: 分析当前槽位
        slots = analyze_meal_slots(meal_type, state, diners_count)

        # Step 2: 找 missing_min > 0 的槽位
        missing = {k: v for k, v in slots.items() if v["missing_min"] > 0}
        if not missing:
            break  # 所有槽位已满足 — V10: 幂等 STOP

        # Step 3: 逐个补齐缺失槽位
        if meal_type == "breakfast":
            ordered_slots = [s for s in BREAKFAST_SLOT_ORDER if s in missing]
        else:
            ordered_slots = list(missing.keys())

        for slot_name in ordered_slots:
            # V12: Re-check if slot is still missing (may have been satisfied by a dish added earlier in this round)
            latest_slots = analyze_meal_slots(meal_type, state, diners_count)
            if latest_slots.get(slot_name, {}).get("missing_min", 0) <= 0:
                continue
            slot_info = missing[slot_name]
            # 获取该槽位的候选菜
            all_candidates = gf.get_candidates(meal_type, exclude_ids=day_history)
            slot_candidates = filter_candidates_for_slot(all_candidates, slot_name)

            # V11: 过滤掉已删除的菜品（re-validate is_active）
            if active_dish_ids:
                slot_candidates = [c for c in slot_candidates if c["id"] in active_dish_ids]

            if not slot_candidates:
                dedup_key = (meal_type, slot_name)
                if dedup_key not in seen_unmet:
                    seen_unmet.add(dedup_key)
                    unmet_slots.append({
                        "meal": meal_type,
                        "slot": slot_name,
                        "reason": "no_candidate",
                        "message": f"暂未找到合适的{slot_name}菜品，请手动添加 / No suitable {slot_name} found.",
                    })
                continue

            # V8: Available Now 优先 — 检查库存可用性
            candidate_ids = [c["id"] for c in slot_candidates]
            avail_batch = check_dishes_availability_batch(candidate_ids, location)

            available_candidates = [
                c for c in slot_candidates
                if avail_batch.get(c["id"], {}).get("status") == "available"
            ]

            if available_candidates:
                # 有 Available 候选 → 评分选最优
                meal_ctx = dict(context)
                meal_ctx["day_proteins"] = set(day_proteins)
                meal_ctx["day_history"] = set(day_history)

                scored = []
                for c in available_candidates:
                    s = gf.scorer.score_dish(c, state, meal_type, meal_ctx)
                    scored.append((s, c))
                scored.sort(key=lambda x: x[0], reverse=True)

                chosen = scored[0][1]

                # V11: 最终 is_active 验证（防止旧 cache）
                if active_dish_ids and chosen["id"] not in active_dish_ids:
                    continue

                # 添加到数据库
                row = conn.execute(
                    "SELECT MAX(sort_order) as max_sort FROM menu_items WHERE menu_id = ? AND meal_type = ?",
                    (menu_id, meal_type)
                ).fetchone()
                sort = (row["max_sort"] or 0) + 1

                conn.execute(
                    "INSERT INTO menu_items (menu_id, dish_id, meal_type, is_locked, sort_order, source) "
                    "VALUES (?, ?, ?, 0, ?, ?)",
                    (menu_id, chosen["id"], meal_type, sort, "ai")
                )
                items_added += 1
                day_history.add(chosen["id"])
                day_proteins.update(chosen.get("protein_types", []))

                # V11: 记录 mutation
                added_dishes.append({
                    "dish_id": chosen["id"],
                    "name_cn": chosen.get("name_cn", ""),
                    "meal_type": meal_type,
                    "slot_role": slot_name,
                })

                # 更新 state
                state.add_dish(chosen, is_locked=False)
            else:
                # V8: 无 Available → 检查 Almost Available
                almost_candidates = [
                    c for c in slot_candidates
                    if avail_batch.get(c["id"], {}).get("status") == "almost_available"
                ]
                dedup_key = (meal_type, slot_name)
                if dedup_key not in seen_unmet:
                    seen_unmet.add(dedup_key)
                    if almost_candidates:
                        unmet_slots.append({
                            "meal": meal_type,
                            "slot": slot_name,
                            "reason": "no_available_candidate",
                            "almost_count": len(almost_candidates),
                            "almost_dishes": [
                                {"id": c["id"], "name_cn": c["name_cn"],
                                 "name_en": c.get("name_en", ""),
                                 "missing": [m["name_cn"] for m in avail_batch.get(c["id"], {}).get("missing_required", [])]}
                                for c in almost_candidates[:5]
                            ],
                            "message": (
                                f"当前库存没有可直接制作的{slot_name}菜品。"
                                f"有{len(almost_candidates)}道菜只差1种食材。"
                                f" / No {slot_name} is fully available. "
                                f"{len(almost_candidates)} dishes are missing only one ingredient."
                            ),
                        })
                    else:
                        unmet_slots.append({
                            "meal": meal_type,
                            "slot": slot_name,
                            "reason": "no_available_candidate",
                            "message": (
                                f"当前库存没有可直接制作的{slot_name}菜品，请手动添加。"
                                f" / No {slot_name} is fully available. Please add manually."
                            ),
                        })

    return items_added


# V10: 槽位 → 角色 映射（用于 reconcile 时识别 AI 菜品角色）
_RECONCILE_SLOT_ROLES = {
    "protein_main": ["protein_main"],
    "vegetable_dish": ["vegetable_dish"],
    "staple": ["staple"],
    "slow_soup": ["slow_soup"],
    "quick_soup": ["quick_soup"],
    "egg": ["egg_dish"],
    "tofu": ["tofu_dish"],
    "porridge": [],
    "companion_staple": [],
    "coarse_grain": [],
    "vegetable": [],
}


def reconcile_meal_for_diners(menu_id, location="shenzhen"):
    """
    V10: Diners 变化后重新调整菜单。
    V11: 使用 _get_effective_diners_count 支持 banquet 模式。
    
    流程:
      1. 读取 diners_count（V11: 通过 _get_effective_diners_count）
      2. 获取精确 target（晚餐按人数）
      3. 分析 Owner Selected（source=owner / is_locked=1）
      4. 分析 AI items（source=ai / is_locked=0）
      5. 计算各 slot 当前数量
      6. 删除 excess AI items（保留 owner）
      7. 对不足 slot 调用 ai_fill_menu 补齐
    
    Owner Selected 永远保留。超额时优先删除 AI items。
    返回: (ok, msg, review)
    """
    conn = get_db()
    try:
        menu = conn.execute(
            "SELECT date, location, diners, meal_mode, banquet_total_diners "
            "FROM menus WHERE id = ?",
            (menu_id,)
        ).fetchone()
        if not menu:
            return False, "菜单不存在", None

        date_str = menu["date"]
        loc = menu["location"] or location

        # V11: 使用 _get_effective_diners_count 支持 banquet 模式
        diners_count = _get_effective_diners_count(menu_row=menu)

        pool = _load_pool()
        dish_map = {d["id"]: d for d in pool["dishes"]}

        removed_count = 0

        # 对每个餐次执行 reconcile（晚餐最关键，但也处理午餐）
        for mt in ["breakfast", "lunch", "dinner"]:
            # 获取该餐次所有 items，区分 owner 和 AI
            items = conn.execute(
                "SELECT id, dish_id, is_locked, source FROM menu_items "
                "WHERE menu_id = ? AND meal_type = ? ORDER BY sort_order",
                (menu_id, mt)
            ).fetchall()

            # 分析所有菜品的槽位贡献
            state = MealState()
            owner_items = []  # (menu_item_id, dish_id, analysis)
            ai_items = []     # (menu_item_id, dish_id, analysis)

            for item in items:
                did = item["dish_id"]
                if did not in dish_map:
                    continue
                analysis = NutritionAnalyzer.analyze(dish_map[did])
                is_owner = item["is_locked"] or item["source"] == "owner"
                # V10 FIX: state 必须包含所有菜品（owner + AI），否则
                # 当没有 owner 菜时 current=0，无法检测超额。
                state.add_dish(analysis, is_locked=is_owner)
                if is_owner:
                    owner_items.append((item["id"], did, analysis))
                else:
                    ai_items.append((item["id"], did, analysis))

            # 获取 target
            if mt == "dinner":
                target = RuleEngine._dinner_target(diners_count)
            elif mt == "lunch":
                target = {"protein_main": 1, "vegetable_dish": 1, "staple": 1, "quick_soup": 1}
            elif mt == "breakfast":
                target = {
                    "porridge": 1, "companion_staple": 1, "coarse_grain": 1,
                    "protein_main": 1, "vegetable": 2, "egg": 1, "tofu": 1
                }
            else:
                continue

            # 计算每个 slot 的 owner-only 数量（用于限制删除：只删 AI，不删 owner）
            owner_state = MealState()
            for _, _, analysis in owner_items:
                owner_state.add_dish(analysis, is_locked=True)
            owner_slots = analyze_meal_slots(mt, owner_state, diners_count)

            # 对每个 slot，如果总数（owner + AI）超过 target，删除多余的 AI items
            slots = analyze_meal_slots(mt, state, diners_count)
            excess_slots = {k: v for k, v in slots.items() if v["current"] > v["target_min"]}

            if not excess_slots:
                continue

            # 按 slot 删除 excess AI items
            # 优先删除：缺食材的 → almost available → 普通 AI → 最近重复度高的
            from inventory import check_dishes_availability_batch
            ai_dish_ids = [aid for _, aid, _ in ai_items]
            avail_batch = check_dishes_availability_batch(ai_dish_ids, loc) if ai_dish_ids else {}

            # 对每个超额 slot，计算需要删除多少 AI items
            for slot_name, slot_info in excess_slots.items():
                total_current = slot_info["current"]
                target_min = slot_info["target_min"]
                # owner 菜占用的槽位数 — 不能删 owner 菜
                owner_current = owner_slots.get(slot_name, {}).get("current", 0)
                # 可删除的 AI 菜数量 = min(超额数, AI 贡献数)
                excess = total_current - target_min
                ai_contributable = total_current - owner_current
                excess = min(excess, max(0, ai_contributable))
                if excess <= 0:
                    continue

                # 找出贡献该 slot 的 AI items
                slot_roles = _RECONCILE_SLOT_ROLES.get(slot_name, [])
                contributing_ai = []

                for mi_id, did, analysis in ai_items:
                    roles = analysis.get("meal_roles", [])
                    cat = analysis.get("category_id", "")
                    # 判断这道 AI 菜是否贡献该 slot
                    contributes = False
                    if slot_name == "protein_main":
                        contributes = "protein_main" in roles or cat in ("protein_main", "egg_tofu")
                    elif slot_name == "vegetable_dish":
                        contributes = "vegetable_dish" in roles or cat in ("vegetable_mushroom", "cold_dish")
                    elif slot_name == "staple":
                        contributes = "staple" in roles or cat == "staple_carb"
                    elif slot_name == "slow_soup":
                        contributes = "slow_soup" in roles or analysis.get("is_slow_soup")
                    elif slot_name == "quick_soup":
                        contributes = "quick_soup" in roles or analysis.get("is_quick_soup")
                    elif slot_name == "egg":
                        contributes = "egg_dish" in roles
                    elif slot_name == "tofu":
                        contributes = "tofu_dish" in roles
                    elif slot_name == "porridge":
                        contributes = analysis.get("carb_type") == "porridge"
                    elif slot_name == "companion_staple":
                        contributes = analysis.get("breakfast_staple_type") in BREAKFAST_COMPANION_STAPLES
                    elif slot_name == "coarse_grain":
                        contributes = analysis.get("carb_type") == "coarse_grain"
                    elif slot_name == "vegetable":
                        # 早餐蔬菜是种类数，不是菜品数 — 不删除
                        contributes = False

                    if contributes:
                        avail_status = avail_batch.get(did, {}).get("status", "available")
                        contributing_ai.append((mi_id, did, avail_status))

                if not contributing_ai:
                    continue

                # 按优先级排序：缺食材的先删 (missing > almost > available)
                # priority: 0=missing, 1=almost, 2=available (V12: incomplete 已废弃)
                def del_priority(item_tuple):
                    _, _, status = item_tuple
                    return {"missing": 0, "almost_available": 1, "available": 2}.get(status, 3)

                contributing_ai.sort(key=del_priority)

                # 删除前 excess 个
                to_remove = contributing_ai[:excess]
                for mi_id, did, _ in to_remove:
                    conn.execute("DELETE FROM menu_items WHERE id = ?", (mi_id,))
                    removed_count += 1
                    # 从 state 移除（重新构建更简单）
                    ai_items = [(m, d, a) for m, d, a in ai_items if m != mi_id]

        conn.commit()

        # 删除完成后，调用 ai_fill_menu 补齐可能新增的缺口
        if removed_count > 0:
            log_event("reconcile_removed_excess", "menu", str(menu_id), {
                "diners_count": diners_count,
                "removed_ai_items": removed_count,
            })

        # 调用 ai_fill 补齐
        ok, msg, review = ai_fill_menu(menu_id, location=loc, seed=42)

        # 附加 reconcile 信息
        if review:
            review["reconciled"] = True
            review["reconcile_diners"] = diners_count
            review["reconcile_removed"] = removed_count

        return True, f"已根据 {diners_count} 人用餐重新调整 AI 推荐（删除 {removed_count} 道多余 AI 菜）", review
    finally:
        conn.close()


def repair_menu(menu_id, location="shenzhen", seed=None):
    """
    重新推荐 AI 菜品 (Refresh AI Suggestions)：
    只替换 source=ai (is_locked=0) 的菜，绝对不修改 owner (is_locked=1) 的菜。
    """
    conn = get_db()
    try:
        menu = conn.execute("SELECT date FROM menus WHERE id = ?", (menu_id,)).fetchone()
        if not menu:
            return False, "菜单不存在", None

        date_str = menu["date"]

        # 获取 owner (locked) 菜品
        locked_items = conn.execute(
            "SELECT dish_id, meal_type FROM menu_items WHERE menu_id = ? AND is_locked = 1",
            (menu_id,)
        ).fetchall()

        locked = {"breakfast": [], "lunch": [], "dinner": []}
        for item in locked_items:
            if item["meal_type"] in locked:
                locked[item["meal_type"]].append(item["dish_id"])

        # 删除所有非锁定 (AI) 菜品
        conn.execute(
            "DELETE FROM menu_items WHERE menu_id = ? AND is_locked = 0",
            (menu_id,)
        )
        conn.commit()

        # 重新生成（保留 locked）
        menu_id_new, review = generate_and_store_menu(
            date_str, location, seed=seed or 42, locked=locked
        )

        log_event("repair_menu", "menu", str(menu_id), {
            "review_passed": review["passed"],
            "locked_preserved": sum(len(v) for v in locked.values())
        })

        return True, "AI 菜品已重新推荐", review
    finally:
        conn.close()


def confirm_menu(menu_id, triggered_by="vivian", expected_location=None, include_transition=False):
    """V3: 确认菜单。Warning 不阻断 Confirm，VV 是唯一最终确认人。
    V11: 确认时记录 VV 偏好（record_vv_confirm），统计保留的菜品。
    同一菜单仅允许从 draft 首次进入 confirmed，重复请求不产生新确认版本。"""
    def result(ok, message, warnings=None, transitioned=False):
        values = (ok, message, warnings or [])
        return values + (transitioned,) if include_transition else values

    conn = get_db()
    try:
        conn.execute("BEGIN IMMEDIATE")
        menu = conn.execute(
            "SELECT date, location, status, diners, meal_mode, banquet_total_diners "
            "FROM menus WHERE id = ?",
            (menu_id,)
        ).fetchone()
        if not menu:
            conn.rollback()
            return result(False, "菜单不存在")
        if expected_location and menu["location"] != expected_location:
            conn.rollback()
            return result(False, "菜单厨房与当前厨房不一致")
        if menu["status"] in ("confirmed", "pushed"):
            conn.rollback()
            return result(True, "该菜单已经确认，未重复确认", transitioned=False)
        if menu["status"] != "draft":
            conn.rollback()
            return result(False, f"当前状态 {menu['status']} 不支持确认")

        # V11: 使用 _get_effective_diners_count 支持 banquet 模式
        diners_count = _get_effective_diners_count(menu_row=menu)

        menu_data = get_menu_with_dishes(menu["date"])

        # 重建 state 做 Final Review (V3: 只生成 Warning，不阻断)
        pool = _load_pool()
        dish_map = {d["id"]: d for d in pool["dishes"]}
        day_result = {}
        for mt in ["breakfast", "lunch", "dinner"]:
            state = MealState()
            for item in menu_data["meals"].get(mt, []):
                did = item["dish_id"]
                if did in dish_map:
                    analysis = NutritionAnalyzer.analyze(dish_map[did])
                    state.add_dish(analysis, is_locked=item["is_locked"])
            day_result[mt] = {"state": state}

        review = RuleEngine.final_review(day_result, diners_count)
        warnings = review.get("warnings", [])

        # V3: 无论是否有 warnings，都允许确认
        conn.execute(
            "UPDATE menus SET status = 'confirmed', confirmed_at = ?, "
            "auto_confirmed = 0, "
            "notes_zh = ?, notes_en = ?, push_status = 'not_sent', "
            "push_error = NULL, confirmed_revision = NULL WHERE id = ?",
            (datetime.now().isoformat(),
             "; ".join(warnings) if warnings else "",
             "",
             menu_id)
        )
        conn.commit()
        # Freeze the exact confirmed content revision before any delivery attempt.
        from push_service import load_menu_for_push, menu_revision
        revision = menu_revision(load_menu_for_push(menu_id))
        conn.execute("UPDATE menus SET confirmed_revision = ? WHERE id = ?", (revision, menu_id))
        conn.commit()
        log_event("menu_confirmed", "menu", str(menu_id), {
            "by": triggered_by,
            "warnings_count": len(warnings),
            "warnings": warnings
        })

        # V11: 记录 VV 偏好 — 统计 Confirm 时保留的菜品
        try:
            record_vv_confirm(menu_id)
        except Exception as e:
            log_event("vv_preferences_error", "menu", str(menu_id), {"error": str(e)})

        if warnings:
            return result(True, f"菜单已确认（有 {len(warnings)} 项提示）", warnings, transitioned=True)
        return result(True, "菜单已确认", transitioned=True)
    finally:
        conn.close()


def get_tomorrow_date():
    return (date.today() + timedelta(days=1)).isoformat()


def revert_to_draft(menu_id):
    """V3: 将 confirmed 菜单回退到 draft，支持 Edit Menu → Reconfirm 流程。"""
    conn = get_db()
    try:
        menu = conn.execute("SELECT status, date FROM menus WHERE id = ?", (menu_id,)).fetchone()
        if not menu:
            return False, "菜单不存在"
        if menu["status"] not in ("confirmed", "pushed"):
            return False, f"当前状态 {menu['status']} 不支持回退"

        conn.execute(
            "UPDATE menus SET status = 'draft', confirmed_at = NULL, "
            "confirmed_revision = NULL, push_status = 'not_sent', push_error = NULL WHERE id = ?",
            (menu_id,)
        )
        conn.commit()
        log_event("menu_reverted_to_draft", "menu", str(menu_id), {
            "previous_status": menu["status"]
        })
        return True, "菜单已回退到草稿，可修改后重新确认"
    finally:
        conn.close()


def push_menu(menu_id):
    """兼容旧调用；实际发送与状态持久化统一由 PushService 完成。"""
    from push_service import push_confirmed_menu
    return push_confirmed_menu(menu_id)


def ensure_tomorrow_menu(location="shenzhen", seed=None):
    """确保明天菜单存在，不存在则生成"""
    tomorrow = get_tomorrow_date()
    conn = get_db()
    menu = conn.execute("SELECT id FROM menus WHERE date = ?", (tomorrow,)).fetchone()
    conn.close()

    if not menu:
        menu_id, review = generate_and_store_menu(tomorrow, location, seed=seed or 42)
        return menu_id, review, True  # newly generated
    else:
        return menu["id"], None, False  # already exists
