#!/usr/bin/env python3
"""
家庭菜单管家 - 保姆提醒脚本 (V3 新增)

功能：
  19:00 - 如果明日菜单尚未 confirmed，提醒保姆去问 VV
  20:00 - 如果仍未 confirmed，再次提醒保姆
  21:00-21:30 - 如果仍未 confirmed，直接提醒 VV

推送方式：PushPlus (与菜单推送相同)
运行方式：可由 cron / GitHub Actions / 本地调度器触发

用法：
  python nanny_reminder.py --time 19:00
  python nanny_reminder.py --time 20:00
  python nanny_reminder.py --time 21:00
"""

import json
import os
import sys
import argparse
from datetime import date, datetime, timedelta
from push_service import PushPlusClient, PushError, get_h5_base_url

# 本地模块
try:
    from db import get_db, get_config
    from menu_service import get_tomorrow_date, get_menu_with_dishes
    DB_AVAILABLE = True
except ImportError:
    DB_AVAILABLE = False

CONFIG_FILE = "config.json"


def load_config():
    try:
        with open(CONFIG_FILE, "r", encoding="utf-8") as f:
            return json.load(f)
    except (FileNotFoundError, json.JSONDecodeError):
        return {}


def get_tomorrow_menu_status():
    """检查明日菜单状态。DB 不可用时返回 (None, error) 触发兜底逻辑。"""
    if not DB_AVAILABLE:
        return None, "SQLite 不可用 (GitHub Actions 环境)"

    try:
        tomorrow = get_tomorrow_date()
        conn = get_db()
        try:
            menu = conn.execute(
                "SELECT id, status, confirmed_at FROM menus WHERE date = ?",
                (tomorrow,)
            ).fetchone()
            if not menu:
                return None, "明日菜单尚未生成"
            return dict(menu), None
        finally:
            conn.close()
    except Exception as e:
        return None, f"DB 查询失败: {e}"


def send_pushplus(token, topic, title, content):
    try:
        PushPlusClient(token, topic).send(title, content)
        print("[OK] PushPlus 提醒发送成功!")
        return True
    except PushError as e:
        print(f"[ERROR] 发送异常: {e}")
        return False


def send_reminder(reminder_type, menu_status, error_msg=None):
    """发送提醒消息"""
    config = load_config()
    token = os.environ.get("PUSHPLUS_TOKEN", "")
    topic = os.environ.get("PUSHPLUS_TOPIC", "home-menu")

    if not token:
        print("[ERROR] 未设置 PUSHPLUS_TOKEN")
        return False

    # H5 链接
    try:
        h5_base = get_h5_base_url()
    except PushError as exc:
        print(f"[ERROR] {exc}")
        return False
    tomorrow_link = f"{h5_base}/tomorrow"

    if reminder_type == "nanny_first":
        # 19:00 第一次提醒保姆
        title = "菜单提醒：请确认明日菜单"
        content = f"""**19:00 保姆提醒**

请问 VV 明天想吃什么，并提醒她选择明日菜单。

Please ask VV what she would like for tomorrow and remind her to choose the menu.

明日菜单状态：{'已确认 Confirmed' if menu_status == 'confirmed' else '待确认 Pending'}

H5 链接：{tomorrow_link}
"""
    elif reminder_type == "nanny_second":
        # 20:00 第二次提醒保姆
        title = " urgent：明日菜单尚未确认"
        content = f"""**20:00 再次提醒**

明日菜单还没有确认，请提醒 VV 选择并确认。

Tomorrow's menu has not been confirmed yet.
Please remind VV to review and confirm it.

明日菜单状态：{'已确认 Confirmed' if menu_status == 'confirmed' else '待确认 Pending'}

H5 链接：{tomorrow_link}
"""
    elif reminder_type == "vv_final":
        # 21:00-21:30 直接提醒 VV
        title = "明日菜单待确认"
        content = f"""**21:00 VV 提醒**

明日菜单尚未确认。
Tomorrow's menu is awaiting your confirmation.

H5 链接：{tomorrow_link}
"""
    else:
        return False

    return send_pushplus(token, topic, title, content)


def main():
    parser = argparse.ArgumentParser(description="保姆提醒脚本")
    parser.add_argument("--time", type=str, required=True,
                        choices=["19:00", "20:00", "21:00"],
                        help="提醒时间点")
    args = parser.parse_args()

    print("=" * 50)
    print(f"保姆提醒脚本 - {args.time}")
    print(f"运行时间: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
    print("=" * 50)

    # 检查明日菜单状态
    menu_row, error = get_tomorrow_menu_status()

    if error:
        print(f"[ERROR] {error}; 为避免错误提醒，本次不发送")
        return 1

    status = menu_row["status"]
    print(f"明日菜单状态: {status}")

    # 如果已确认，不发送提醒
    if status == "confirmed" or status == "pushed":
        print("[SKIP] 明日菜单已确认/已推送，无需提醒")
        return

    # 根据时间点发送不同提醒
    if args.time == "19:00":
        print("[SEND] 19:00 第一次保姆提醒")
        send_reminder("nanny_first", status)
    elif args.time == "20:00":
        print("[SEND] 20:00 第二次保姆提醒")
        send_reminder("nanny_second", status)
    elif args.time == "21:00":
        print("[SEND] 21:00 VV 直接提醒")
        send_reminder("vv_final", status)


if __name__ == "__main__":
    main()
