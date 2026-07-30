#!/usr/bin/env python3
"""
家庭菜单管家 - 库存与采购闭环模块

流程：保姆录库存 → 老板点菜 → 缺货检测 → 采购任务 → PushPlus通知 → 采购完成
"""

import os
import json
import urllib.request
from datetime import date, datetime, timedelta
from db import get_db, log_event, get_config

PUSHPLUS_API = "https://www.pushplus.plus/send"


# ============================================================
# 库存录入
# ============================================================

def submit_inventory(location, inv_date, items, submitted_by="nanny", notes=None):
    """
    保姆提交库存。
    items: list of {ingredient_id, status, notes?}
    status: available / priority_use / expiring / out_of_stock
    返回: inventory_id
    """
    conn = get_db()
    try:
        # 创建库存记录（UPSERT）
        conn.execute(
            "INSERT INTO inventory (location, date, submitted_by, submitted_at, status, notes) "
            "VALUES (?, ?, ?, ?, 'submitted', ?) "
            "ON CONFLICT(location, date) DO UPDATE SET "
            "submitted_by=excluded.submitted_by, submitted_at=excluded.submitted_at, "
            "status='submitted', notes=excluded.notes",
            (location, inv_date, submitted_by, datetime.now().isoformat(), notes)
        )
        conn.commit()

        # 查询实际的 inventory_id（UPSERT 后 lastrowid 不可靠）
        row = conn.execute(
            "SELECT id FROM inventory WHERE location = ? AND date = ?",
            (location, inv_date)
        ).fetchone()
        inv_id = row["id"]

        # 清除旧条目，重新写入
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
            "submitted_by": submitted_by
        })
        return inv_id
    finally:
        conn.close()


def get_latest_inventory(location, before_date=None):
    """
    获取指定地点最新已提交的库存。
    返回: {inventory_id, date, items: [{ingredient_id, status, name_cn}]}
    """
    conn = get_db()
    try:
        if before_date:
            row = conn.execute(
                "SELECT id, date, location FROM inventory "
                "WHERE location = ? AND date <= ? AND status = 'submitted' "
                "ORDER BY date DESC LIMIT 1",
                (location, before_date)
            ).fetchone()
        else:
            row = conn.execute(
                "SELECT id, date, location FROM inventory "
                "WHERE location = ? AND status = 'submitted' "
                "ORDER BY date DESC LIMIT 1",
                (location,)
            ).fetchone()

        if not row:
            return None

        items = conn.execute(
            "SELECT ii.ingredient_id, ii.status, ii.notes, i.name_cn, i.name_en "
            "FROM inventory_items ii "
            "JOIN ingredients i ON ii.ingredient_id = i.ingredient_id "
            "WHERE ii.inventory_id = ?",
            (row["id"],)
        ).fetchall()

        return {
            "inventory_id": row["id"],
            "date": row["date"],
            "location": row["location"],
            "items": [dict(item) for item in items]
        }
    finally:
        conn.close()


def get_available_ingredient_ids(location, before_date=None):
    """获取可用食材ID集合（available + priority_use + expiring）"""
    inv = get_latest_inventory(location, before_date)
    if not inv:
        return set(), set(), set()

    available = set()
    priority = set()
    expiring = set()

    for item in inv["items"]:
        ing_id = item["ingredient_id"]
        if item["status"] in ("available", "priority_use", "expiring"):
            available.add(ing_id)
        if item["status"] == "priority_use":
            priority.add(ing_id)
        if item["status"] == "expiring":
            expiring.add(ing_id)

    return available, priority, expiring


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
        # 获取菜品所需食材
        placeholders = ",".join("?" * len(dish_ids))
        rows = conn.execute(
            f"SELECT di.dish_id, di.ingredient_id, d.name_cn as dish_name, "
            f"i.name_cn as ingredient_name "
            f"FROM dish_ingredients di "
            f"JOIN dishes d ON di.dish_id = d.id "
            f"JOIN ingredients i ON di.ingredient_id = i.ingredient_id "
            f"WHERE di.dish_id IN ({placeholders})",
            dish_ids
        ).fetchall()

        # 获取库存
        available, _, _ = get_available_ingredient_ids(location, target_date)

        shortages = []
        for r in rows:
            if r["ingredient_id"] not in available:
                shortages.append({
                    "dish_id": r["dish_id"],
                    "dish_name": r["dish_name"],
                    "ingredient_id": r["ingredient_id"],
                    "ingredient_name": r["ingredient_name"],
                    "missing": True
                })

        return shortages
    finally:
        conn.close()


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
    """通过 PushPlus API 发送消息"""
    payload = {
        "token": token,
        "title": title,
        "content": content,
        "template": "markdown",
    }
    if topic:
        payload["topic"] = topic

    data = json.dumps(payload).encode("utf-8")
    req = urllib.request.Request(
        PUSHPLUS_API,
        data=data,
        headers={"Content-Type": "application/json"},
        method="POST",
    )

    try:
        with urllib.request.urlopen(req, timeout=30) as response:
            result = json.loads(response.read().decode("utf-8"))
            return result.get("code") == 200
    except Exception as e:
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
        token = os.environ.get("PUSHPLUS_TOKEN", get_config("pushplus_token", ""))
        topic = os.environ.get("PUSHPLUS_TOPIC", get_config("pushplus_topic", "home-menu"))

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
