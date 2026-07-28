#!/usr/bin/env python3
"""
家庭菜单管家 - 每日菜单推送脚本
从 menu_data.json 读取当天菜单，格式化后通过 PushPlus 推送到微信。
设计为在 GitHub Actions 中运行，仅使用 Python 内置库。
"""

import json
import os
import urllib.request
import urllib.error
from datetime import date, datetime


# ========== 配置 ==========
MENU_FILE = "menu_data.json"
CONFIG_FILE = "config.json"
TIPS_FILE = "seasonal_tips.json"
PUSHPLUS_API = "http://www.pushplus.plus/send"
CYCLE_START_DEFAULT = "2026-07-28"


def load_json(filepath):
    """加载 JSON 文件"""
    try:
        with open(filepath, "r", encoding="utf-8") as f:
            return json.load(f)
    except FileNotFoundError:
        print(f"[ERROR] 文件不存在: {filepath}")
        return None
    except json.JSONDecodeError as e:
        print(f"[ERROR] JSON 解析失败: {filepath} - {e}")
        return None


def calculate_day_number(cycle_start_str, total_days):
    """计算今天是周期内的第几天"""
    try:
        cycle_start = datetime.strptime(cycle_start_str, "%Y-%m-%d").date()
    except ValueError:
        print(f"[WARN] cycle_start 格式错误: {cycle_start_str}, 使用默认值 {CYCLE_START_DEFAULT}")
        cycle_start = datetime.strptime(CYCLE_START_DEFAULT, "%Y-%m-%d").date()

    today = date.today()
    days_diff = (today - cycle_start).days
    day_number = (days_diff % total_days) + 1
    return day_number


def get_seasonal_tips(tips_data, month):
    """获取当月时令建议"""
    if not tips_data:
        return []
    month_str = str(month)
    if month_str in tips_data.get("tips", {}):
        return tips_data["tips"][month_str].get("tips", [])
    return []


def format_menu_message(menu_entry, day_number, tips, config):
    """格式化菜单为 Markdown 消息"""
    today_str = date.today().strftime("%Y年%m月%d日")

    # 根据 cook_language 决定语言排序
    cook_lang = config.get("cook_language", "zh")
    zh_first = cook_lang == "zh"

    lines = []
    lines.append(f"# 🍽️ 家庭菜单 | Day {day_number} | {today_str}")
    lines.append("")

    meals = [
        ("🌅 早餐 Breakfast", "breakfast"),
        ("☀️ 午餐 Lunch", "lunch"),
        ("🍵 下午茶/加餐 Afternoon Snack", "afternoon_snack"),
        ("🌙 晚餐 Dinner", "dinner"),
        ("😴 夜宵/睡前调理 Late Night", "late_night"),
    ]

    for emoji_label, meal_key in meals:
        meal = menu_entry.get(meal_key, {})
        zh = meal.get("zh", "")
        en = meal.get("en", "")

        if not zh or zh == "无需安排":
            lines.append(f"## {emoji_label}")
            lines.append("无需安排 | No arrangement needed")
            lines.append("")
            continue

        lines.append(f"## {emoji_label}")
        if zh_first:
            lines.append(f"**{zh}**")
            if en:
                lines.append(f"*{en}*")
        else:
            if en:
                lines.append(f"**{en}**")
            lines.append(f"*{zh}*")
        lines.append("")

    # 当日备注
    notes = menu_entry.get("notes", {})
    notes_zh = notes.get("zh", "")
    if notes_zh and notes_zh not in ["无", ""]:
        lines.append("## 📌 当日备注 Notes")
        lines.append(f"{'**' + notes_zh + '**' if zh_first else '*' + notes_zh + '*'}")
        if notes.get("en"):
            lines.append(f"*{notes['en']}*")
        lines.append("")

    # 时令建议
    if tips:
        lines.append("## 🌿 时令饮食建议 Seasonal Tips")
        for tip in tips:
            tip_zh = tip.get("zh", "")
            tip_en = tip.get("en", "")
            if zh_first:
                lines.append(f"- {tip_zh}")
                if tip_en:
                    lines.append(f"  *{tip_en}*")
            else:
                if tip_en:
                    lines.append(f"- {tip_en}")
                lines.append(f"  *{tip_zh}*")
        lines.append("")

    lines.append("---")
    lines.append("📱 由家庭菜单管家自动推送 | Auto-pushed by Family Menu Manager")

    return "\n".join(lines)


def send_pushplus(token, topic, title, content):
    """通过 PushPlus API 发送消息"""
    payload = {
        "token": token,
        "title": title,
        "content": content,
        "template": "markdown",
    }

    # 如果有群组 topic，添加到请求中
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
            if result.get("code") == 200:
                print(f"[OK] PushPlus 推送成功!")
                print(f"  消息ID: {result.get('data', 'N/A')}")
                return True
            else:
                print(f"[FAIL] PushPlus 推送失败: {result.get('msg', 'Unknown error')}")
                print(f"  完整返回: {result}")
                return False
    except urllib.error.URLError as e:
        print(f"[ERROR] 网络请求失败: {e}")
        return False
    except Exception as e:
        print(f"[ERROR] 推送异常: {e}")
        return False


def main():
    print("=" * 50)
    print("家庭菜单管家 - 每日推送")
    print(f"运行时间: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
    print("=" * 50)

    # 1. 加载配置
    config = load_json(CONFIG_FILE) or {}
    menu_data = load_json(MENU_FILE)
    tips_data = load_json(TIPS_FILE)

    if not menu_data:
        print("[FATAL] 无法加载菜单数据，退出")
        return

    # 2. 计算今天是第几天
    cycle_start = menu_data.get("cycle_start", CYCLE_START_DEFAULT)
    total_days = menu_data.get("total_days", 20)
    day_number = calculate_day_number(cycle_start, total_days)

    print(f"周期起始日: {cycle_start}")
    print(f"周期总天数: {total_days}")
    print(f"今天是第: {day_number} 天")

    # 3. 获取今天菜单
    menu_list = menu_data.get("menu", [])
    if not menu_list or day_number > len(menu_list):
        print(f"[ERROR] 菜单数据不完整，第{day_number}天不存在")
        return

    menu_entry = menu_list[day_number - 1]

    # 4. 获取时令建议
    current_month = date.today().month
    tips = get_seasonal_tips(tips_data, current_month)
    print(f"当月时令建议: {len(tips)} 条")

    # 5. 格式化消息
    content = format_menu_message(menu_entry, day_number, tips, config)
    today_str = date.today().strftime("%m月%d日")
    title = f"家庭菜单 Day{day_number} | {today_str}"

    print(f"\n--- 消息预览 ---")
    print(content[:500])
    print("--- 预览结束 ---\n")

    # 6. 发送推送
    # 从环境变量获取 PushPlus token（GitHub Secrets 注入）
    pushplus_token = os.environ.get("PUSHPLUS_TOKEN", "")
    pushplus_topic = os.environ.get("PUSHPLUS_TOPIC", "home-menu")

    if not pushplus_token:
        print("[ERROR] 未设置 PUSHPLUS_TOKEN 环境变量")
        print("  在 GitHub 仓库 Settings → Secrets → Actions 中添加 PUSHPLUS_TOKEN")
        return

    print(f"PushPlus Topic: {pushplus_topic}")
    success = send_pushplus(pushplus_token, pushplus_topic, title, content)

    if success:
        print("\n✅ 推送完成!")
    else:
        print("\n❌ 推送失败，请检查错误信息")
        exit(1)


if __name__ == "__main__":
    main()
