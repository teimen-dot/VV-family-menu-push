#!/usr/bin/env python3
"""
家庭菜单管家 - H5 核心应用 (v2.0 重构版)
4个页面：Tomorrow(明日菜单) / Pantry(家中食材) / Dishes(菜品库) / History(历史菜单)
全站双语同屏，location 切换，交互式菜单管理，微信内嵌浏览器优先。
"""

import json
import os
from datetime import date, datetime, timedelta
from http.server import ThreadingHTTPServer, BaseHTTPRequestHandler
from urllib.parse import urlparse, parse_qs
from db import get_db, log_event
from inventory import (
    get_latest_inventory, submit_inventory,
    get_available_ingredient_ids, check_shortages,
    create_purchase_requests, get_purchase_requests,
    update_purchase_status,
    save_pantry_changes, get_current_pantry,
    check_dish_availability, check_dishes_availability_batch,
    check_dish_availability_debug,
    get_common_ingredients_static,
    add_ingredient_to_pantry, remove_ingredient_from_pantry,
    update_ingredient_status, confirm_pantry_unchanged,
    is_ingredient_in_pantry,
    _invalidate_availability_cache, _increment_inventory_version,
    get_inventory_version,
)
from menu_service import (
    get_menu_with_dishes, add_dish_to_menu, remove_dish_from_menu,
    replace_dish_in_menu, lock_dish, ai_fill_menu, repair_menu,
    confirm_menu, generate_and_store_menu, ensure_tomorrow_menu,
    get_tomorrow_date, revert_to_draft, push_menu,
)

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
PORT = 8090
LOCATIONS = {"shenzhen": "深圳 Shenzhen", "hongkong": "香港 Hong Kong"}


# ============================================================
# 数据查询
# ============================================================

def get_all_dishes(category=None, search=""):
    """V6: 只返回 is_active=1 的菜品（Single Source of Truth）"""
    conn = get_db()
    try:
        query = "SELECT * FROM dishes WHERE (is_active = 1 OR is_active IS NULL)"
        params = []
        if category:
            query += " AND category_id = ?"
            params.append(category)
        if search:
            query += " AND (name_cn LIKE ? OR name_en LIKE ?)"
            params.extend([f"%{search}%", f"%{search}%"])
        query += " ORDER BY category_id, name_cn"
        rows = conn.execute(query, params).fetchall()
        return [dict(r) for r in rows]
    finally:
        conn.close()


def get_dish_recommendations(meal_type, current_dish_id, category_id, location):
    """V7: 智能换菜推荐。返回 {available: [...], almost_available: [...]}"""
    conn = get_db()
    try:
        # 1. 获取所有 active 菜品
        all_dishes = conn.execute(
            "SELECT id, name_cn, name_en, category_id, image, meal_tags, "
            "protein_types, vegetables, cooking_methods, carb_type, taste "
            "FROM dishes WHERE (is_active = 1 OR is_active IS NULL) "
            "ORDER BY category_id, name_cn"
        ).fetchall()

        # 2. 过滤：meal_tags 包含当前餐别 + 排除当前菜
        candidates = []
        for d in all_dishes:
            if d["id"] == current_dish_id:
                continue
            meal_tags = []
            if d["meal_tags"]:
                try:
                    meal_tags = json.loads(d["meal_tags"])
                except (json.JSONDecodeError, TypeError):
                    meal_tags = []
            if meal_type in meal_tags:
                candidates.append(dict(d))

        if not candidates:
            return {"available": [], "almost_available": []}

        # 3. 批量检查 availability
        dish_ids = [c["id"] for c in candidates]
        avail_batch = check_dishes_availability_batch(dish_ids, location)

        # 4. 获取 pantry 状态（priority_use / expiring 食材集合）
        pantry = get_current_pantry(location)
        priority_ings = set()
        expiring_ings = set()
        for item in pantry.get("items", []):
            if item["status"] == "priority_use":
                priority_ings.add(item["ingredient_id"])
            elif item["status"] == "expiring":
                expiring_ings.add(item["ingredient_id"])

        # 5. 获取过去 3 天的菜品 ID（历史去重）
        past_3d = (date.today() - timedelta(days=3)).isoformat()
        today = date.today().isoformat()
        recent_menus = conn.execute(
            "SELECT mi.dish_id FROM menu_items mi "
            "JOIN menus m ON mi.menu_id = m.id "
            "WHERE m.date >= ? AND m.date < ?",
            (past_3d, today)
        ).fetchall()
        recent_dish_ids = set(r["dish_id"] for r in recent_menus if r["dish_id"])

        # 6. 获取当前菜的信息（用于 cooking_methods 对比）
        current_dish = conn.execute(
            "SELECT cooking_methods, protein_types FROM dishes WHERE id = ?",
            (current_dish_id,)
        ).fetchone()
        current_cooking = set()
        current_proteins = set()
        if current_dish:
            if current_dish["cooking_methods"]:
                try:
                    current_cooking = set(json.loads(current_dish["cooking_methods"]))
                except (json.JSONDecodeError, TypeError):
                    pass
            if current_dish["protein_types"]:
                try:
                    current_proteins = set(json.loads(current_dish["protein_types"]))
                except (json.JSONDecodeError, TypeError):
                    pass

        # 7. 获取当前餐已有蛋白质（用于去重惩罚）
        # 从 menu_items 获取今天的菜单
        tomorrow = get_tomorrow_date()
        tomorrow_menu = conn.execute(
            "SELECT id FROM menus WHERE date = ?", (tomorrow,)
        ).fetchone()
        meal_proteins = set()
        if tomorrow_menu:
            meal_items = conn.execute(
                "SELECT d.protein_types FROM menu_items mi "
                "JOIN dishes d ON mi.dish_id = d.id "
                "WHERE mi.menu_id = ? AND mi.meal_type = ?",
                (tomorrow_menu["id"], meal_type)
            ).fetchall()
            for mi in meal_items:
                if mi["protein_types"]:
                    try:
                        for p in json.loads(mi["protein_types"]):
                            meal_proteins.add(p)
                    except (json.JSONDecodeError, TypeError):
                        pass

        # 8. 获取每道菜的食材（用于 priority/expiring 评分）
        dish_ingredients_map = {}
        placeholders = ",".join("?" * len(dish_ids))
        di_rows = conn.execute(
            f"SELECT dish_id, ingredient_id FROM dish_ingredients WHERE dish_id IN ({placeholders})",
            dish_ids
        ).fetchall()
        for row in di_rows:
            if row["dish_id"] not in dish_ingredients_map:
                dish_ingredients_map[row["dish_id"]] = set()
            dish_ingredients_map[row["dish_id"]].add(row["ingredient_id"])

        # 9. 评分
        available_list = []
        almost_list = []

        for c in candidates:
            did = c["id"]
            avail = avail_batch.get(did, {})
            status = avail.get("status", "incomplete")

            # 解析 JSON 字段
            c_proteins = set()
            if c["protein_types"]:
                try:
                    c_proteins = set(json.loads(c["protein_types"]))
                except (json.JSONDecodeError, TypeError):
                    pass
            c_cooking = set()
            if c["cooking_methods"]:
                try:
                    c_cooking = set(json.loads(c["cooking_methods"]))
                except (json.JSONDecodeError, TypeError):
                    pass

            score = 0

            # 同 category/role: +60 (V7: 角色优先)
            if c["category_id"] == category_id:
                score += 60

            # priority_use 食材: +30
            dish_ings = dish_ingredients_map.get(did, set())
            if dish_ings & priority_ings:
                score += 30

            # expiring 食材: +50
            if dish_ings & expiring_ings:
                score += 50

            # 过去3天没吃过: +20
            if did not in recent_dish_ids:
                score += 20

            # 与当前菜做法不同: +10
            if current_cooking and c_cooking and not (current_cooking & c_cooking):
                score += 10

            # 与当前餐已有蛋白质重复: -10
            if c_proteins and meal_proteins and (c_proteins & meal_proteins):
                score -= 10

            # 构建返回对象
            item = {
                "id": did,
                "name_cn": c["name_cn"],
                "name_en": c["name_en"] or "",
                "category_id": c["category_id"],
                "image": c["image"],
                "availability": status,
                "missing_required": [m["name_cn"] for m in avail.get("missing_required", [])],
                "missing_required_en": [m.get("name_en", "") for m in avail.get("missing_required", [])],
                "score": score,
            }

            if status == "available":
                available_list.append(item)
            elif status == "almost_available":
                almost_list.append(item)
            # missing / incomplete 不进入推荐

        # 10. 排序并截断
        available_list.sort(key=lambda x: x["score"], reverse=True)
        almost_list.sort(key=lambda x: x["score"], reverse=True)

        return {
            "available": available_list[:6],
            "almost_available": almost_list[:4],
        }
    finally:
        conn.close()


def get_dish_detail(dish_id):
    conn = get_db()
    try:
        dish = conn.execute("SELECT * FROM dishes WHERE id = ?", (dish_id,)).fetchone()
        if not dish:
            return None
        d = dict(dish)
        for field in ["protein_types", "vegetables", "meal_tags", "cooking_methods", "custom_tags"]:
            if d.get(field):
                try:
                    d[field] = json.loads(d[field])
                except (json.JSONDecodeError, TypeError):
                    d[field] = []
            else:
                d[field] = []
        # 食材
        ings = conn.execute(
            "SELECT i.ingredient_id, i.name_cn, i.name_en "
            "FROM dish_ingredients di JOIN ingredients i ON di.ingredient_id = i.ingredient_id "
            "WHERE di.dish_id = ?", (dish_id,)
        ).fetchall()
        d["ingredients"] = [dict(r) for r in ings]
        return d
    finally:
        conn.close()


def get_categories():
    conn = get_db()
    try:
        rows = conn.execute(
            "SELECT id, label_cn, label_en FROM categories WHERE active = 1 ORDER BY sort_order"
        ).fetchall()
        return [dict(r) for r in rows]
    finally:
        conn.close()


def get_all_diners():
    """获取所有用餐成员"""
    conn = get_db()
    try:
        rows = conn.execute("SELECT * FROM diners ORDER BY sort_order, id").fetchall()
        return [dict(r) for r in rows]
    finally:
        conn.close()


def get_menu_diners(menu_id):
    """获取菜单已设置的用餐成员"""
    conn = get_db()
    try:
        row = conn.execute("SELECT diners FROM menus WHERE id = ?", (menu_id,)).fetchone()
        if not row or not row["diners"]:
            return []
        try:
            return json.loads(row["diners"])
        except (json.JSONDecodeError, TypeError):
            return []
    finally:
        conn.close()


def update_menu_diners(menu_id, diners_list):
    """更新菜单的用餐成员"""
    conn = get_db()
    try:
        diners_json = json.dumps(diners_list, ensure_ascii=False)
        conn.execute(
            "UPDATE menus SET diners = ?, diners_count = ?, updated_at = datetime('now') WHERE id = ?",
            (diners_json, len(diners_list), menu_id)
        )
        conn.commit()
        log_event("diners_updated", "menu", str(menu_id), {"diners": diners_list})
        return True
    finally:
        conn.close()


def get_all_ingredients():
    """获取所有食材（含 aliases 和 ingredient_group）"""
    conn = get_db()
    try:
        rows = conn.execute(
            "SELECT ingredient_id, name_cn, name_en, aliases, category, ingredient_group "
            "FROM ingredients ORDER BY ingredient_group, name_cn"
        ).fetchall()
        result = []
        for r in rows:
            d = dict(r)
            # Parse aliases JSON
            try:
                d["aliases"] = json.loads(d.get("aliases") or "[]")
            except (json.JSONDecodeError, TypeError):
                d["aliases"] = []
            result.append(d)
        return result
    finally:
        conn.close()


def _load_dish_name_map():
    """加载 dish_pool.json 建立中文菜名→英文名映射（用于历史菜单英文补全）"""
    try:
        with open(os.path.join(BASE_DIR, "dish_pool.json"), "r", encoding="utf-8") as f:
            pool = json.load(f)
        return {d["name_cn"]: d.get("name_en", "") for d in pool.get("dishes", []) if d.get("name_cn")}
    except Exception:
        return {}


def _split_text_dishes(text):
    """拆分历史菜单中的文本 dish_id 为单个菜名列表。
    处理分隔符： ＋ / + / / (斜杠替代)
    """
    if not text:
        return []
    # 先按 ＋ 或 + 拆分
    parts = text.replace("＋", "+").split("+")
    result = []
    for p in parts:
        p = p.strip()
        if not p:
            continue
        # 处理 " / " 斜杠替代选项（如 "清蒸银鳕鱼 / 清蒸新鲜鱼片"）
        if " / " in p:
            # 取第一个选项，但保留完整文本用于显示
            result.append(p)
        else:
            result.append(p)
    return result


def _lookup_english_name(cn_name, name_map):
    """查中文菜名对应的英文名，支持模糊匹配"""
    if not cn_name:
        return ""
    # 精确匹配
    if cn_name in name_map:
        return name_map[cn_name]
    # 去掉括号内容后精确匹配
    import re
    clean = re.sub(r"[（(].*?[)）]", "", cn_name).strip()
    if clean in name_map:
        return name_map[clean]
    # 模糊匹配（包含关系）
    for cn, en in name_map.items():
        if cn_name in cn or cn in cn_name:
            return en
    # 分词模糊匹配：菜名包含映射key的主要部分
    for cn, en in name_map.items():
        # 去掉空格后比较
        cn_compact = cn.replace(" ", "")
        if cn_compact and (cn_compact in cn_name.replace(" ", "") or cn_name.replace(" ", "") in cn_compact):
            return en
    # 逐词匹配：菜名中包含映射key的任一关键词（>=2字）
    for cn, en in name_map.items():
        for part in cn.split():
            part = part.strip()
            if len(part) >= 2 and part in cn_name:
                return en
    return ""


def get_history_menus(days=30):
    conn = get_db()
    try:
        today = date.today().isoformat()
        past = (date.today() - timedelta(days=days)).isoformat()
        menus = conn.execute(
            "SELECT * FROM menus WHERE date >= ? AND date < ? ORDER BY date DESC",
            (past, today)
        ).fetchall()

        # 加载菜名映射用于历史数据英文补全
        dish_name_map = _load_dish_name_map()

        result = []
        for m in menus:
            items = conn.execute(
                "SELECT mi.*, d.name_cn, d.name_en, d.image "
                "FROM menu_items mi "
                "LEFT JOIN dishes d ON mi.dish_id = d.id "
                "WHERE mi.menu_id = ? ORDER BY mi.meal_type, mi.sort_order",
                (m["id"],)
            ).fetchall()

            meals = {"breakfast": [], "lunch": [], "afternoon_snack": [], "dinner": []}
            for item in items:
                mt = item["meal_type"]
                if mt not in meals:
                    continue

                dish_id = item["dish_id"] or ""
                is_text = not dish_id.startswith("dish_")

                if is_text:
                    # 历史数据：dish_id 是整餐菜名文本，需拆分
                    dish_names = _split_text_dishes(dish_id)
                    for dn in dish_names:
                        # 处理斜杠替代选项：取第一个作为主菜名
                        display_cn = dn
                        lookup_cn = dn.split(" / ")[0].strip() if " / " in dn else dn
                        en = _lookup_english_name(lookup_cn, dish_name_map)
                        meals[mt].append({
                            "name_cn": display_cn,
                            "name_en": en,
                            "image": "",
                            "is_locked": bool(item["is_locked"]),
                        })
                else:
                    # 新数据：真实 dish_id，JOIN 成功
                    name_cn = item["name_cn"] or dish_id
                    name_en = item["name_en"] or ""
                    # 如果英文名仍为空，尝试从映射补全
                    if not name_en:
                        name_en = _lookup_english_name(name_cn, dish_name_map)
                    meals[mt].append({
                        "name_cn": name_cn,
                        "name_en": name_en,
                        "image": item["image"] or "",
                        "is_locked": bool(item["is_locked"]),
                    })

            # V3: 确认来源（取消 auto_confirmed）
            confirmed_source = "待确认 Pending"
            if m["confirmed_at"]:
                confirmed_source = "VV 确认 VV Confirmed"
            elif m["status"] == "draft":
                confirmed_source = "待确认 Pending"

            # Push status
            push_status = "未推送 Not Pushed"
            if m["pushed_at"]:
                push_status = "已推送 Pushed"

            result.append({
                "date": m["date"],
                "status": m["status"],
                "location": m["location"],
                "confirmed_source": confirmed_source,
                "push_status": push_status,
                "meals": meals
            })
        return result
    finally:
        conn.close()


def get_last_inventory_items(location):
    """获取上次库存的食材列表（用于'沿用上次'）"""
    inv = get_latest_inventory(location)
    if not inv:
        return []
    return [{"ingredient_id": i["ingredient_id"], "name_cn": i["name_cn"],
             "name_en": i.get("name_en", ""), "status": i["status"]}
            for i in inv["items"]]


def get_menu_purchase_requests(menu_id):
    """获取某菜单的采购任务，按食材分组"""
    conn = get_db()
    try:
        menu = conn.execute("SELECT date, location FROM menus WHERE id = ?", (menu_id,)).fetchone()
        if not menu:
            return []
        reqs = conn.execute(
            "SELECT pr.*, i.name_cn as ingredient_name, i.name_en as ingredient_name_en, "
            "d.name_cn as dish_name "
            "FROM purchase_requests pr "
            "LEFT JOIN ingredients i ON pr.ingredient_id = i.ingredient_id "
            "LEFT JOIN dishes d ON pr.dish_id = d.id "
            "WHERE pr.menu_date = ? AND pr.location = ? "
            "ORDER BY pr.status, pr.created_at",
            (menu["date"], menu["location"])
        ).fetchall()
        return [dict(r) for r in reqs]
    finally:
        conn.close()


def get_dish_availability(dish_ids, location):
    """V6: 检查菜品食材可用性（使用统一 InventoryService）。
    返回 {dish_id: {available, missing_count, missing_names, total_ingredients, status}}"""
    if not dish_ids:
        return {}
    result = {}
    batch = check_dishes_availability_batch(dish_ids, location)
    for did, avail in batch.items():
        result[did] = {
            "available": avail["status"] == "available",
            "missing_count": len(avail["missing_required"]),
            "missing_names": [m["name_cn"] for m in avail["missing_required"]],
            "total_ingredients": len(avail["required"]),
            "status": avail["status"],
        }
    return result


def get_common_ingredients():
    """V4: 常用食材（is_common 字段，独立于当前库存）"""
    return get_common_ingredients_static()


# ============================================================
# HTML/CSS/JS 共享模板
# ============================================================

CSS = """
*{margin:0;padding:0;box-sizing:border-box}
body{font-family:-apple-system,BlinkMacSystemFont,"Segoe UI","PingFang SC","Hiragino Sans GB",sans-serif;background:#faf7f2;color:#2c2620;line-height:1.6;font-size:16px}
.header{background:#2c2620;color:#faf7f2;padding:14px 16px;display:flex;align-items:center;justify-content:space-between;position:sticky;top:0;z-index:100}
.header h1{font-size:22px;font-weight:700;letter-spacing:.5px}
.loc-switch{display:flex;gap:4px}
.loc-btn{font-size:14px;padding:5px 12px;border-radius:14px;border:1px solid #a89888;background:transparent;color:#a89888;cursor:pointer}
.loc-btn.active{background:#a89888;color:#2c2620;border-color:#a89888}
.role-badge{font-size:13px;background:#a89888;padding:3px 10px;border-radius:10px}
.nav{display:flex;background:#fff;border-bottom:1px solid #e8e0d4;position:sticky;top:62px;z-index:99}
.nav a{flex:1;text-align:center;padding:12px 2px;font-size:17px;color:#a89888;text-decoration:none;border-bottom:2px solid transparent;font-weight:600}
.nav a.active{color:#2c2620;border-bottom-color:#2c2620}
.nav a span{display:block;font-size:13px;margin-top:1px;opacity:.6}
.content{max-width:600px;margin:0 auto;padding:12px}
.meal-section{background:#fff;border-radius:12px;margin-bottom:10px;overflow:hidden;box-shadow:0 1px 3px rgba(0,0,0,.05)}
.meal-header{padding:12px 14px;display:flex;align-items:center;gap:8px}
.meal-bar{width:4px;height:24px;border-radius:2px}
.meal-title{font-size:22px;font-weight:700}
.meal-title-en{font-size:15px;color:#a89888;font-style:italic;margin-left:4px}
.meal-actions{margin-left:auto;display:flex;gap:6px}
.meal-act-btn{font-size:15px;padding:8px 14px;border-radius:8px;border:1px solid #d4c9b8;background:#faf7f2;color:#5a4a3a;cursor:pointer;white-space:nowrap;min-height:44px;display:flex;align-items:center}
.meal-act-btn:active{background:#e8e0d4}
.meal-items{padding:0 14px 8px}
.meal-item{display:flex;gap:10px;padding:10px 0;border-bottom:1px solid #f5f0e8;align-items:center}
.meal-item:last-child{border-bottom:none}
.meal-item img{width:56px;height:56px;border-radius:8px;object-fit:cover;flex-shrink:0;background:#f5f0e8}
.meal-item .no-img{width:56px;height:56px;border-radius:8px;background:#f5f0e8;flex-shrink:0;display:flex;align-items:center;justify-content:center;font-size:24px}
.meal-item .info{flex:1;min-width:0}
.dish-name{font-size:18px;font-weight:600}
.dish-name-en{font-size:14px;color:#a89888;font-style:italic}
.dish-meta{font-size:12px;color:#a89888;margin-top:2px}
.badge{display:inline-block;font-size:12px;padding:2px 6px;border-radius:4px;margin-right:3px;vertical-align:middle}
.badge-owner{background:#d4edda;color:#155724}
.badge-ai{background:#d1ecf1;color:#0c5460}
.badge-shortage-missing{background:#f8d7da;color:#721c24}
.badge-shortage-tobuy{background:#fff3cd;color:#856404}
.badge-shortage-notified{background:#cce5ff;color:#004085}
.badge-shortage-purchased{background:#d4edda;color:#155724}
.badge-cat{background:#e8e0d4;color:#5a4a3a}
.badge-warning{background:#fff3cd;color:#856404}
.item-actions{display:flex;gap:6px;flex-shrink:0}
.item-btn{width:44px;height:44px;border-radius:8px;border:1px solid #d4c9b8;background:#faf7f2;color:#5a4a3a;cursor:pointer;font-size:18px;display:flex;align-items:center;justify-content:center}
.item-btn:active{background:#e8e0d4}
.item-btn.danger{border-color:#f5c6cb;color:#721c24}
.card{background:#fff;border-radius:12px;padding:14px;margin-bottom:10px;box-shadow:0 1px 3px rgba(0,0,0,.05)}
.card h3{font-size:16px;margin-bottom:6px}
.card p{font-size:14px;color:#5a4a3a}
.empty{text-align:center;padding:40px 20px;color:#a89888}
.empty h2{font-size:18px;margin-bottom:6px}
.btn{display:block;width:100%;padding:14px;text-align:center;border:none;border-radius:8px;font-size:16px;font-weight:500;cursor:pointer;text-decoration:none;min-height:44px}
.btn-primary{background:#2c2620;color:#faf7f2}
.btn-outline{background:transparent;border:1px solid #2c2620;color:#2c2620}
.btn-danger{background:#c0504c;color:#fff}
.btn:active{opacity:.7}
.btn:disabled{opacity:.4;cursor:not-allowed}
.status-tag{display:inline-block;font-size:13px;padding:3px 10px;border-radius:10px}
.status-draft{background:#fff3cd;color:#856404}
.status-confirmed{background:#d4edda;color:#155724}
.status-pushed{background:#d1ecf1;color:#0c5460}
.loc-banner{background:#e8e0d4;color:#5a4a3a;padding:8px 16px;font-size:14px;text-align:center;font-weight:500}
.search-bar{padding:10px 14px;background:#fff;position:sticky;top:118px;z-index:98}
.search-bar input{width:100%;padding:12px 16px;border:1px solid #e8e0d4;border-radius:24px;font-size:16px;outline:none;box-sizing:border-box}
.filter-tabs{display:flex;flex-wrap:wrap;gap:6px;padding:6px 14px;background:#fff;border-bottom:1px solid #f5f0e8}
.filter-tab{white-space:nowrap;padding:6px 14px;border-radius:16px;font-size:14px;background:#f5f0e8;color:#5a4a3a;cursor:pointer}
.filter-tab.active{background:#2c2620;color:#faf7f2}
.dish-grid{display:grid;grid-template-columns:1fr 1fr;gap:8px;padding:4px 0}
.dish-card{background:#fff;border-radius:10px;overflow:hidden;box-shadow:0 1px 2px rgba(0,0,0,.03);cursor:pointer}
.dish-card img{width:100%;aspect-ratio:1.2;object-fit:cover;background:#f5f0e8}
.dish-card .no-img{width:100%;aspect-ratio:1.2;background:#f5f0e8;display:flex;align-items:center;justify-content:center;font-size:28px}
.dish-card .info{padding:8px}
.dish-card .name{font-size:18px;font-weight:600;overflow:hidden;text-overflow:ellipsis;white-space:nowrap}
.dish-card .name-en{font-size:14px;color:#a89888;font-style:italic;overflow:hidden;text-overflow:ellipsis;white-space:nowrap}
.dish-card .cat-label{font-size:13px;color:#a89888;margin-top:2px}
.dish-card .avail-badge{font-size:13px;padding:2px 6px;border-radius:4px;margin-top:3px;display:inline-block}
.avail-yes{background:#d4edda;color:#155724}
.avail-almost{background:#fff3cd;color:#856404}
.pantry-item{display:flex;align-items:center;justify-content:space-between;padding:12px 0;border-bottom:1px solid #f5f0e8;gap:8px}
.pantry-item:last-child{border-bottom:none}
.pantry-item .name{font-size:18px;font-weight:600}
.pantry-item .name-en{font-size:14px;color:#a89888}
.pantry-controls{display:flex;align-items:center;gap:12px;flex-shrink:0}
.pantry-status-group{display:flex;gap:4px}
.st-btn{font-size:14px;padding:8px 12px;border-radius:8px;cursor:pointer;border:1px solid #d4c9b8;background:#faf7f2;color:#5a4a3a;white-space:nowrap;transition:all .15s;min-height:40px;display:flex;align-items:center}
.st-btn:active{opacity:.7}
.st-btn.active-priority{background:#fff3cd;color:#856404;border-color:#e0c060}
.st-btn.active-expiring{background:#f8d7da;color:#721c24;border-color:#e8a0a8}
.pantry-used-up{font-size:14px;padding:8px 12px;border-radius:8px;cursor:pointer;border:1px solid #f5c6cb;background:#fff;color:#721c24;white-space:nowrap;min-height:40px;display:flex;align-items:center}
.pantry-used-up:active{opacity:.7}
.history-entry{background:#fff;border-radius:10px;padding:12px;margin-bottom:8px;box-shadow:0 1px 2px rgba(0,0,0,.03)}
.history-date{font-size:16px;font-weight:600;margin-bottom:4px;display:flex;justify-content:space-between;align-items:center}
.history-context{font-size:13px;color:#a89888;margin-bottom:6px;display:flex;gap:8px;flex-wrap:wrap}
.history-context span{display:inline-flex;align-items:center;gap:2px}
.history-meal{font-size:14px;margin-top:3px}
.history-meal .label{color:#a89888;font-weight:600;margin-right:4px;font-size:13px}
.history-meal .dish-list{color:#2c2620}
.history-meal .dish-list-en{color:#a89888;font-style:italic;font-size:13px}
.footer{text-align:center;padding:20px;color:#a89888;font-size:13px}
.modal-overlay{position:fixed;inset:0;background:rgba(0,0,0,.5);z-index:200;display:none;align-items:flex-end;justify-content:center}
.modal-overlay.show{display:flex}
.modal{background:#fff;width:100%;max-width:600px;border-radius:16px 16px 0 0;max-height:85vh;overflow-y:auto}
.modal-img{width:100%;aspect-ratio:2;object-fit:cover;background:#f5f0e8}
.modal-body{padding:16px}
.modal-body h2{font-size:22px;font-weight:700;margin-bottom:2px}
.modal-body .en-name{font-size:15px;color:#a89888;font-style:italic;margin-bottom:10px}
.modal-body .meta-row{display:flex;gap:6px;flex-wrap:wrap;margin-bottom:8px}
.modal-body .section-title{font-size:15px;font-weight:600;color:#5a4a3a;margin:10px 0 4px}
.modal-body .ing-list{font-size:14px;color:#5a4a3a}
.modal-actions{display:flex;gap:8px;padding:12px 16px;border-top:1px solid #f5f0e8;position:sticky;bottom:0;background:#fff}
.modal-actions .btn{flex:1;padding:12px;font-size:16px}
.rec-section-title{font-size:15px;font-weight:700;color:#5a4a3a;padding:8px 16px 4px;background:#f5f0e8;border-radius:6px 6px 0 0}
.add-to-meal{display:grid;grid-template-columns:1fr 1fr 1fr;gap:8px;margin-top:8px}
.add-to-meal .btn{padding:12px;font-size:15px;min-height:44px}
.snack-bar{position:fixed;bottom:20px;left:50%;transform:translateX(-50%);background:#2c2620;color:#faf7f2;padding:10px 20px;border-radius:20px;font-size:14px;z-index:300;display:none;white-space:nowrap}
.snack-bar.show{display:block}
.ing-picker{display:flex;gap:6px;flex-wrap:wrap;padding:8px 0}
.ing-chip{font-size:14px;padding:6px 12px;border-radius:14px;border:1px solid #d4c9b8;background:#faf7f2;color:#5a4a3a;cursor:pointer}
.ing-chip.selected{background:#2c2620;color:#faf7f2;border-color:#2c2620}
.ing-chip:active{opacity:.7}
.ing-add-chip{font-size:14px;padding:6px 12px;border-radius:14px;border:1px dashed #4a9eff;background:#e8f4ff;color:#0c5460;cursor:pointer}
.selected-list{margin-top:10px}
.selected-item{display:flex;align-items:center;justify-content:space-between;padding:10px 12px;background:#f5f0e8;border-radius:8px;margin-bottom:6px;gap:8px}
.selected-item .name{font-size:16px;font-weight:600}
.selected-item .name-en{font-size:14px;color:#a89888}
.status-picker{display:flex;gap:4px}
.status-pick{font-size:14px;padding:4px 10px;border-radius:10px;border:1px solid #d4c9b8;background:#fff;color:#5a4a3a;cursor:pointer}
.status-pick.active{font-weight:600}
.collapsible{margin-bottom:10px}
.collapsible-header{display:flex;align-items:center;justify-content:space-between;padding:12px 14px;background:#fff;border-radius:10px;cursor:pointer;font-size:16px;font-weight:500}
.collapsible-header .arrow{transition:transform .2s}
.collapsible-header.open .arrow{transform:rotate(90deg)}
.collapsible-body{display:none;padding:0 14px}
.collapsible-body.open{display:block}
.warning-box{background:#fff3cd;border:1px solid #ffeaa7;border-radius:8px;padding:10px 14px;margin-bottom:8px;font-size:14px;color:#856404}
.warning-box .warn-title{font-weight:600;margin-bottom:4px}
.diners-section{background:#fff;border-radius:10px;padding:10px 14px;margin-bottom:8px}
.diners-section .diners-title{font-size:14px;font-weight:600;color:#a89888;margin-bottom:6px}
.diners-row{display:flex;flex-wrap:wrap;gap:6px}
.diner-chip{display:inline-flex;align-items:center;gap:3px;padding:8px 14px;border-radius:14px;border:1px solid #ddd5c8;font-size:16px;cursor:pointer;transition:all .15s;min-height:44px}
.diner-chip.active{background:#2c2620;color:#faf7f2;border-color:#2c2620}
.diner-chip.active .diner-en{color:#a89888}
.diner-en{font-size:13px;opacity:.7}
.purchase-task{display:flex;align-items:center;justify-content:space-between;padding:10px 0;border-bottom:1px solid #f5f0e8}
.purchase-task:last-child{border-bottom:none}
.purchase-task .info{flex:1;min-width:0}
.purchase-task .dish-name-sm{font-size:15px;font-weight:500}
.purchase-task .missing-list{font-size:13px;color:#a89888;margin-top:2px}
.purchase-task .act-btn{font-size:14px;padding:8px 14px;border-radius:8px;border:none;cursor:pointer;white-space:nowrap;flex-shrink:0;margin-left:8px;min-height:44px}
.act-notify{background:#4a9eff;color:#fff}
.act-notified{background:#e8e0d4;color:#6c757d}
.act-purchased{background:#d4edda;color:#155724}
.ing-group-header{font-size:15px;font-weight:600;color:#5a4a3a;padding:8px 0 4px;border-bottom:1px solid #f5f0e8;margin-top:4px}
.ing-group-header:first-child{margin-top:0}
"""

def page_head(title, active_nav="", location="shenzhen"):
    loc_btns = ""
    for loc_id, loc_label in LOCATIONS.items():
        cls = "active" if loc_id == location else ""
        loc_btns += f'<button class="loc-btn {cls}" onclick="switchLocation(\'{loc_id}\')">{loc_label}</button>'
    nav_items = [
        ("tomorrow", "明日", "Tomorrow"),
        ("pantry", "食材", "Pantry"),
        ("dishes", "菜品", "Dishes"),
        ("history", "历史", "History"),
    ]
    nav_html = ""
    for path, cn, en in nav_items:
        cls = "active" if path == active_nav else ""
        nav_html += f'<a href="/{path}" class="{cls}">{cn}<span>{en}</span></a>'

    return f"""<!DOCTYPE html>
<html lang="zh-CN">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width,initial-scale=1.0,maximum-scale=1.0,user-scalable=no">
<meta name="apple-mobile-web-app-capable" content="yes">
<title>{title}</title>
<style>{CSS}</style>
</head>
<body>
<div class="header">
<h1>家庭菜单</h1>
<div class="loc-switch">{loc_btns}</div>
</div>
<div class="loc-banner" id="locBanner">📍 当前厨房 Kitchen: {LOCATIONS.get(location, location)}</div>
<div class="nav">{nav_html}</div>"""

PAGE_FOOT = """
<div class="footer">家庭菜单管家 · Powered by AI</div>
<div class="snack-bar" id="snackBar"></div>
<script>
function switchLocation(loc){
  if(typeof hasUnsavedChanges!=='undefined'&&hasUnsavedChanges){
    if(!confirm('当前修改尚未保存，确定切换厨房吗?\\nUnsaved changes will be lost. Switch kitchen?'))return;
  }
  document.cookie='loc='+loc+';path=/';
  location.reload();
}
function getLoc(){
  let m=document.cookie.match(/loc=(\\w+)/);
  return m?m[1]:'shenzhen';
}
function snack(msg){
  let b=document.getElementById('snackBar');
  b.textContent=msg;b.classList.add('show');
  setTimeout(()=>b.classList.remove('show'),2000);
}
</script>
</body></html>"""


def get_location_from_cookie(cookie_header):
    if not cookie_header:
        return "shenzhen"
    for part in cookie_header.split(";"):
        part = part.strip()
        if part.startswith("loc="):
            val = part[4:]
            if val in LOCATIONS:
                return val
    return "shenzhen"


# ============================================================
# 明日菜单页面
# ============================================================

def render_tomorrow(role="owner", location="shenzhen"):
    tomorrow = get_tomorrow_date()
    # 确保菜单存在
    ensure_tomorrow_menu(location)
    menu = get_menu_with_dishes(tomorrow)

    meal_colors = {
        "breakfast": ("#f0a040", "早餐", "Breakfast"),
        "lunch": ("#4a9eff", "午餐", "Lunch"),
        "afternoon_snack": ("#7bc67b", "下午茶", "Afternoon Tea"),
        "dinner": ("#c0504c", "晚餐", "Dinner"),
    }

    # 获取用餐成员（仅用于人数计算，不参与过敏判断）
    all_diners = get_all_diners()
    menu_diners = get_menu_diners(menu["menu_id"]) if menu.get("menu_id") else []
    # 默认使用 default_attends=1 的成员
    if not menu_diners:
        menu_diners = [d["id"] for d in all_diners if d["default_attends"]]

    # V3: 生成 Warning（不阻断 Confirm）
    menu_warnings = []
    if menu.get("exists") and menu.get("menu_id"):
        try:
            from rule_engine import RuleEngine, NutritionAnalyzer, MealState
            with open(os.path.join(BASE_DIR, "dish_pool.json"), "r", encoding="utf-8") as f:
                pool = json.load(f)
            dish_map = {d["id"]: d for d in pool["dishes"]}
            day_result = {}
            for mt in ["breakfast", "lunch", "dinner"]:
                state = MealState()
                for item in menu["meals"].get(mt, []):
                    did = item.get("dish_id", "")
                    if did in dish_map:
                        analysis = NutritionAnalyzer.analyze(dish_map[did])
                        state.add_dish(analysis, is_locked=item.get("is_locked", False))
                day_result[mt] = {"state": state}
            review = RuleEngine.final_review(day_result)
            menu_warnings = review.get("warnings", [])
        except Exception:
            pass

    sections = []

    if not menu.get("exists"):
        sections.append(f'<div class="empty"><h2>明日菜单未生成</h2><p>系统将自动生成</p></div>')
    else:
        # 状态卡
        status_cls = f"status-{menu['status']}"
        status_text = {"draft": "待确认 Pending", "confirmed": "已确认 Confirmed", "pushed": "已推送 Pushed"}.get(menu["status"], menu["status"])
        sections.append(f'<div class="card"><p>状态 Status: <span class="status-tag {status_cls}">{status_text}</span></p><p style="margin-top:4px;font-size:14px">日期 Date: {menu["date"]} | {LOCATIONS.get(menu["location"], menu["location"])}</p></div>')

        # 用餐成员选择
        diners_chips = ""
        for d in all_diners:
            active = "active" if d["id"] in menu_diners else ""
            diners_chips += f'<span class="diner-chip {active}" data-diner="{d["id"]}" onclick="toggleDiner(\'{d["id"]}\')">{d["name_cn"]} <span class="diner-en">{d["name_en"]}</span></span>'
        sections.append(f'<div class="diners-section"><div class="diners-title">用餐成员 Diners</div><div class="diners-row">{diners_chips}</div></div>')

        # V3: Warning UI（不阻断 Confirm）
        if menu_warnings:
            warn_items = "".join(f'<div class="warn-item">⚠️ {w}</div>' for w in menu_warnings)
            sections.append(f'<div class="warning-box"><div class="warn-title">提示 Warnings ({len(menu_warnings)})</div>{warn_items}<div style="margin-top:4px;font-size:13px;color:#a89060">VV 仍可确认 · VV can still confirm</div></div>')

        # 获取采购任务
        purchase_reqs = get_menu_purchase_requests(menu["menu_id"]) if menu.get("menu_id") else []
        # 按食材分组
        req_by_ingredient = {}
        for r in purchase_reqs:
            ing_id = r["ingredient_id"]
            if ing_id not in req_by_ingredient:
                req_by_ingredient[ing_id] = r

        # 各餐次
        for mt in ["breakfast", "lunch", "afternoon_snack", "dinner"]:
            dishes = menu["meals"].get(mt, [])
            if not dishes:
                continue
            color, cn, en = meal_colors[mt]

            # 餐次操作按钮
            meal_actions = ""
            if role == "owner":
                if mt == "afternoon_snack":
                    # V3: 下午茶 VV 自选，允许添加但不进入正式推送
                    meal_actions = f'<div class="meal-actions"><span class="badge badge-warning" style="margin-right:8px">VV 自选 Optional</span><button class="meal-act-btn" onclick="openDishSearch(\'{mt}\')">＋添加 Add</button></div>'
                else:
                    meal_actions = f'<div class="meal-actions"><button class="meal-act-btn" onclick="openDishSearch(\'{mt}\')">＋添加 Add</button><button class="meal-act-btn" onclick="aiFillMeal(\'{mt}\')">AI 补充 AI Fill</button></div>'

            items_html = ""
            for d in dishes:
                img_html = f'<img src="/photos/{d["image"]}" onerror="this.style.display=\'none\';this.nextElementSibling.style.display=\'flex\'">' if d.get("image") else ""
                no_img = '<div class="no-img" style="display:none">🍽️</div>' if d.get("image") else '<div class="no-img">🍽️</div>'

                # 来源标签：owner = 已选, AI = AI 推荐
                if d.get("is_locked"):
                    source_badge = '<span class="badge badge-owner">已选 Selected</span>'
                else:
                    source_badge = '<span class="badge badge-ai">AI 推荐 AI Suggestion</span>'

                # V6: 已下架菜品标记
                archived_badge = ""
                if d.get("dish_archived"):
                    archived_badge = '<span class="badge" style="background:#e8e0d4;color:#6c757d">已下架 Archived</span>'

                # 缺货标识（拆分状态，V5: 显示具体缺失食材名）
                shortage_badge = ""
                short_ings = menu.get("shortages", {}).get(d["dish_id"], [])
                if short_ings:
                    missing_names = ", ".join(short_ings[:3])
                    if len(short_ings) > 3:
                        missing_names += f" 等{len(short_ings)}种"
                    # 检查采购任务状态
                    req_status = None
                    for r in purchase_reqs:
                        if r.get("dish_id") == d["dish_id"]:
                            req_status = r["status"]
                            break
                    if req_status == "notified":
                        shortage_badge = f'<span class="badge badge-shortage-notified">已通知采购: {missing_names}</span>'
                    elif req_status == "purchased":
                        shortage_badge = f'<span class="badge badge-shortage-purchased">已购买: {missing_names}</span>'
                    elif req_status == "needed":
                        shortage_badge = f'<span class="badge badge-shortage-tobuy">待采购: {missing_names}</span>'
                    else:
                        shortage_badge = f'<span class="badge badge-shortage-missing">缺: {missing_names}</span>'

                cat_label = d.get("category_id", "")
                cat_badge = f'<span class="badge badge-cat">{cat_label}</span>' if cat_label else ""

                # 操作按钮：替换 + 删除（所有菜都可操作）
                item_actions = ""
                if role == "owner":
                    item_actions = f'<div class="item-actions"><button class="item-btn" onclick="openDishSearch(\'{mt}\',{d["menu_item_id"]},\'{d.get("dish_id","")}\',\'{d.get("category_id","")}\')" title="替换 Replace">↻</button><button class="item-btn danger" onclick="removeDish({d["menu_item_id"]})" title="删除 Delete">×</button></div>'

                items_html += f"""<div class="meal-item">
{img_html}{no_img}
<div class="info">
<div class="dish-name">{source_badge}{archived_badge}{shortage_badge}{d["name_cn"]}</div>
<div class="dish-name-en">{d["name_en"] or ""}</div>
<div class="dish-meta">{cat_badge} {" · ".join(d.get("protein_types", []) or [])} {("· " + " ".join((d.get("vegetables") or [])[:2])) if d.get("vegetables") else ""}</div>
</div>{item_actions}</div>"""

            sections.append(f"""<div class="meal-section">
<div class="meal-header"><div class="meal-bar" style="background:{color}"></div><div><span class="meal-title">{cn}</span><span class="meal-title-en">{en}</span></div>{meal_actions}</div>
<div class="meal-items">{items_html}</div></div>""")

        # 采购任务区（可操作）
        if purchase_reqs:
            reqs_html = ""
            for r in purchase_reqs:
                ing_name = r.get("ingredient_name") or r["ingredient_id"]
                ing_en = r.get("ingredient_name_en") or ""
                dish_name = r.get("dish_name") or ""
                status = r["status"]
                if status == "needed":
                    action_btn = f'<button class="act-btn act-notify" onclick="notifyPurchase({r["id"]})">通知采购 Notify</button>'
                    status_label = ""
                elif status == "notified":
                    action_btn = f'<button class="act-btn act-purchased" onclick="markPurchased({r["id"]})">已购买 Purchased</button>'
                    status_label = '<span class="badge badge-shortage-notified">已通知</span>'
                elif status == "purchased":
                    action_btn = '<span class="act-btn act-purchased">✓ 已购买</span>'
                    status_label = ""
                else:
                    action_btn = f'<span class="badge badge-cat">{status}</span>'
                    status_label = ""

                reqs_html += f"""<div class="purchase-task">
<div class="info">
<div class="dish-name-sm">{ing_name} <span style="font-size:13px;color:#a89888">{ing_en}</span></div>
{f'<div class="missing-list">用于: {dish_name}</div>' if dish_name else ''}
{status_label}
</div>{action_btn}</div>"""

            sections.append(f'<div class="card"><h3>采购任务 Purchase Tasks</h3>{reqs_html}</div>')

        # 底部操作
        if role == "owner":
            if menu["status"] == "draft":
                sections.append(f'<button class="btn btn-outline" onclick="repairMenu()">重新推荐 AI 菜品 Refresh AI Suggestions</button>')
                sections.append(f'<button class="btn btn-primary" style="margin-top:8px" onclick="confirmMenu()">确认明日菜单 Confirm</button>')
            elif menu["status"] == "confirmed":
                # V3: Confirmed → Edit Menu → Reconfirm flow
                sections.append('<div class="card" style="text-align:center"><p>✅ 菜单已确认，等待推送 Menu confirmed, awaiting push</p></div>')
                sections.append('<button class="btn btn-outline" style="margin-top:8px" onclick="editMenu()">修改菜单 Edit Menu</button>')
            elif menu["status"] == "pushed":
                sections.append('<div class="card" style="text-align:center"><p>📡 菜单已推送 Menu Pushed</p></div>')
                sections.append('<button class="btn btn-outline" style="margin-top:8px" onclick="editMenu()">修改菜单 Edit Menu</button>')
                sections.append('<p style="font-size:13px;color:#a89888;margin-top:4px;text-align:center">修改后需重新确认并通知 Reconfirm required after edit</p>')

    body = "\n".join(sections)
    js = f"""<script>
let menuId={menu.get("menu_id","null")};
let currentLoc='{location}';
let hasUnsavedChanges=false;
let selectedDiners={json.dumps(menu_diners)};
function toggleDiner(id){{
  let i=selectedDiners.indexOf(id);
  if(i>=0)selectedDiners.splice(i,1);
  else selectedDiners.push(id);
  // Update chip UI
  let chip=document.querySelector('.diner-chip[data-diner="'+id+'"]');
  if(chip)chip.classList.toggle('active');
  // Save to server
  fetch('/api/tomorrow/diners',{{method:'POST',headers:{{'Content-Type':'application/json'}},body:JSON.stringify({{menu_id:menuId,diners:selectedDiners}})}}).then(r=>r.json()).then(res=>{{
    if(res.ok){{snack('用餐成员已更新 Diners updated');}}
  }});
  // Reload to refresh menu
  setTimeout(()=>location.reload(),800);
}}
// V7: Smart dish replacement modal
let searchMode={{meal:null,replaceId:null,currentDishId:null,categoryId:null}};
function openDishSearch(mt,replaceId,currentDishId,categoryId){{
  searchMode={{meal:mt,replaceId:replaceId||null,currentDishId:currentDishId||null,categoryId:categoryId||null}};
  let m=document.getElementById('dishSearchModal');
  m.classList.add('show');
  let input=document.getElementById('dishSearchInput');
  input.value='';
  let titleEl=document.getElementById('dishSearchTitle');
  let hintEl=document.getElementById('dishSearchHint');
  if(replaceId){{
    titleEl.textContent='换一道 Replace Dish';
    hintEl.style.display='block';
    loadRecommendations();
  }}else{{
    titleEl.textContent='添加菜品 Add Dish';
    hintEl.style.display='none';
    document.getElementById('dishSearchResults').innerHTML='<div style="text-align:center;padding:30px 20px;color:#a89888"><div style="font-size:15px">输入菜名搜索</div><div style="font-size:13px;margin-top:4px">Type to search</div></div>';
    input.focus();
  }}
}}
function closeDishSearch(){{document.getElementById('dishSearchModal').classList.remove('show');}}
let _searchTimer=null;
function onDishSearchInput(){{
  clearTimeout(_searchTimer);
  let q=document.getElementById('dishSearchInput').value.trim();
  if(!q){{
    if(searchMode.replaceId){{loadRecommendations();}}else{{
      document.getElementById('dishSearchResults').innerHTML='<div style="text-align:center;padding:30px 20px;color:#a89888"><div style="font-size:15px">输入菜名搜索</div><div style="font-size:13px;margin-top:4px">Type to search</div></div>';
    }}
    return;
  }}
  _searchTimer=setTimeout(()=>doDishSearch(q),300);
}}
async function loadRecommendations(){{
  let container=document.getElementById('dishSearchResults');
  container.innerHTML='<div style="text-align:center;padding:30px 20px;color:#a89888"><div style="font-size:15px">推荐加载中...</div><div style="font-size:13px;margin-top:4px">Loading recommendations...</div></div>';
  let r=await fetch('/api/dishes/recommend',{{method:'POST',headers:{{'Content-Type':'application/json'}},body:JSON.stringify({{meal_type:searchMode.meal,current_dish_id:searchMode.currentDishId,category_id:searchMode.categoryId,location:currentLoc}})}});
  let data=await r.json();
  window._recMap={{}};
  (data.available||[]).forEach(d=>{{window._recMap[d.id]=d;}});
  (data.almost_available||[]).forEach(d=>{{window._recMap[d.id]=d;}});
  let html='';
  // Available Now
  if(data.available&&data.available.length){{
    html+='<div class="rec-section-title">库存可做 Available Now</div>';
    html+=data.available.map(d=>renderRecCard(d,'available')).join('');
  }}
  // Almost Available
  if(data.almost_available&&data.almost_available.length){{
    html+='<div class="rec-section-title" style="margin-top:12px">差1种食材 Almost Available</div>';
    html+=data.almost_available.map(d=>renderRecCard(d,'almost')).join('');
  }}
  if(!html){{
    html='<div style="text-align:center;padding:30px 20px;color:#a89888"><div style="font-size:15px">暂无推荐</div><div style="font-size:13px;margin-top:4px">No recommendations</div></div>';
  }}
  container.innerHTML=html;
}}
function renderRecCard(d,type){{
  let img=d.image?'<img src="/photos/'+d.image+'" onerror="this.style.display=\\'none\\';this.nextElementSibling.style.display=\\'flex\\'" style="width:60px;height:60px;border-radius:8px;object-fit:cover;flex-shrink:0">':'<div style="width:60px;height:60px;border-radius:8px;background:#f5f0e8;display:flex;align-items:center;justify-content:center;font-size:24px;flex-shrink:0">🍽️</div>';
  let noImg=d.image?'<div style="width:60px;height:60px;border-radius:8px;background:#f5f0e8;display:none;align-items:center;justify-content:center;font-size:24px;flex-shrink:0">🍽️</div>':'';
  let badge=type==='available'?'<span style="font-size:13px;color:#155724;background:#d4edda;padding:2px 8px;border-radius:4px;display:inline-block;margin-top:3px">库存可做 Available</span>':'';
  let missing='';
  if(d.missing_required&&d.missing_required.length){{
    let mn=d.missing_required.join(', ');
    let mnEn=d.missing_required_en?d.missing_required_en.join(', '):'';
    missing='<span style="font-size:13px;color:#856404;background:#fff3cd;padding:2px 8px;border-radius:4px;display:inline-block;margin-top:3px">缺：'+mn+' Missing: '+mnEn+'</span>';
  }}
  return '<div onclick="pickRec(\\''+d.id+'\\','+(type==='almost')+')" style="display:flex;gap:10px;padding:12px;border-bottom:1px solid #f5f0e8;cursor:pointer;align-items:center">'+img+noImg+'<div style="flex:1;min-width:0"><div style="font-size:18px;font-weight:600">'+d.name_cn+'</div><div style="font-size:14px;color:#a89888;font-style:italic">'+(d.name_en||'')+'</div>'+badge+missing+'</div></div>';
}}
async function pickRec(dishId,isAlmost){{
  if(isAlmost){{
    let d=window._recMap&&window._recMap[dishId];
    if(d&&d.missing_required&&d.missing_required.length){{
      let mn=d.missing_required.join(', ');
      let mnEn=d.missing_required_en?d.missing_required_en.join(', '):'';
      if(!confirm('这道菜还缺：'+mn+'\\n\\nThis dish is missing:\\n'+mnEn+'\\n\\n仍然选择? Choose Anyway?'))return;
    }}
  }}
  await doPickDish(dishId);
}}
async function doDishSearch(q){{
  let res=await fetch('/api/dishes?search='+encodeURIComponent(q));
  let data=await res.json();
  let container=document.getElementById('dishSearchResults');
  if(!data.length){{
    container.innerHTML='<div style="text-align:center;padding:30px 20px;color:#a89888"><div style="font-size:15px">没有找到相关菜品</div><div style="font-size:13px;margin-top:4px">No matching dishes</div></div>';
    return;
  }}
  container.innerHTML=data.slice(0,20).map(d=>{{
    let img=d.image?'<img src="/photos/'+d.image+'" onerror="this.style.display=\\'none\\';this.nextElementSibling.style.display=\\'flex\\'" style="width:60px;height:60px;border-radius:8px;object-fit:cover;flex-shrink:0">':'<div style="width:60px;height:60px;border-radius:8px;background:#f5f0e8;display:flex;align-items:center;justify-content:center;font-size:24px;flex-shrink:0">🍽️</div>';
    let noImg=d.image?'<div style="width:60px;height:60px;border-radius:8px;background:#f5f0e8;display:none;align-items:center;justify-content:center;font-size:24px;flex-shrink:0">🍽️</div>':'';
    return '<div onclick="doPickDish(\\''+d.id+'\\')" style="display:flex;gap:10px;padding:12px;border-bottom:1px solid #f5f0e8;cursor:pointer;align-items:center">'+img+noImg+'<div style="flex:1;min-width:0"><div style="font-size:18px;font-weight:600">'+d.name_cn+'</div><div style="font-size:14px;color:#a89888;font-style:italic">'+(d.name_en||'')+'</div><div style="font-size:13px;color:#a89888;margin-top:2px">'+(d.category_id||'')+'</div></div></div>';
  }}).join('');
}}
async function doPickDish(dishId){{
  if(searchMode.replaceId){{
    let r=await fetch('/api/tomorrow/replace',{{method:'POST',headers:{{'Content-Type':'application/json'}},body:JSON.stringify({{menu_id:menuId,menu_item_id:searchMode.replaceId,new_dish_id:dishId}})}});
    let result=await r.json();
    if(result.ok){{closeDishSearch();snack('已替换 Replaced');location.reload();}}else{{snack(result.error||'替换失败');}}
  }}else{{
    let r=await fetch('/api/tomorrow/add',{{method:'POST',headers:{{'Content-Type':'application/json'}},body:JSON.stringify({{menu_id:menuId,dish_id:dishId,meal_type:searchMode.meal}})}});
    let result=await r.json();
    if(result.ok){{closeDishSearch();snack('已添加 Added');location.reload();}}else{{snack(result.error||'添加失败');}}
  }}
}}
async function removeDish(itemId){{
  if(!confirm('确认删除? Confirm delete?'))return;
  let r=await fetch('/api/tomorrow/remove',{{method:'POST',headers:{{'Content-Type':'application/json'}},body:JSON.stringify({{menu_id:menuId,menu_item_id:itemId}})}});
  let result=await r.json();
  if(result.ok){{snack('已删除 Removed');location.reload();}}else{{snack(result.error||'删除失败');}}
}}
async function aiFillMeal(mt){{
  snack('AI 补充中... AI filling...');
  let r=await fetch('/api/tomorrow/ai-fill',{{method:'POST',headers:{{'Content-Type':'application/json'}},body:JSON.stringify({{menu_id:menuId,location:currentLoc,meal_type:mt}})}});
  let result=await r.json();
  if(result.ok){{snack('AI 补充完成 AI fill done');location.reload();}}else{{snack(result.error||'补充失败');}}
}}
async function repairMenu(){{
  if(!confirm('重新推荐所有 AI 菜品? Refresh all AI suggestions?'))return;
  snack('重新推荐中... Refreshing...');
  let r=await fetch('/api/tomorrow/repair',{{method:'POST',headers:{{'Content-Type':'application/json'}},body:JSON.stringify({{menu_id:menuId,location:currentLoc}})}});
  let result=await r.json();
  if(result.ok){{snack('已重新推荐 Refreshed');location.reload();}}else{{snack(result.error||'失败');}}
}}
async function confirmMenu(){{
  if(!confirm('确认明日菜单? Confirm tomorrow menu?'))return;
  let r=await fetch('/api/tomorrow/confirm',{{method:'POST',headers:{{'Content-Type':'application/json'}},body:JSON.stringify({{menu_id:menuId}})}});
  let result=await r.json();
  if(result.ok){{
    if(result.warnings&&result.warnings.length){{
      snack('已确认（有'+result.warnings.length+'项提示）');
    }}else{{
      snack('已确认 Confirmed');
    }}
    setTimeout(()=>location.reload(),1500);
  }}else{{
    snack(result.error||'确认失败');
  }}
}}
async function editMenu(){{
  if(!confirm('修改菜单将回退到草稿状态，修改后需重新确认。\\nEditing will revert to draft. Reconfirm required.'))return;
  let r=await fetch('/api/tomorrow/revert',{{method:'POST',headers:{{'Content-Type':'application/json'}},body:JSON.stringify({{menu_id:menuId}})}});
  let result=await r.json();
  if(result.ok){{
    snack('已回退到草稿 Reverted to draft');
    setTimeout(()=>location.reload(),1000);
  }}else{{
    snack(result.error||'操作失败');
  }}
}}
async function notifyPurchase(reqId){{
  let r=await fetch('/api/purchase/update',{{method:'POST',headers:{{'Content-Type':'application/json'}},body:JSON.stringify({{id:reqId,status:'notified',by:'owner'}})}});
  let result=await r.json();
  if(result.ok){{snack('已通知采购 Notified');location.reload();}}else{{snack('失败');}}
}}
async function markPurchased(reqId){{
  let r=await fetch('/api/purchase/update',{{method:'POST',headers:{{'Content-Type':'application/json'}},body:JSON.stringify({{id:reqId,status:'purchased',by:'owner'}})}});
  let result=await r.json();
  if(result.ok){{snack('已标记购买 Purchased');location.reload();}}else{{snack('失败');}}
}}
</script>"""

    return f"""{page_head("明日菜单 · Tomorrow", "tomorrow", location)}
<div class="content">{body}</div>
<div class="modal-overlay" id="dishSearchModal" onclick="if(event.target===this)closeDishSearch()">
<div class="modal" style="max-height:85vh">
<div style="padding:14px 16px;border-bottom:1px solid #f5f0e8">
<div id="dishSearchTitle" style="font-size:22px;font-weight:700;margin-bottom:8px">换一道 Replace Dish</div>
<div id="dishSearchHint" style="font-size:13px;color:#a89888;margin-bottom:8px">系统根据库存和当前餐位推荐 · 或搜索菜品 Smart recommendations based on pantry & meal</div>
<input type="text" id="dishSearchInput" placeholder="搜索菜品 Search dishes..." oninput="onDishSearchInput()" style="width:100%;padding:12px 16px;border:1px solid #e8e0d4;border-radius:24px;font-size:16px;outline:none;box-sizing:border-box">
</div>
<div id="dishSearchResults" style="overflow-y:auto;max-height:60vh"></div>
<div class="modal-actions"><button class="btn btn-outline" onclick="closeDishSearch()">取消 Cancel</button></div>
</div></div>
{PAGE_FOOT}
{js}"""


# ============================================================
# 菜品库页面
# ============================================================

def render_dishes(role="owner", location="shenzhen"):
    cats = get_categories()
    cat_json = json.dumps({c["id"]: {"cn": c["label_cn"], "en": c["label_en"]} for c in cats}, ensure_ascii=False)

    cat_tabs_html = '<div class="filter-tabs">'
    cat_tabs_html += '<div class="filter-tab active" data-cat="">全部 All</div>'
    for c in cats:
        cat_tabs_html += f'<div class="filter-tab" data-cat="{c["id"]}">{c["label_cn"]} <span style="opacity:.6">{c["label_en"]}</span></div>'
    cat_tabs_html += "</div>"

    # Availability filter tabs
    avail_tabs_html = '<div class="filter-tabs" style="border-bottom:none">'
    avail_tabs_html += '<div class="filter-tab active" data-avail="">全部 All</div>'
    avail_tabs_html += '<div class="filter-tab" data-avail="available">库存可做 Available Now</div>'
    avail_tabs_html += '<div class="filter-tab" data-avail="almost">差少量 Almost Available</div>'
    avail_tabs_html += "</div>"

    return f"""{page_head("菜品库 · Dishes", "dishes", location)}
<div class="search-bar"><input type="text" id="search" placeholder="搜索菜名 Search dishes..." oninput="loadDishes()"></div>
{cat_tabs_html}
{avail_tabs_html}
<div class="content">
<div id="dishGrid" class="dish-grid"></div>
</div>
{PAGE_FOOT}
<script>
const catNames={cat_json};
let currentCat='';
let currentSearch='';
let currentAvail='';
let availData={{}};
async function loadDishes(){{
  currentSearch=document.getElementById('search').value;
  let params=[];
  if(currentSearch)params.push('search='+encodeURIComponent(currentSearch));
  if(currentCat)params.push('category='+currentCat);
  let url='/api/dishes'+(params.length?'?'+params.join('&'):'');
  let res=await fetch(url);
  let data=await res.json();
  // If availability filter is set, fetch availability data
  if(currentAvail&&data.length){{
    let ids=data.map(d=>d.id);
    let avRes=await fetch('/api/dishes/availability',{{
      method:'POST',headers:{{'Content-Type':'application/json'}},
      body:JSON.stringify({{dish_ids:ids,location:'{location}'}})
    }});
    availData=await avRes.json();
    // Filter
    data=data.filter(d=>{{
      let av=availData[d.id];
      if(!av)return false;
      if(currentAvail==='available')return av.status==='available';
      if(currentAvail==='almost')return av.status==='almost_available';
      return true;
    }});
  }}
  let grid=document.getElementById('dishGrid');
  if(!data.length){{grid.innerHTML='<div class="empty"><p>无匹配菜品 No dishes found</p></div>';return;}}
  grid.innerHTML=data.map(d=>{{
    let img=d.image?'<img src="/photos/'+d.image+'" onerror="this.style.display=\\'none\\';this.nextElementSibling.style.display=\\'flex\\'">':'<div class="no-img">🍽️</div>';
    let noImg=d.image?'<div class="no-img" style="display:none">🍽️</div>':'';
    let catInfo=catNames[d.category_id]||{{cn:'',en:''}};
    let av=availData[d.id];
    let avBadge='';
    if(av){{
      if(av.status==='available')avBadge='<div class="avail-badge avail-yes">库存可做 Available</div>';
      else if(av.status==='almost_available'){{
        let mn=av.missing_names||[];
        avBadge='<div class="avail-badge avail-almost">缺: '+mn.join(', ')+' Missing: '+mn.join(', ')+'</div>';
      }}
      else if(av.status==='incomplete')avBadge='<div class="avail-badge" style="background:#e8e0d4;color:#6c757d">食材资料待完善 Incomplete</div>';
      else if(av.status==='missing'&&av.missing_count>0){{
        let mn=av.missing_names||[];
        avBadge='<div class="avail-badge" style="background:#f8d7da;color:#721c24">缺'+av.missing_count+'种: '+mn.slice(0,3).join(', ')+'</div>';
      }}
    }}
    return '<div class="dish-card" onclick="showDetail(\\''+d.id+'\\')">'+img+noImg+'<div class="info"><div class="name">'+d.name_cn+'</div><div class="name-en">'+(d.name_en||'')+'</div><div class="cat-label">'+catInfo.cn+' '+catInfo.en+'</div>'+avBadge+'</div></div>';
  }}).join('');
}}
document.querySelectorAll('.filter-tab[data-cat]').forEach(t=>{{
  t.onclick=()=>{{
    document.querySelectorAll('.filter-tab[data-cat]').forEach(x=>x.classList.remove('active'));
    t.classList.add('active');
    currentCat=t.dataset.cat;
    loadDishes();
  }};
}});
document.querySelectorAll('.filter-tab[data-avail]').forEach(t=>{{
  t.onclick=()=>{{
    document.querySelectorAll('.filter-tab[data-avail]').forEach(x=>x.classList.remove('active'));
    t.classList.add('active');
    currentAvail=t.dataset.avail;
    availData={{}};
    loadDishes();
  }};
}});
async function showDetail(id){{
  let res=await fetch('/api/dishes/'+id);
  let d=await res.json();
  if(!d)return;
  let img=d.image?'<img class="modal-img" src="/photos/'+d.image+'" onerror="this.style.display=\\'none\\'">':'';
  let ings=(d.ingredients||[]).map(i=>i.name_cn+' '+(i.name_en||'')).join(', ');
  let proteins=(d.protein_types||[]).join(', ');
  let vegs=(d.vegetables||[]).join(', ');
  let catInfo=catNames[d.category_id]||{{cn:'',en:''}};
  let modal=document.getElementById('dishModal');
  let body=document.getElementById('dishModalBody');
  body.innerHTML=img+
    '<div class="modal-body">'+
    '<h2>'+d.name_cn+'</h2>'+
    '<div class="en-name">'+(d.name_en||'')+'</div>'+
    '<div class="meta-row"><span class="badge badge-cat">'+catInfo.cn+' '+catInfo.en+'</span>'+(d.carb_type?'<span class="badge badge-cat">'+d.carb_type+'</span>':'')+(d.banquet?'<span class="badge badge-ai">家宴 Banquet</span>':'')+'</div>'+
    (proteins?'<div class="section-title">蛋白质 Protein</div><div class="ing-list">'+proteins+'</div>':'')+
    (vegs?'<div class="section-title">蔬菜 Vegetable</div><div class="ing-list">'+vegs+'</div>':'')+
    (ings?'<div class="section-title">食材 Ingredients</div><div class="ing-list">'+ings+'</div>':'')+
    '</div>';
  let actions=document.getElementById('dishModalActions');
  actions.innerHTML=
    '<button class="btn btn-outline" onclick="addToTomorrow(\\''+id+'\\',\\'breakfast\\')">加入早餐 Breakfast</button>'+
    '<button class="btn btn-outline" onclick="addToTomorrow(\\''+id+'\\',\\'lunch\\')">加入午餐 Lunch</button>'+
    '<button class="btn btn-outline" onclick="addToTomorrow(\\''+id+'\\',\\'dinner\\')">加入晚餐 Dinner</button>';
  modal.classList.add('show');
}}
function closeModal(){{document.getElementById('dishModal').classList.remove('show');}}
async function addToTomorrow(dishId,mt){{
  let res=await fetch('/api/tomorrow/add',{{method:'POST',headers:{{'Content-Type':'application/json'}},body:JSON.stringify({{menu_id:window.tomorrowMenuId,dish_id:dishId,meal_type:mt}})}});
  let result=await res.json();
  if(result.ok){{snack('已加入 Added to '+mt);closeModal();}}else{{snack(result.error||'添加失败');}}
}}
loadDishes();
</script>
<div class="modal-overlay" id="dishModal" onclick="if(event.target===this)closeModal()">
<div class="modal">
<div id="dishModalBody"></div>
<div class="modal-actions" id="dishModalActions"></div>
</div></div>
<script>
// 预加载 tomorrow menu_id
fetch('/api/tomorrow').then(r=>r.json()).then(d=>{{window.tomorrowMenuId=d.menu_id||null;}});
</script>"""


# ============================================================
# 食材库存页面
# ============================================================

def render_pantry(role="nanny", location="shenzhen"):
    pantry = get_current_pantry(location)
    reqs = get_purchase_requests(location=location) if location else []
    common = get_common_ingredients()
    all_ings = get_all_ingredients()

    sections = []

    # V6: 顶部显示库存数量
    pantry_count = pantry["count"] if pantry else 0
    sections.append(f'<div class="card"><h3>当前库存 Current Pantry</h3><p>当前库存 <strong>{pantry_count}</strong> 项 · {LOCATIONS.get(location, location)}</p></div>')

    # V6: "和上次一样" 主按钮
    sections.append(f'<button class="btn btn-outline" style="margin-bottom:10px" onclick="sameAsLast()">✓ 和上次一样 Same as Last Update</button>')

    if not pantry or pantry_count == 0:
        sections.append(f'<div class="empty"><h2>暂无库存 No Inventory</h2><p>当前冰箱为空，请添加食材</p><p style="font-size:14px;color:#a89888">Current pantry is empty, please add ingredients</p></div>')
    else:
        # V7: 库存列表 — 2状态切换 + 用完按钮（取消"可用"显示）
        items_html = ""
        for item in pantry["items"]:
            ing_id = item["ingredient_id"]
            name_en = item.get("name_en", "") or ""
            st = item["status"]
            # Use First button
            pf_cls = "active-priority" if st == "priority_use" else ""
            # Expiring Soon button
            ex_cls = "active-expiring" if st == "expiring" else ""
            items_html += f"""<div class="pantry-item" id="pi-{ing_id}">
<div><div class="name">{item["name_cn"]}</div><div class="name-en">{name_en}</div></div>
<div class="pantry-controls">
<div class="pantry-status-group">
<span class="st-btn {pf_cls}" onclick="toggleStatus('{ing_id}','priority_use')">优先用 Use First</span>
<span class="st-btn {ex_cls}" onclick="toggleStatus('{ing_id}','expiring')">快过期 Expiring Soon</span>
</div>
<span class="pantry-used-up" onclick="usedUp('{ing_id}')">用完 Used Up</span>
</div></div>"""

        sections.append(f'<div class="card">{items_html}</div>')

    # V6: 搜索添加区（内嵌，不需要跳转页面）
    common_json = json.dumps([{"id": i["ingredient_id"], "cn": i["name_cn"], "en": i.get("name_en") or ""} for i in common], ensure_ascii=False)
    all_ings_json = json.dumps([{"id": i["ingredient_id"], "cn": i["name_cn"], "en": i["name_en"] or "", "aliases": i.get("aliases", [])} for i in all_ings], ensure_ascii=False)
    current_ids = json.dumps([i["ingredient_id"] for i in (pantry["items"] if pantry else [])], ensure_ascii=False)

    sections.append(f"""<div class="card">
<h3>搜索或添加食材 Search or Add</h3>
<input type="text" id="ingSearch" placeholder="搜索食材 Search ingredient..." oninput="filterIngs()" style="width:100%;padding:9px 14px;border:1px solid #e8e0d4;border-radius:20px;font-size:14px;outline:none;margin-top:8px">
<div id="searchResults" style="margin-top:8px"></div>
</div>""")

    sections.append(f"""<div class="card">
<h3>常用食材 Common</h3>
<div class="ing-picker" id="commonList"></div>
</div>""")

    # 采购任务（可操作）
    if reqs:
        active_reqs = [r for r in reqs if r["status"] in ("needed", "notified")]
        if active_reqs:
            reqs_html = ""
            for r in active_reqs:
                ing_name = r.get("ingredient_name") or r["ingredient_id"]
                ing_en = r.get("ingredient_name_en") or ""
                dish_name = r.get("dish_name") or ""
                status = r["status"]

                if status == "needed":
                    action_btn = f'<button class="act-btn act-notify" onclick="notifyPurchase({r["id"]})">通知采购 Notify</button>'
                    status_badge = ""
                elif status == "notified":
                    action_btn = f'<button class="act-btn act-purchased" onclick="markPurchased({r["id"]})">已购买 Purchased</button>'
                    status_badge = '<span class="badge badge-shortage-notified">已通知 Notified</span>'
                else:
                    action_btn = f'<span class="badge badge-cat">{status}</span>'
                    status_badge = ""

                reqs_html += f"""<div class="purchase-task">
<div class="info">
<div class="dish-name-sm">{ing_name} <span style="font-size:13px;color:#a89888">{ing_en}</span></div>
{f'<div class="missing-list">用于: {dish_name}</div>' if dish_name else ''}
{status_badge}
</div>{action_btn}</div>"""

            sections.append(f'<div class="card"><h3>采购任务 Purchase Tasks ({len(active_reqs)})</h3>{reqs_html}</div>')

    # V6: 保存状态提示
    sections.append('<div id="saveIndicator" style="text-align:center;padding:8px;color:#a89888;font-size:14px;display:none">✓ 已保存 Saved</div>')

    body = "\n".join(sections)
    return f"""{page_head("食材库存 · Pantry", "pantry", location)}
<div class="content">{body}</div>
{PAGE_FOOT}
<script>
const allIngs={all_ings_json};
const commonIngs={common_json};
const currentIds={current_ids};
let currentLoc='{location}';

function init(){{
  let cl=document.getElementById('commonList');
  cl.innerHTML=commonIngs.map(i=>{{
    let inPantry=currentIds.includes(i.id);
    let cls=inPantry?'ing-chip selected':'ing-chip';
    let suffix=inPantry?' ✓ 已在库存':'';
    return `<div class="${{cls}}" onclick="addIngredient('${{i.id}}')">${{i.cn}} ${{i.en}}${{suffix}}</div>`;
  }}).join('');
}}
function filterIngs(){{
  let q=document.getElementById('ingSearch').value.toLowerCase().trim();
  let res=document.getElementById('searchResults');
  if(!q){{res.innerHTML='';return;}}
  let matched=allIngs.filter(i=>{{
    if(i.cn.toLowerCase().includes(q))return true;
    if(i.en&&i.en.toLowerCase().includes(q))return true;
    if(i.aliases&&i.aliases.some(a=>a.toLowerCase().includes(q)))return true;
    return false;
  }}).slice(0,15);
  let html='';
  if(matched.length){{
    html=matched.map(i=>{{
      let inPantry=currentIds.includes(i.id);
      if(inPantry){{
        return `<div class="ing-chip selected" style="opacity:.6">✓ ${{i.cn}} ${{i.en||''}} 已在库存</div>`;
      }}else{{
        return `<div class="ing-chip" onclick="addIngredient('${{i.id}}')">${{i.cn}} ${{i.en||''}}</div>`;
      }}
    }}).join('');
  }}
  html+=`<div class="ing-add-chip" onclick="addNewIng('${{q}}')">+ 添加"${{q}}" Add New</div>`;
  res.innerHTML=html;
}}
function addNewIng(name){{
  fetch('/api/ingredients/add',{{method:'POST',headers:{{'Content-Type':'application/json'}},body:JSON.stringify({{name_cn:name}})}}).then(r=>r.json()).then(data=>{{
    if(data.ok){{addIngredient(data.ingredient_id);}}else{{snack(data.error||'添加失败');}}
  }});
}}
async function addIngredient(ingId){{
  if(currentIds.includes(ingId)){{snack('该食材已在当前库存中 Already in pantry');return;}}
  let r=await fetch('/api/pantry/add',{{method:'POST',headers:{{'Content-Type':'application/json'}},body:JSON.stringify({{ingredient_id:ingId,location:currentLoc}})}});
  let result=await r.json();
  if(result.ok){{showSaved();setTimeout(()=>location.reload(),500);}}else{{snack('失败 Failed');}}
}}
async function toggleStatus(ingId,status){{
  // V7: Toggle logic — if current status matches, revert to "available"
  let itemEl=document.getElementById('pi-'+ingId);
  let currentActive=itemEl.querySelector('.active-priority,.active-expiring');
  let isAlreadyActive=currentActive&&currentActive.classList.contains(status==='priority_use'?'active-priority':'active-expiring');
  let finalStatus=isAlreadyActive?'available':status;
  let r=await fetch('/api/pantry/update_status',{{method:'POST',headers:{{'Content-Type':'application/json'}},body:JSON.stringify({{ingredient_id:ingId,status:finalStatus,location:currentLoc}})}});
  let result=await r.json();
  if(result.ok){{
    // Update UI without reload
    let btns=itemEl.querySelectorAll('.st-btn');
    btns.forEach(b=>{{b.classList.remove('active-priority','active-expiring');}});
    if(finalStatus==='priority_use')btns[0].classList.add('active-priority');
    else if(finalStatus==='expiring')btns[1].classList.add('active-expiring');
    showSaved();
  }}else{{snack('失败 Failed');}}
}}
async function usedUp(ingId){{
  if(!confirm('确定已经用完并移出当前库存吗？\\nRemove this ingredient from the current pantry?'))return;
  let r=await fetch('/api/pantry/remove',{{method:'POST',headers:{{'Content-Type':'application/json'}},body:JSON.stringify({{ingredient_id:ingId,location:currentLoc}})}});
  let result=await r.json();
  if(result.ok){{snack('已移出 Removed');setTimeout(()=>location.reload(),500);}}else{{snack('失败 Failed');}}
}}
async function sameAsLast(){{
  let r=await fetch('/api/pantry/same-as-last',{{method:'POST',headers:{{'Content-Type':'application/json'}},body:JSON.stringify({{location:currentLoc}})}});
  let result=await r.json();
  if(result.ok){{snack('✓ 已确认: 和上次一样 Same as last');}}else{{snack('失败 Failed');}}
}}
function showSaved(){{
  let s=document.getElementById('saveIndicator');
  if(s){{s.style.display='block';setTimeout(()=>s.style.display='none',1500);}}
}}
async function notifyPurchase(reqId){{
  let r=await fetch('/api/purchase/update',{{method:'POST',headers:{{'Content-Type':'application/json'}},body:JSON.stringify({{id:reqId,status:'notified',by:'nanny'}})}});
  let result=await r.json();
  if(result.ok){{snack('已通知采购 Notified');location.reload();}}else{{snack('失败');}}
}}
async function markPurchased(reqId){{
  let r=await fetch('/api/purchase/update',{{method:'POST',headers:{{'Content-Type':'application/json'}},body:JSON.stringify({{id:reqId,status:'purchased',by:'nanny'}})}});
  let result=await r.json();
  if(result.ok){{snack('已标记购买 Purchased');location.reload();}}else{{snack('失败');}}
}}
init();
</script>"""


# ============================================================
# 库存录入页面（新 UX）
# ============================================================

def render_pantry_submit(role="nanny", location="shenzhen"):
    common = get_common_ingredients()
    # V4: 从 current_pantry 获取当前库存（预加载已选）
    pantry_data = get_current_pantry(location)
    current_items = pantry_data["items"] if pantry_data else []
    all_ings = get_all_ingredients()

    common_json = json.dumps([{"id": i["ingredient_id"], "cn": i["name_cn"], "en": i.get("name_en") or ""} for i in common], ensure_ascii=False)
    current_json = json.dumps([{"id": i["ingredient_id"], "cn": i["name_cn"], "en": i.get("name_en") or "", "status": i["status"]} for i in current_items], ensure_ascii=False)
    all_ings_json = json.dumps([{"id": i["ingredient_id"], "cn": i["name_cn"], "en": i["name_en"] or "", "aliases": i.get("aliases", []), "group": i.get("ingredient_group", "vegetable_mushroom"), "category": i.get("category", "")} for i in all_ings], ensure_ascii=False)
    pantry_count = len(current_items)

    # Group labels
    group_labels = {
        "protein": "肉 / 海鲜 Meat / Seafood",
        "vegetable_mushroom": "蔬菜 / 菌菇 Vegetable / Mushroom",
        "egg_tofu": "蛋 / 豆 Egg / Tofu",
        "staple_coarse": "主食 / 粗粮 Staple / Coarse Grain",
        "fruit": "水果 Fruit",
        "seasoning_sauce": "调味料 / 酱料 Seasoning / Sauce",
        "other": "其他 Other",
    }

    return f"""{page_head("管理库存 · Manage Pantry", "pantry", location)}
<div class="content">
<div class="card"><p>当前厨房 Kitchen: <strong>{LOCATIONS.get(location, location)}</strong> | 当前库存 <strong>{pantry_count}</strong> 项</p></div>

<div class="search-bar" style="position:static;padding:0">
<input type="text" id="ingSearch" placeholder="搜索食材 Search ingredient (name/aliases)..." oninput="filterIngs()">
</div>

<div id="searchResults" style="margin-top:8px"></div>

<div class="card">
<h3>快捷操作 Quick Actions</h3>
<div style="display:flex;gap:8px;margin-top:8px">
<button class="btn btn-outline" style="flex:1" onclick="clearAll()">清空全部 Clear All</button>
</div>
</div>

<div class="card">
<h3>常用食材 Common</h3>
<div class="ing-picker" id="commonList"></div>
</div>

<div class="card" id="selectedSection" style="display:none">
<h3>当前库存 + 新增 Current Pantry + Added (<span id="selectedCount">0</span>)</h3>
<div class="selected-list" id="selectedList"></div>
</div>

<button class="btn btn-primary" id="submitBtn" style="margin-top:12px" onclick="submitPantry()" disabled>保存库存变更 Save Changes</button>
</div>
{PAGE_FOOT}
<script>
const allIngs={all_ings_json};
const commonIngs={common_json};
const currentPantry={current_json};
let selected={{}};
let currentLoc='{location}';
let hasUnsavedChanges=false;

function init(){{
  // 渲染常用食材
  let cl=document.getElementById('commonList');
  cl.innerHTML=commonIngs.map(i=>`<div class="ing-chip" onclick="toggleIng('${{i.id}}','${{i.cn}}','${{i.en}}')">${{i.cn}} ${{i.en}}</div>`).join('');
  // V4: 预加载当前库存为已选
  currentPantry.forEach(i=>{{selected[i.id]={{cn:i.cn,en:i.en,status:i.status}};}});
  renderSelected();
}}
function filterIngs(){{
  let q=document.getElementById('ingSearch').value.toLowerCase().trim();
  let res=document.getElementById('searchResults');
  if(!q){{res.innerHTML='';return;}}
  // Search name_cn, name_en, AND aliases
  let matched=allIngs.filter(i=>{{
    if(i.cn.toLowerCase().includes(q))return true;
    if(i.en&&i.en.toLowerCase().includes(q))return true;
    if(i.aliases&&i.aliases.some(a=>a.toLowerCase().includes(q)))return true;
    return false;
  }}).slice(0,15);

  let html='';
  if(matched.length){{
    html=matched.map(i=>`<div class="ing-chip" onclick="toggleIng('${{i.id}}','${{i.cn}}','${{i.en}}')">${{i.cn}} ${{i.en||''}}</div>`).join('');
  }}

  // Always show "add new" option
  html+=`<div class="ing-add-chip" onclick="addNewIng('${{q}}')">+ 添加"-${{q}}-" Add New</div>`;

  res.innerHTML=html;
}}
function addNewIng(name){{
  // Create new ingredient via API
  fetch('/api/ingredients/add',{{
    method:'POST',headers:{{'Content-Type':'application/json'}},
    body:JSON.stringify({{name_cn:name}})
  }}).then(r=>r.json()).then(data=>{{
    if(data.ok){{
      // Add to allIngs
      allIngs.push({{id:data.ingredient_id,cn:name,en:'',aliases:[],group:'vegetable_mushroom'}});
      // Select it
      selected[data.ingredient_id]={{cn:name,en:'',status:'available'}};
      renderSelected();
      document.getElementById('ingSearch').value='';
      document.getElementById('searchResults').innerHTML='';
      snack('已添加新食材 Added: '+name);
      hasUnsavedChanges=true;
    }}else{{
      snack(data.error||'添加失败');
    }}
  }});
}}
function toggleIng(id,cn,en){{
  if(selected[id]){{delete selected[id];}}else{{selected[id]={{cn,en,status:'available'}};}}
  hasUnsavedChanges=true;
  renderSelected();
}}
function clearAll(){{
  selected={{}};
  hasUnsavedChanges=true;
  renderSelected();
}}
function toggleStatusSelected(id,status){{
  if(selected[id]){{
    // Toggle: if already this status, revert to available
    selected[id].status=selected[id].status===status?'available':status;
  }}
  hasUnsavedChanges=true;
  renderSelected();
}}
function removeIng(id){{
  delete selected[id];
  hasUnsavedChanges=true;
  renderSelected();
}}
function renderSelected(){{
  let keys=Object.keys(selected);
  let count=keys.length;
  document.getElementById('selectedCount').textContent=count;
  document.getElementById('submitBtn').disabled=count===0;
  let section=document.getElementById('selectedSection');
  let list=document.getElementById('selectedList');
  if(count===0){{section.style.display='none';return;}}
  section.style.display='block';
  list.innerHTML=keys.map(id=>{{
    let s=selected[id];
    let pfCls=s.status==='priority_use'?'active-priority':'';
    let exCls=s.status==='expiring'?'active-expiring':'';
    return `<div class="selected-item"><div><div class="name">${{s.cn}}</div><div class="name-en">${{s.en}}</div></div><div class="pantry-controls"><div class="pantry-status-group"><span class="st-btn ${{pfCls}}" onclick="toggleStatusSelected('${{id}}','priority_use')">优先用 Use First</span><span class="st-btn ${{exCls}}" onclick="toggleStatusSelected('${{id}}','expiring')">快过期 Expiring Soon</span></div><span class="pantry-used-up" onclick="removeIng('${{id}}')">用完 Used Up</span></div></div>`;
  }}).join('');
}}
async function submitPantry(){{
  let items=Object.entries(selected).map(([id,s])=>({{ingredient_id:id,status:s.status}}));
  let res=await fetch('/api/pantry/submit',{{
    method:'POST',headers:{{'Content-Type':'application/json'}},
    body:JSON.stringify({{items:items,location:currentLoc,submitted_by:'nanny'}})
  }});
  let result=await res.json();
  if(result.ok){{
    hasUnsavedChanges=false;
    let msg='库存已更新 Pantry Updated';
    if(result.summary) msg+=` (${{result.summary.added}}新增 ${{result.summary.updated}}更新 ${{result.summary.removed}}移除)`;
    snack(msg);
    setTimeout(()=>location.href='/pantry',1500);
  }}else{{snack(result.error||'保存失败 Save failed');}}
}}
init();
</script>"""


# ============================================================
# 历史菜单页面
# ============================================================

def render_history(role="owner", location="shenzhen"):
    menus = get_history_menus(30)

    if not menus:
        body = '<div class="empty"><h2>暂无历史记录 No History</h2></div>'
    else:
        meal_labels = {"breakfast": ("早", "Breakfast"), "lunch": ("午", "Lunch"),
                       "afternoon_snack": ("茶", "Snack"), "dinner": ("晚", "Dinner")}
        entries = []
        for m in menus:
            meal_html = ""
            for mt in ["breakfast", "lunch", "afternoon_snack", "dinner"]:
                dishes = m["meals"].get(mt, [])
                if not dishes:
                    continue
                cn_label, en_label = meal_labels[mt]
                names_zh = " ＋ ".join(d["name_cn"] for d in dishes)
                names_en = " + ".join((d.get("name_en") or "") for d in dishes if d.get("name_en"))
                # Bilingual: Chinese first, English below
                meal_html += f'<div class="history-meal"><span class="label">{cn_label} {en_label}</span><span class="dish-list">{names_zh}</span></div>'
                if names_en:
                    meal_html += f'<div class="history-meal"><span class="dish-list-en">{names_en}</span></div>'

            status_cls = f"status-{m['status']}"
            loc_label = LOCATIONS.get(m.get("location", ""), m.get("location", ""))
            confirmed_src = m.get("confirmed_source", "")
            push_status = m.get("push_status", "")

            entries.append(f"""<div class="history-entry">
<div class="history-date">{m["date"]} <span class="status-tag {status_cls}" style="float:right">{m["status"]}</span></div>
<div class="history-context">
<span>📍 {loc_label}</span>
<span>✓ {confirmed_src}</span>
<span>📡 {push_status}</span>
</div>
{meal_html}</div>""")

        body = "\n".join(entries)

    return f"""{page_head("历史菜单 · History", "history", location)}
<div class="content">{body}</div>
{PAGE_FOOT}"""


# ============================================================
# HTTP Handler
# ============================================================

class AppHandler(BaseHTTPRequestHandler):
    def do_GET(self):
        parsed = urlparse(self.path)
        path = parsed.path
        qs = parse_qs(parsed.query)
        location = get_location_from_cookie(self.headers.get("Cookie", ""))
        role = qs.get("role", ["owner"])[0]

        # 静态资源
        if path.startswith("/photos/"):
            self.serve_photo(path[8:])
            return

        # 页面路由
        if path == "/" or path == "/tomorrow":
            self.send_html(render_tomorrow(role, location))
        elif path == "/pantry":
            self.send_html(render_pantry(qs.get("role", ["nanny"])[0], location))
        elif path == "/pantry/submit":
            # V6: pantry submit 已合并到主页面，重定向
            self.send_response(302)
            self.send_header("Location", f"/pantry?role={qs.get('role', ['nanny'])[0]}")
            self.end_headers()
        elif path == "/dishes":
            self.send_html(render_dishes(role, location))
        elif path == "/history":
            self.send_html(render_history(role, location))

        # API 路由
        elif path == "/api/dishes":
            cat = qs.get("category", [None])[0]
            search = qs.get("search", [""])[0]
            dishes = get_all_dishes(category=cat, search=search)
            self.send_json(dishes)
        elif path.startswith("/api/dishes/"):
            parts = path.split("/")
            if len(parts) == 5 and parts[4] == "availability-debug":
                # V5 Section 22: Availability Debug API
                dish_id = parts[3]
                loc = qs.get("location", [location])[0]
                debug = check_dish_availability_debug(dish_id, loc)
                self.send_json(debug)
            elif len(parts) == 4 and parts[3] == "availability":
                # POST handled in do_POST
                self.send_error(405, "Use POST")
            else:
                dish_id = parts[-1]
                dish = get_dish_detail(dish_id)
                if dish:
                    self.send_json(dish)
                else:
                    self.send_json({"error": "not found"}, 404)
        elif path == "/api/ingredients":
            self.send_json(get_all_ingredients())
        elif path == "/api/tomorrow":
            tomorrow = get_tomorrow_date()
            ensure_tomorrow_menu(location)
            self.send_json(get_menu_with_dishes(tomorrow))
        elif path == "/api/history":
            days = int(qs.get("days", ["30"])[0])
            self.send_json(get_history_menus(days))
        elif path == "/api/pantry":
            # V4: 从 current_pantry 读取
            pantry = get_current_pantry(location)
            self.send_json(pantry or {})
        elif path == "/api/pantry/last":
            # V4: 兼容旧接口，返回 current_pantry items
            pantry = get_current_pantry(location)
            self.send_json(pantry["items"] if pantry else [])
        elif path == "/api/purchase-requests":
            reqs = get_purchase_requests(location=location, status=qs.get("status", [None])[0])
            self.send_json(reqs)
        elif path == "/api/categories":
            self.send_json(get_categories())
        elif path == "/api/diners":
            self.send_json(get_all_diners())
        else:
            self.send_error(404, "Not Found")

    def do_POST(self):
        parsed = urlparse(self.path)
        path = parsed.path
        location = get_location_from_cookie(self.headers.get("Cookie", ""))
        body = self.read_json()

        if path == "/api/pantry/submit":
            # V4: 增量保存库存变更
            loc = body.get("location", location)
            summary = save_pantry_changes(
                loc,
                body["items"],
                submitted_by=body.get("submitted_by", "nanny")
            )
            self.send_json({"ok": True, "summary": summary})

        elif path == "/api/pantry/add":
            # V6: 单项增量添加（不影响其他食材）
            loc = body.get("location", location)
            result = add_ingredient_to_pantry(
                loc, body["ingredient_id"],
                status=body.get("status", "available"),
                submitted_by=body.get("submitted_by", "nanny")
            )
            self.send_json(result)

        elif path == "/api/pantry/same-as-last":
            # V6: "和上次一样" — 不修改库存，只记录确认时间
            loc = body.get("location", location)
            result = confirm_pantry_unchanged(loc, body.get("submitted_by", "nanny"))
            self.send_json(result)

        elif path == "/api/pantry/update_status":
            # V4: 单项状态更新
            from db import get_db
            loc = body.get("location", location)
            conn = get_db()
            try:
                conn.execute(
                    "UPDATE current_pantry SET status = ?, updated_at = datetime('now') "
                    "WHERE location = ? AND ingredient_id = ? AND is_active = 1",
                    (body["status"], loc, body["ingredient_id"])
                )
                # V5: 递增 inventory_version
                _increment_inventory_version(conn, loc)
                conn.commit()
                # V5: 清除 availability 缓存
                _invalidate_availability_cache(loc)
                self.send_json({"ok": True})
            finally:
                conn.close()

        elif path == "/api/pantry/remove":
            # V4: 从当前库存移除单项
            from db import get_db
            loc = body.get("location", location)
            conn = get_db()
            try:
                conn.execute(
                    "UPDATE current_pantry SET is_active = 0, updated_at = datetime('now') "
                    "WHERE location = ? AND ingredient_id = ?",
                    (loc, body["ingredient_id"])
                )
                # V5: 递增 inventory_version
                _increment_inventory_version(conn, loc)
                conn.commit()
                # V5: 清除 availability 缓存
                _invalidate_availability_cache(loc)
                self.send_json({"ok": True})
            finally:
                conn.close()

        elif path == "/api/ingredients/add":
            # Add new ingredient (needs_review = true)
            name_cn = body.get("name_cn", "").strip()
            if not name_cn:
                self.send_json({"ok": False, "error": "name_cn required"})
                return
            conn = get_db()
            try:
                # Generate ingredient_id from name
                ing_id = name_cn.lower().replace(" ", "_")
                # Check if already exists
                existing = conn.execute(
                    "SELECT ingredient_id FROM ingredients WHERE ingredient_id = ? OR name_cn = ?",
                    (ing_id, name_cn)
                ).fetchone()
                if existing:
                    self.send_json({"ok": True, "ingredient_id": existing["ingredient_id"], "exists": True})
                    return
                conn.execute(
                    "INSERT INTO ingredients (ingredient_id, name_cn, name_en, aliases, category, ingredient_group) "
                    "VALUES (?, ?, '', '[]', '', 'vegetable_mushroom')",
                    (ing_id, name_cn)
                )
                conn.commit()
                log_event("ingredient_added", "ingredient", ing_id, {"name_cn": name_cn, "needs_review": True})
                self.send_json({"ok": True, "ingredient_id": ing_id})
            finally:
                conn.close()

        elif path == "/api/dishes/availability":
            dish_ids = body.get("dish_ids", [])
            loc = body.get("location", location)
            avail = get_dish_availability(dish_ids, loc)
            self.send_json(avail)

        elif path == "/api/dishes/recommend":
            rec = get_dish_recommendations(
                body.get("meal_type", ""),
                body.get("current_dish_id", ""),
                body.get("category_id", ""),
                body.get("location", location),
            )
            self.send_json(rec)

        elif path == "/api/tomorrow/add":
            ok = add_dish_to_menu(body["menu_id"], body["dish_id"], body["meal_type"])
            self.send_json({"ok": ok})

        elif path == "/api/tomorrow/remove":
            ok, msg = remove_dish_from_menu(body["menu_id"], body["menu_item_id"])
            self.send_json({"ok": ok, "error": msg if not ok else None})

        elif path == "/api/tomorrow/replace":
            ok, msg = replace_dish_in_menu(body["menu_id"], body["menu_item_id"], body["new_dish_id"])
            self.send_json({"ok": ok, "error": msg if not ok else None})

        elif path == "/api/tomorrow/ai-fill":
            meal_type = body.get("meal_type")
            ok, msg, review = ai_fill_menu(body["menu_id"], body.get("location", location), meal_type=meal_type)
            self.send_json({"ok": ok, "error": msg if not ok else None, "review": review})

        elif path == "/api/tomorrow/repair":
            ok, msg, review = repair_menu(body["menu_id"], body.get("location", location), seed=body.get("seed"))
            self.send_json({"ok": ok, "error": msg if not ok else None, "review": review})

        elif path == "/api/tomorrow/confirm":
            # V3: confirm_menu returns (ok, msg, warnings)
            result = confirm_menu(body["menu_id"])
            if len(result) == 3:
                ok, msg, warnings = result
            else:
                ok, msg = result
                warnings = []
            self.send_json({"ok": ok, "error": msg if not ok else None, "warnings": warnings, "message": msg})

        elif path == "/api/tomorrow/revert":
            # V3: Confirmed → Edit Menu → Reconfirm flow
            ok, msg = revert_to_draft(body["menu_id"])
            self.send_json({"ok": ok, "error": msg if not ok else None, "message": msg})

        elif path == "/api/tomorrow/push":
            # V3: Push menu (only if VV confirmed)
            ok, msg = push_menu(body["menu_id"])
            self.send_json({"ok": ok, "error": msg if not ok else None, "message": msg})

        elif path == "/api/purchase/update":
            update_purchase_status(body["id"], body["status"], resolved_by=body.get("by", "nanny"))
            self.send_json({"ok": True})

        elif path == "/api/tomorrow/diners":
            ok = update_menu_diners(body["menu_id"], body["diners"])
            self.send_json({"ok": ok})

        else:
            self.send_error(404, "Not Found")

    def serve_photo(self, filename):
        filepath = os.path.join(BASE_DIR, "photos", filename)
        if os.path.exists(filepath):
            with open(filepath, "rb") as f:
                data = f.read()
            self.send_response(200)
            ext = filename.rsplit(".", 1)[-1].lower()
            ct = {"jpg": "image/jpeg", "jpeg": "image/jpeg", "png": "image/png"}.get(ext, "application/octet-stream")
            self.send_header("Content-Type", ct)
            self.send_header("Content-Length", str(len(data)))
            self.send_header("Cache-Control", "max-age=3600")
            self.end_headers()
            self.wfile.write(data)
        else:
            self.send_error(404, "Photo not found")

    def send_html(self, content):
        data = content.encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def send_json(self, obj, status=200):
        data = json.dumps(obj, ensure_ascii=False, default=str).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def read_json(self):
        length = int(self.headers.get("Content-Length", 0))
        if length:
            return json.loads(self.rfile.read(length))
        return {}

    def log_message(self, *args):
        pass


def main():
    # 确保明天菜单存在
    ensure_tomorrow_menu("shenzhen")
    server = ThreadingHTTPServer(("0.0.0.0", PORT), AppHandler)
    print(f"[OK] H5 应用已启动: http://localhost:{PORT}")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\n[BYE]")
        server.shutdown()


if __name__ == "__main__":
    main()
