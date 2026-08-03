#!/usr/bin/env python3
"""SQLite-backed menu delivery and shared PushPlus client."""

import hashlib
import html
import json
import os
import urllib.error
import urllib.request
from datetime import datetime

from db import get_db
from runtime_config import app_env, push_enabled, validate_production_h5_url

PUSHPLUS_API = "https://www.pushplus.plus/send"
BASE_DIR = os.path.dirname(os.path.abspath(__file__))


class PushError(RuntimeError):
    pass


def push_is_enabled():
    return push_enabled()


def get_h5_base_url():
    value = os.environ.get("H5_BASE_URL", "").strip()
    if not value:
        try:
            with open(os.path.join(BASE_DIR, "config.json"), encoding="utf-8") as f:
                value = str(json.load(f).get("h5_base_url", "")).strip()
        except (OSError, ValueError, TypeError):
            value = ""
    if not value:
        if app_env() == "production":
            raise PushError("生产环境缺少 H5_BASE_URL")
        value = "http://localhost:8090"
    try:
        return validate_production_h5_url(value)
    except ValueError as exc:
        raise PushError(str(exc)) from exc


def redact_secret(value, secret):
    text = str(value)
    return text.replace(secret, "[REDACTED]") if secret else text


class PushPlusClient:
    def __init__(self, token=None, topic=None):
        self.token = token if token is not None else os.environ.get("PUSHPLUS_TOKEN", "")
        self.topic = topic if topic is not None else os.environ.get("PUSHPLUS_TOPIC", "home-menu")

    def send(self, title, content):
        if not self.token:
            raise PushError("未设置 PUSHPLUS_TOKEN")
        payload = {"token": self.token, "title": title, "content": content, "template": "markdown"}
        if self.topic:
            payload["topic"] = self.topic
        request = urllib.request.Request(
            PUSHPLUS_API,
            data=json.dumps(payload).encode("utf-8"),
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        try:
            with urllib.request.urlopen(request, timeout=30) as response:
                result = json.loads(response.read().decode("utf-8"))
        except (OSError, ValueError, urllib.error.URLError) as exc:
            raise PushError(f"PushPlus 请求失败: {redact_secret(exc, self.token)}") from exc
        if result.get("code") != 200:
            message = redact_secret(result.get("msg", "unknown error"), self.token)
            raise PushError(f"PushPlus 返回失败: {message}")
        return result


def load_menu_for_push(menu_id):
    conn = get_db()
    try:
        menu = conn.execute("SELECT * FROM menus WHERE id = ?", (menu_id,)).fetchone()
        if not menu:
            raise PushError("菜单不存在")
        items = conn.execute(
            "SELECT mi.id, mi.dish_id, mi.custom_name, mi.meal_type, mi.sort_order, "
            "d.name_cn, d.name_en, d.image "
            "FROM menu_items mi LEFT JOIN dishes d ON d.id = mi.dish_id "
            "WHERE mi.menu_id = ? ORDER BY mi.sort_order, mi.id",
            (menu_id,),
        ).fetchall()
        data = dict(menu)
        data["items"] = [dict(row) for row in items]
        return data
    finally:
        conn.close()


def menu_revision(menu):
    canonical = {
        "date": menu["date"],
        "location": menu["location"],
        "items": [
            {k: item.get(k) for k in ("dish_id", "custom_name", "meal_type", "sort_order", "name_cn", "name_en", "image")}
            for item in menu["items"]
        ],
    }
    raw = json.dumps(canonical, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def format_menu(menu, h5_base_url=None):
    base = (h5_base_url or get_h5_base_url()).rstrip("/")
    labels = {"breakfast": "早餐 Breakfast", "lunch": "午餐 Lunch", "dinner": "晚餐 Dinner"}
    sections = []
    for meal_type in ("breakfast", "lunch", "dinner"):
        items = [item for item in menu["items"] if item["meal_type"] == meal_type]
        if not items:
            continue
        rows = [f"<h3>{labels[meal_type]}</h3>"]
        for item in items:
            cn = item.get("name_cn") or item.get("custom_name") or item.get("dish_id") or ""
            en = item.get("name_en") or ""
            rows.append(f"<p><strong>{html.escape(cn)}</strong>{' / ' + html.escape(en) if en else ''}</p>")
            if item.get("image"):
                url = f"{base}/photos/{item['image']}"
                rows.append(f"<img src=\"{html.escape(url, quote=True)}\" width=\"100%\" style=\"border-radius:6px;max-width:320px\">")
        sections.append("".join(rows))
    return (
        f"<h2>家庭菜单 Family Menu</h2><p>{html.escape(menu['date'])} · {html.escape(menu['location'])}</p>"
        + "".join(sections)
    )


def push_confirmed_menu(menu_id, client=None, h5_base_url=None):
    menu = load_menu_for_push(menu_id)
    if menu["status"] != "confirmed":
        return False, f"菜单状态为 {menu['status']}，只有 confirmed 菜单可以推送"
    revision = menu.get("confirmed_revision") or menu_revision(menu)
    conn = get_db()
    try:
        conn.execute("BEGIN IMMEDIATE")
        previous = conn.execute(
            "SELECT status FROM push_logs WHERE menu_id=? AND menu_revision=? AND channel='pushplus'",
            (menu_id, revision),
        ).fetchone()
        if previous and previous["status"] == "success":
            conn.rollback()
            return True, "该版本菜单已经推送，未重复发送"
        if previous and previous["status"] == "pending":
            conn.rollback()
            return False, "该版本菜单正在推送，请勿重复触发"
        if not push_is_enabled():
            message = "实际推送已禁用：仅 APP_ENV=production 且 PUSH_ENABLED=true 时允许发送"
            conn.execute(
                "INSERT INTO push_logs(menu_id,menu_revision,date,location,channel,status,error,updated_at) "
                "VALUES(?,?,?,?, 'pushplus','disabled',?,datetime('now')) "
                "ON CONFLICT(menu_id,menu_revision,channel) DO UPDATE SET "
                "status='disabled',error=excluded.error,pushed_at=NULL,updated_at=datetime('now')",
                (menu_id, revision, menu["date"], menu["location"], message),
            )
            if not menu.get("confirmed_revision"):
                conn.execute("UPDATE menus SET confirmed_revision=? WHERE id=?", (revision, menu_id))
            conn.execute(
                "UPDATE menus SET push_status='disabled',push_error=?,pushed_at=NULL WHERE id=?",
                (message, menu_id),
            )
            conn.commit()
            return False, message
        if not menu.get("confirmed_revision"):
            conn.execute("UPDATE menus SET confirmed_revision=? WHERE id=?", (revision, menu_id))
        conn.execute(
            "INSERT INTO push_logs(menu_id,menu_revision,date,location,channel,status,error,updated_at) "
            "VALUES(?,?,?,?, 'pushplus','pending',NULL,datetime('now')) "
            "ON CONFLICT(menu_id,menu_revision,channel) DO UPDATE SET status='pending',error=NULL,updated_at=datetime('now')",
            (menu_id, revision, menu["date"], menu["location"]),
        )
        conn.execute("UPDATE menus SET push_status='pending', push_error=NULL WHERE id=?", (menu_id,))
        conn.commit()
    finally:
        conn.close()

    try:
        result = (client or PushPlusClient()).send(
            f"家庭菜单 | {menu['date']}", format_menu(menu, h5_base_url=h5_base_url)
        )
    except Exception as exc:
        error = str(exc)
        conn = get_db()
        try:
            conn.execute(
                "UPDATE push_logs SET status='failed',error=?,updated_at=datetime('now') "
                "WHERE menu_id=? AND menu_revision=? AND channel='pushplus'",
                (error, menu_id, revision),
            )
            conn.execute("UPDATE menus SET push_status='failed',push_error=? WHERE id=?", (error, menu_id))
            conn.commit()
        finally:
            conn.close()
        return False, error

    pushed_at = datetime.now().isoformat()
    conn = get_db()
    try:
        conn.execute(
            "UPDATE push_logs SET status='success',pushed_at=?,error=NULL,updated_at=datetime('now') "
            "WHERE menu_id=? AND menu_revision=? AND channel='pushplus'",
            (pushed_at, menu_id, revision),
        )
        conn.execute(
            "UPDATE menus SET pushed_at=?,push_status='success',push_error=NULL WHERE id=?",
            (pushed_at, menu_id),
        )
        conn.commit()
    finally:
        conn.close()
    return True, f"PushPlus 推送成功: {result.get('data', '')}"
