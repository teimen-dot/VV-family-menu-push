#!/usr/bin/env python3
"""
V11: VV Preference Service
统计 VV 在 Confirmed 菜单中最终保留的菜品，用于推荐排序加分。

只统计：
  - 最终 Confirm 时仍保留在菜单中的菜
  - VV 手动 Add 的菜
  - VV 手动 Replace 后保留的菜
  - AI 推荐但 VV 最终 Confirm 保留的菜

不计：
  - AI 自动生成但菜单没有 Confirm
  - AI 推荐后 VV 删除
  - Draft 最后废弃 / skipped
"""

from datetime import datetime, date, timedelta
from db import get_db, log_event

# VV Preference 评分权重 (soft score, added to ScoringEngine)
W_VV_CONFIRM_HIGH = 40     # vv_confirm_count >= 10: +40
W_VV_CONFIRM_MED = 20      # vv_confirm_count >= 3: +20
W_VV_CONFIRM_30D = 20     # 30天内高频确认: +0~20
W_VV_RECENT_PENALTY = -30  # 最近3天吃过: -30

def record_vv_confirm(menu_id):
    """
    V11: 当菜单被 VV Confirm 时，记录菜品偏好统计。
    遍历该菜单所有菜品，对每道菜 increment vv_confirm_count。
    同时计算 30 天内确认次数。
    """
    conn = get_db()
    try:
        menu = conn.execute("SELECT date FROM menus WHERE id = ?", (menu_id,)).fetchone()
        if not menu:
            return 0

        items = conn.execute(
            "SELECT dish_id FROM menu_items WHERE menu_id = ? AND dish_id IS NOT NULL",
            (menu_id,)
        ).fetchall()

        now = datetime.now().isoformat()
        count = 0
        for item in items:
            did = item["dish_id"]
            if not did or not did.startswith("dish_"):
                continue

            # UPSERT: increment counts
            existing = conn.execute(
                "SELECT vv_confirm_count, vv_confirm_count_30d, last_confirmed_at "
                "FROM dish_preference_stats WHERE dish_id = ?",
                (did,)
            ).fetchone()

            if existing:
                # Calculate 30d count: increment only if last confirmed > 1 day ago
                # (prevents same-day reconfirm double counting)
                last = existing["last_confirmed_at"] or ""
                today = date.today().isoformat()
                if last[:10] != today:
                    new_30d = existing["vv_confirm_count_30d"] + 1
                else:
                    new_30d = existing["vv_confirm_count_30d"]

                conn.execute(
                    "UPDATE dish_preference_stats SET "
                    "vv_confirm_count = vv_confirm_count + 1, "
                    "vv_confirm_count_30d = ?, "
                    "last_confirmed_at = ?, "
                    "last_selected_at = ? "
                    "WHERE dish_id = ?",
                    (new_30d, now, now, did)
                )
            else:
                conn.execute(
                    "INSERT INTO dish_preference_stats "
                    "(dish_id, vv_confirm_count, vv_confirm_count_30d, last_confirmed_at, last_selected_at) "
                    "VALUES (?, 1, 1, ?, ?)",
                    (did, now, now)
                )
            count += 1

        conn.commit()
        log_event("vv_preferences_recorded", "menu", str(menu_id), {
            "dishes_counted": count
        })
        return count
    finally:
        conn.close()


def _prune_30d_counts():
    """定期清理 30 天外的确认计数（可选，不影响运行）"""
    conn = get_db()
    try:
        cutoff = (date.today() - timedelta(days=30)).isoformat()
        conn.execute(
            "UPDATE dish_preference_stats SET vv_confirm_count_30d = 0 "
            "WHERE last_confirmed_at < ?",
            (cutoff,)
        )
        conn.commit()
    finally:
        conn.close()


def get_preference_scores(dish_ids):
    """
    V11: 批量获取菜品偏好评分。
    返回 {dish_id: score} — score 范围 0~60 (confirm bonus + 30d bonus)
    不含 negative penalty（penalty 由 history_3day 处理）。
    """
    if not dish_ids:
        return {}

    conn = get_db()
    try:
        placeholders = ",".join("?" * len(dish_ids))
        rows = conn.execute(
            f"SELECT dish_id, vv_confirm_count, vv_confirm_count_30d, last_confirmed_at "
            f"FROM dish_preference_stats WHERE dish_id IN ({placeholders})",
            dish_ids
        ).fetchall()

        result = {}
        for r in rows:
            score = 0
            cc = r["vv_confirm_count"] or 0
            cc30 = r["vv_confirm_count_30d"] or 0

            # High confirm: >=10 → +40, >=3 → +20
            if cc >= 10:
                score += 40
            elif cc >= 3:
                score += 20

            # 30-day confirm: proportional bonus (max +20)
            if cc30 >= 5:
                score += 20
            elif cc30 >= 2:
                score += min(cc30 * 4, 20)

            result[r["dish_id"]] = score
        return result
    finally:
        conn.close()


def get_preference_stats(dish_id):
    """获取单道菜的偏好统计"""
    conn = get_db()
    try:
        row = conn.execute(
            "SELECT * FROM dish_preference_stats WHERE dish_id = ?",
            (dish_id,)
        ).fetchone()
        return dict(row) if row else None
    finally:
        conn.close()
