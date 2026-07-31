#!/usr/bin/env python3
"""
家庭菜单管家 - 菜单服务层
连接 rule_engine.py (配餐引擎) 和 SQLite (数据存储)。
提供：生成菜单、增删改查菜品、AI补齐、重新搭配、锁定、确认。
"""

import json
import random
from datetime import date, datetime, timedelta
from db import get_db, log_event
from rule_engine import (
    GapFiller, RuleEngine, NutritionAnalyzer, MealState,
    generate_afternoon_snack, get_dish_ingredients_map,
    get_history_3day, get_history_7day, get_inventory_ingredients,
    analyze_meal_slots, filter_candidates_for_slot,
)
from inventory import check_shortages, get_available_ingredient_ids, check_dishes_availability_batch


def _load_pool():
    """V6: 从 SQLite 加载菜品池（Single Source of Truth），不再读 dish_pool.json。
    只加载 is_active=1 的菜品。"""
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
        return {"dishes": dishes}
    finally:
        conn.close()


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
    inv_avail, inv_pri, inv_exp = get_inventory_ingredients(location)

    context = {
        "history_3day": get_history_3day(),
        "history_7day": get_history_7day(),
        "inventory_ingredients": inv_avail,
        "priority_ingredients": inv_pri,
        "expiring_ingredients": inv_exp,
        "dish_ingredients": dish_ings,
    }

    # 生成三餐
    gf = GapFiller(pool, seed=seed, dish_ingredients=dish_ings)
    result, logs = gf.generate_day(locked=locked, context=context)

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
        # 获取当前最大 sort_order
        row = conn.execute(
            "SELECT MAX(sort_order) as max_sort FROM menu_items WHERE menu_id = ? AND meal_type = ?",
            (menu_id, meal_type)
        ).fetchone()
        sort = (row["max_sort"] or 0) + 1

        # Owner 添加 = 自动锁定
        conn.execute(
            "INSERT INTO menu_items (menu_id, dish_id, meal_type, is_locked, locked_by, locked_at, sort_order) "
            "VALUES (?, ?, ?, 1, 'owner', ?, ?)",
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
    V8: AI 补充缺少菜品 (AI Fill Gaps)：
    保留当前所有菜（owner + AI），先分析槽位缺口，只补 missing_min > 0 的槽位。
    Available Now 优先；无 Available 不自动加入缺货菜；无候选返回 unmet_slot。
    meal_type: 指定餐次，None = 所有餐次。
    返回: (ok, msg, review) — review 包含 unmet_slots 信息。
    """
    conn = get_db()
    try:
        menu = conn.execute("SELECT date, location FROM menus WHERE id = ?", (menu_id,)).fetchone()
        if not menu:
            return False, "菜单不存在", None

        date_str = menu["date"]
        loc = menu["location"] or location
        pool = _load_pool()
        dish_ings = get_dish_ingredients_map()
        inv_avail, inv_pri, inv_exp = get_inventory_ingredients(loc)

        context = {
            "history_3day": get_history_3day(),
            "history_7day": get_history_7day(),
            "inventory_ingredients": inv_avail,
            "priority_ingredients": inv_pri,
            "expiring_ingredients": inv_exp,
            "dish_ingredients": dish_ings,
        }

        gf = GapFiller(pool, seed=seed or 42, dish_ingredients=dish_ings)
        dish_map = {d["id"]: d for d in pool["dishes"]}

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

        # 获取用餐人数
        diners_count = 4  # 默认
        try:
            diner_row = conn.execute("SELECT diners FROM menus WHERE id = ?", (menu_id,)).fetchone()
            if diner_row and diner_row["diners"]:
                diner_ids = json.loads(diner_row["diners"])
                diners_count = max(len(diner_ids), 1)
        except Exception:
            pass

        # 确定要处理的餐次
        target_meals = [meal_type] if meal_type else ["breakfast", "lunch", "dinner"]

        day_history = set()
        day_proteins = set()
        new_items_added = 0
        unmet_slots = []  # V8: 记录无法补齐的槽位

        for mt in ["breakfast", "lunch", "dinner"]:
            for did in meals_existing[mt]:
                if did in dish_map:
                    day_history.add(did)
                    day_proteins.update(dish_map[did].get("protein_types", []))

        for mt in target_meals:
            if mt not in meals_existing:
                continue

            existing_ids = [did for did in meals_existing[mt] if did in dish_map]
            if not existing_ids:
                continue

            # 构建 MealState
            state = MealState()
            for did in existing_ids:
                analysis = NutritionAnalyzer.analyze(dish_map[did])
                state.add_dish(analysis, is_locked=True)

            # V8: 槽位分析
            if mt in ("dinner", "lunch"):
                # 晚餐/午餐：使用 V8 槽位分析 + Available Now 优先
                added = _fill_missing_slots_v8(
                    conn, menu_id, mt, state, gf, dish_map, context,
                    day_history, day_proteins, diners_count, loc,
                    unmet_slots
                )
                new_items_added += added
            else:
                # 早餐：保持使用 generate_meal（已修复 is_satisfied + _filter_by_gaps）
                if RuleEngine.is_satisfied(mt, state):
                    continue

                meal_ctx = dict(context)
                meal_ctx["day_proteins"] = set(day_proteins)
                meal_ctx["day_history"] = set(day_history)

                dishes, new_state, log = gf.generate_meal(
                    mt, locked_dish_ids=existing_ids, context=meal_ctx
                )

                existing_set = set(existing_ids)
                new_dishes = [d for d in dishes if d["id"] not in existing_set]

                row = conn.execute(
                    "SELECT MAX(sort_order) as max_sort FROM menu_items WHERE menu_id = ? AND meal_type = ?",
                    (menu_id, mt)
                ).fetchone()
                sort = (row["max_sort"] or 0) + 1

                for d in new_dishes:
                    if not d.get("id") or d["id"] == "None":
                        continue
                    conn.execute(
                        "INSERT INTO menu_items (menu_id, dish_id, meal_type, is_locked, sort_order, source) "
                        "VALUES (?, ?, ?, 0, ?, ?)",
                        (menu_id, d["id"], mt, sort, "ai")
                    )
                    sort += 1
                    new_items_added += 1
                    day_history.add(d["id"])
                    day_proteins.update(d.get("protein_types", []))

        conn.commit()

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

        review = RuleEngine.final_review(day_result)
        review["unmet_slots"] = unmet_slots  # V8: 附加 unmet_slot 信息

        log_event("ai_fill_menu", "menu", str(menu_id), {
            "new_items_added": new_items_added,
            "review_passed": review["passed"],
            "unmet_slots": unmet_slots,
        })

        return True, f"AI 补充了 {new_items_added} 道菜", review
    finally:
        conn.close()


def _fill_missing_slots_v8(conn, menu_id, meal_type, state, gf, dish_map, context,
                            day_history, day_proteins, diners_count, location,
                            unmet_slots):
    """
    V8: 槽位分析 + Available Now 优先补齐。
    用于 dinner 和 lunch。
    返回: items_added (int)
    """
    items_added = 0
    max_rounds = 6  # 安全上限

    for round_i in range(max_rounds):
        # Step 1: 分析当前槽位
        slots = analyze_meal_slots(meal_type, state, diners_count)

        # Step 2: 找 missing_min > 0 的槽位
        missing = {k: v for k, v in slots.items() if v["missing_min"] > 0}
        if not missing:
            break  # 所有最低槽位已满足

        # Step 3: 逐个补齐缺失槽位
        for slot_name, slot_info in missing.items():
            # 获取该槽位的候选菜
            all_candidates = gf.get_candidates(meal_type, exclude_ids=day_history)
            slot_candidates = filter_candidates_for_slot(all_candidates, slot_name)

            if not slot_candidates:
                unmet_slots.append({
                    "meal": meal_type,
                    "slot": slot_name,
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
                # ScoringEngine 已处理 Expiring > Use First > normal 排序
                meal_ctx = dict(context)
                meal_ctx["day_proteins"] = set(day_proteins)
                meal_ctx["day_history"] = set(day_history)

                scored = []
                for c in available_candidates:
                    s = gf.scorer.score_dish(c, state, meal_type, meal_ctx)
                    scored.append((s, c))
                scored.sort(key=lambda x: x[0], reverse=True)

                chosen = scored[0][1]

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

                # 更新 state
                state.add_dish(chosen, is_locked=False)
            else:
                # V8: 无 Available → 检查 Almost Available
                almost_candidates = [
                    c for c in slot_candidates
                    if avail_batch.get(c["id"], {}).get("status") == "almost_available"
                ]
                if almost_candidates:
                    unmet_slots.append({
                        "meal": meal_type,
                        "slot": slot_name,
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
                        "message": (
                            f"当前库存没有可直接制作的{slot_name}菜品，请手动添加。"
                            f" / No {slot_name} is fully available. Please add manually."
                        ),
                    })

    return items_added


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


def confirm_menu(menu_id):
    """V3: 确认菜单。Warning 不阻断 Confirm，VV 是唯一最终确认人。"""
    conn = get_db()
    try:
        menu = conn.execute("SELECT date FROM menus WHERE id = ?", (menu_id,)).fetchone()
        if not menu:
            return False, "菜单不存在"

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

        review = RuleEngine.final_review(day_result)
        warnings = review.get("warnings", [])

        # V3: 无论是否有 warnings，都允许确认
        conn.execute(
            "UPDATE menus SET status = 'confirmed', confirmed_at = ?, "
            "auto_confirmed = 0, "
            "notes_zh = ?, notes_en = ? WHERE id = ?",
            (datetime.now().isoformat(),
             "; ".join(warnings) if warnings else "",
             "",
             menu_id)
        )
        conn.commit()
        log_event("menu_confirmed", "menu", str(menu_id), {
            "by": "vv",
            "warnings_count": len(warnings),
            "warnings": warnings
        })

        if warnings:
            return True, f"菜单已确认（有 {len(warnings)} 项提示）", warnings
        return True, "菜单已确认", []
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
            "UPDATE menus SET status = 'draft', confirmed_at = NULL WHERE id = ?",
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
    """V3: 推送菜单。只有 VV confirmed 的菜单才能推送。"""
    conn = get_db()
    try:
        menu = conn.execute("SELECT status, date FROM menus WHERE id = ?", (menu_id,)).fetchone()
        if not menu:
            return False, "菜单不存在"
        if menu["status"] != "confirmed":
            return False, f"菜单状态为 {menu['status']}，只有 VV confirmed 才能推送"

        conn.execute(
            "UPDATE menus SET status = 'pushed', pushed_at = ? WHERE id = ?",
            (datetime.now().isoformat(), menu_id)
        )
        conn.commit()
        log_event("menu_pushed", "menu", str(menu_id), {
            "pushed_by": "system",
            "trigger": "vv_confirmed"
        })
        return True, "菜单已推送"
    finally:
        conn.close()


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
