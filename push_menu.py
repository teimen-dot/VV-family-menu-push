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
MANIFEST_FILE = "photo_manifest.json"
PUSHPLUS_API = "https://www.pushplus.plus/send"
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


def load_photo_manifest():
    """加载菜品照片映射，不存在时返回空字典（优雅降级）"""
    try:
        with open(MANIFEST_FILE, "r", encoding="utf-8") as f:
            return json.load(f)
    except (FileNotFoundError, json.JSONDecodeError):
        return {}


def find_meal_photos(zh_meal_str, manifest, github_raw_base):
    """
    从一餐的菜品字符串中找到所有有照片的菜品，返回图片 HTML 列表。
    匹配逻辑：先全名匹配，再用 " / " 前的部分匹配；去重避免同一张照片重复出现。
    """
    if not manifest:
        return []

    photos = []
    seen_files = set()
    dishes = zh_meal_str.split(" ＋ ")
    for dish in dishes:
        dish = dish.strip()
        candidates = [dish]
        # " / " 前的部分匹配（如 "清炒豆苗 / 豆苗菜" → "清炒豆苗"）
        if " / " in dish:
            candidates.append(dish.split(" / ")[0].strip())

        for candidate in candidates:
            if candidate in manifest:
                photo_file = manifest[candidate].get("file", "")
                if photo_file and photo_file not in seen_files:
                    photos.append(
                        f"<img src='{github_raw_base}/photos/{photo_file}' width='100%' "
                        f"style='border-radius:6px;object-fit:cover;aspect-ratio:1/1;'>"
                    )
                    seen_files.add(photo_file)
                    break
    return photos


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


def format_menu_message(menu_entry, day_number, tips, config, manifest):
    """格式化菜单为 HTML 消息（杂志风格 + iPhone 适配）"""
    today_str = date.today().strftime("%Y.%m.%d")

    # 根据 cook_language 决定语言排序
    cook_lang = config.get("cook_language", "zh")
    zh_first = cook_lang == "zh"

    # GitHub raw URL 基地址
    github_raw_base = config.get(
        "github_raw_base",
        "https://raw.githubusercontent.com/teimen-dot/VV-family-menu-push/main",
    )

    # 星期几中文
    weekday_zh = ["星期一", "星期二", "星期三", "星期四", "星期五", "星期六", "星期日"][date.today().weekday()]

    lines = []

    # ========== 杂志风格页头 ==========
    lines.append("<div style='font-family:-apple-system,BlinkMacSystemFont,\"PingFang SC\",\"Helvetica Neue\",sans-serif;max-width:680px;margin:0 auto;padding:18px 16px;background:#faf7f2;color:#3a3530;'>")

    # 顶部装饰横线
    lines.append("<div style='display:flex;align-items:center;gap:10px;margin-bottom:8px;'>")
    lines.append("<div style='width:24px;height:1px;background:#b8a89a;'></div>")
    lines.append("<div style='font-size:10px;letter-spacing:3px;color:#b8a89a;text-transform:uppercase;'>FAMILY MENU</div>")
    lines.append("<div style='flex:1;height:1px;background:#b8a89a;'></div>")
    lines.append("</div>")

    # 主标题区
    lines.append("<div style='text-align:center;padding:14px 0 6px;'>")
    lines.append(f"<div style='font-family:Georgia,serif;font-size:30px;font-weight:300;color:#2c2620;letter-spacing:8px;line-height:1.2;'>家庭菜单</div>")
    lines.append(f"<div style='font-family:Georgia,serif;font-style:italic;font-size:14px;color:#9a8a7a;margin-top:4px;letter-spacing:1px;'>Family Table &middot; Day {day_number}</div>")
    lines.append(f"<div style='font-size:12px;color:#a89888;margin-top:8px;letter-spacing:2px;'>{today_str} &nbsp;·&nbsp; {weekday_zh}</div>")
    lines.append("</div>")

    # 底部装饰
    lines.append("<div style='display:flex;justify-content:center;gap:6px;margin:6px 0 18px;'>")
    lines.append("<div style='width:5px;height:5px;border-radius:50%;background:#c4a87c;'></div>")
    lines.append("<div style='width:5px;height:5px;border-radius:50%;background:#a89878;'></div>")
    lines.append("<div style='width:5px;height:5px;border-radius:50%;background:#c4a87c;'></div>")
    lines.append("</div>")

    meals = [
        ("早", "Breakfast", "breakfast", "#c9a876"),
        ("午", "Lunch", "lunch", "#a89878"),
        ("茶", "Afternoon Snack", "afternoon_snack", "#9a9080"),
        ("晚", "Dinner", "dinner", "#7a6a5a"),
    ]

    for zh_label, en_label, meal_key, color in meals:
        meal = menu_entry.get(meal_key, {})
        zh = meal.get("zh", "")
        en = meal.get("en", "")

        # 餐次区块开始
        lines.append("<div style='margin:14px 0;'>")

        # 餐次标题（彩色竖条 + 中文大字 + 英文斜体小字）
        lines.append("<div style='display:flex;align-items:baseline;gap:10px;border-left:3px solid " + color + ";padding-left:10px;margin-bottom:8px;'>")
        lines.append(f"<div style='font-family:Georgia,serif;font-size:20px;font-weight:400;color:#2c2620;letter-spacing:4px;'>{zh_label}</div>")
        lines.append(f"<div style='font-family:Georgia,serif;font-style:italic;font-size:11px;color:#a89888;letter-spacing:1px;'>{en_label}</div>")
        lines.append("</div>")

        if not zh or zh == "无需安排":
            lines.append("<div style='font-size:13px;color:#a89888;font-style:italic;padding:6px 0 6px 14px;'>无需安排 / No arrangement needed</div>")
        else:
            # 收集一餐中所有有照片的菜品，2列网格
            photo_htmls = find_meal_photos(zh, manifest, github_raw_base)
            if photo_htmls:
                lines.append(
                    "<div style='display:grid;grid-template-columns:repeat(2,1fr);gap:6px;margin:6px 0 10px;'>"
                )
                for ph in photo_htmls:
                    lines.append(ph)
                lines.append("</div>")

            # 菜品文字（小字号+宽松行距）
            if zh_first:
                lines.append(f"<div style='font-size:14px;color:#3a3530;line-height:1.75;padding-left:14px;letter-spacing:0.3px;'>{zh}</div>")
                if en:
                    lines.append(f"<div style='font-size:11px;color:#a89888;font-style:italic;line-height:1.6;padding:2px 0 0 14px;letter-spacing:0.5px;'>{en}</div>")
            else:
                if en:
                    lines.append(f"<div style='font-size:14px;color:#3a3530;line-height:1.75;padding-left:14px;'>{en}</div>")
                lines.append(f"<div style='font-size:11px;color:#a89888;font-style:italic;line-height:1.6;padding:2px 0 0 14px;'>{zh}</div>")

        lines.append("</div>")

    # 当日备注
    notes = menu_entry.get("notes", {})
    notes_zh = notes.get("zh", "")
    has_notes = notes_zh and notes_zh not in ["无", ""]
    if has_notes:
        lines.append("<div style='margin:14px 0;padding:10px 14px;background:#f4ede0;border-radius:4px;'>")
        lines.append(f"<div style='font-size:10px;letter-spacing:3px;color:#a89888;margin-bottom:4px;'>NOTES</div>")
        if zh_first:
            lines.append(f"<div style='font-size:13px;color:#5a5048;line-height:1.6;'>{notes_zh}</div>")
            if notes.get("en"):
                lines.append(f"<div style='font-size:10px;color:#a89888;font-style:italic;margin-top:3px;'>{notes['en']}</div>")
        else:
            if notes.get("en"):
                lines.append(f"<div style='font-size:13px;color:#5a5048;line-height:1.6;'>{notes['en']}</div>")
            lines.append(f"<div style='font-size:10px;color:#a89888;font-style:italic;margin-top:3px;'>{notes_zh}</div>")
        lines.append("</div>")

    # 时令建议
    if tips:
        lines.append("<div style='margin:14px 0;padding:10px 14px;background:#eef0e8;border-radius:4px;'>")
        lines.append(f"<div style='font-size:10px;letter-spacing:3px;color:#889078;margin-bottom:4px;'>SEASONAL TIPS</div>")
        for tip in tips:
            tip_zh = tip.get("zh", "")
            tip_en = tip.get("en", "")
            if zh_first:
                lines.append(f"<div style='font-size:12px;color:#5a5a48;line-height:1.7;'>· {tip_zh}</div>")
                if tip_en:
                    lines.append(f"<div style='font-size:10px;color:#9a9a88;font-style:italic;margin:1px 0 4px 14px;'>{tip_en}</div>")
            else:
                if tip_en:
                    lines.append(f"<div style='font-size:12px;color:#5a5a48;line-height:1.7;'>· {tip_en}</div>")
                lines.append(f"<div style='font-size:10px;color:#9a9a88;font-style:italic;margin:1px 0 4px 14px;'>{tip_zh}</div>")
        lines.append("</div>")

    # 页脚
    lines.append("<div style='text-align:center;padding:10px 0 0;border-top:1px solid #e8e0d4;margin-top:14px;'>")
    lines.append("<div style='font-size:10px;color:#b8a898;letter-spacing:2px;'>家庭菜单管家 · AUTO-PUSHED</div>")
    lines.append("</div>")

    lines.append("</div>")

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
            elif result.get("code") == 905:
                print(f"[FAIL] PushPlus 账户未实名认证!")
                print(f"  错误信息: {result.get('msg', '')}")
                print(f"  请访问 https://verify.pushplus.plus 完成实名认证后重试。")
                print(f"  完整返回: {result}")
                return False
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
    manifest = load_photo_manifest()

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
    print(f"照片映射: {len(manifest)} 道菜有照片")

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
    content = format_menu_message(menu_entry, day_number, tips, config, manifest)
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
