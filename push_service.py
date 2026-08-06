#!/usr/bin/env python3
"""SQLite-backed menu delivery and shared PushPlus client."""

import hashlib
import html
import json
import os
import urllib.error
import urllib.request
import time
from datetime import datetime

from db import get_db
from runtime_config import app_env, push_enabled, push_on_confirm_enabled, validate_production_h5_url

PUSHPLUS_API = "https://www.pushplus.plus/send"
BASE_DIR = os.path.dirname(os.path.abspath(__file__))


class PushError(RuntimeError):
    def __init__(self, message, response_code=None, message_id=None):
        super().__init__(message)
        self.response_code = response_code
        self.message_id = message_id


def push_is_enabled():
    return push_enabled()


def push_on_confirm_is_enabled():
    return push_on_confirm_enabled()


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
        payload = {"token": self.token, "title": title, "content": content, "template": "html"}
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
        except urllib.error.HTTPError as exc:
            try:
                failure = json.loads(exc.read().decode("utf-8"))
            except (ValueError, OSError):
                failure = {}
            message = redact_secret(failure.get("msg") or str(exc), self.token)
            raise PushError(f"PushPlus 请求失败: {message}", response_code=exc.code) from exc
        except (OSError, ValueError, urllib.error.URLError) as exc:
            raise PushError(f"PushPlus 请求失败: {redact_secret(exc, self.token)}") from exc
        if result.get("code") != 200:
            message = redact_secret(result.get("msg", "unknown error"), self.token)
            raise PushError(
                f"PushPlus 返回失败: {message}",
                response_code=result.get("code"), message_id=result.get("data"),
            )
        return result


def load_menu_for_push(menu_id):
    conn = get_db()
    try:
        menu = conn.execute("SELECT * FROM menus WHERE id = ?", (menu_id,)).fetchone()
        if not menu:
            raise PushError("菜单不存在")
        items = conn.execute(
            "SELECT mi.id, mi.dish_id, mi.custom_name, mi.meal_type, mi.sort_order, "
            "d.name_cn, d.name_en, d.image, d.is_active "
            "FROM menu_items mi LEFT JOIN dishes d ON d.id = mi.dish_id "
            "WHERE mi.menu_id = ? ORDER BY mi.sort_order, mi.id",
            (menu_id,),
        ).fetchall()
        skipped_meals = {
            row["meal_type"] for row in conn.execute(
                "SELECT meal_type FROM menu_meal_settings WHERE menu_id=? AND is_skipped=1",
                (menu_id,),
            ).fetchall()
        }
        data = dict(menu)
        data["items"] = [
            dict(row) for row in items
            if row["meal_type"] not in skipped_meals
            and (row["custom_name"] or row["is_active"] in (None, 1))
        ]
        data["skipped_meals"] = sorted(skipped_meals)
        try:
            diner_ids = json.loads(data.get("diners") or "[]")
        except (TypeError, ValueError):
            diner_ids = []
        if diner_ids:
            placeholders = ",".join("?" for _ in diner_ids)
            diner_rows = conn.execute(
                f"SELECT id,name_cn,name_en FROM diners WHERE id IN ({placeholders})", diner_ids
            ).fetchall()
            diner_map = {row["id"]: dict(row) for row in diner_rows}
            data["diner_names"] = [diner_map[diner_id] for diner_id in diner_ids if diner_id in diner_map]
        else:
            data["diner_names"] = []
        return data
    finally:
        conn.close()


def menu_revision(menu):
    canonical = {
        "date": menu["date"],
        "location": menu["location"],
        "confirmed_at": menu.get("confirmed_at"),
        "items": [
            {k: item.get(k) for k in ("dish_id", "custom_name", "meal_type", "sort_order", "name_cn", "name_en", "image")}
            for item in menu["items"]
        ],
        "skipped_meals": menu.get("skipped_meals", []),
    }
    raw = json.dumps(canonical, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def format_menu(menu, h5_base_url=None):
    base = (h5_base_url or get_h5_base_url()).rstrip("/")
    labels = {
        "breakfast": ("早餐", "Breakfast"),
        "lunch": ("午餐", "Lunch"),
        "afternoon_snack": ("下午茶", "Afternoon Tea"),
        "dinner": ("晚餐", "Dinner"),
    }
    location_labels = {
        "shenzhen": ("深圳", "Shenzhen"),
        "hongkong": ("香港", "Hong Kong"),
    }
    location_cn, location_en = location_labels.get(menu["location"], (menu["location"], menu["location"]))
    diners = menu.get("diner_names") or []
    diners_cn = "、".join(person.get("name_cn") or person["id"] for person in diners) or "未设置"
    diners_en = ", ".join(person.get("name_en") or person.get("name_cn") or person["id"] for person in diners) or "Not set"
    sections = []
    for meal_type in ("breakfast", "lunch", "afternoon_snack", "dinner"):
        items = [item for item in menu["items"] if item["meal_type"] == meal_type]
        if not items:
            continue
        label_cn, label_en = labels[meal_type]
        rows = [f"<h3 style=\"margin:22px 0 8px\">{label_cn}<br><small>{label_en}</small></h3>"]
        for item in items:
            cn = item.get("name_cn") or item.get("custom_name") or item.get("dish_id") or ""
            en = item.get("name_en") or ""
            rows.append(f"<p style=\"margin:7px 0\"><strong>{html.escape(cn)}</strong>{'<br><small>' + html.escape(en) + '</small>' if en else ''}</p>")
            if item.get("image"):
                url = f"{base}/photos/{item['image']}"
                rows.append(f"<img src=\"{html.escape(url, quote=True)}\" width=\"100%\" style=\"border-radius:6px;max-width:320px\">")
        sections.append("".join(rows))
    return (
        "<div style=\"font-family:-apple-system,BlinkMacSystemFont,'Segoe UI',sans-serif;color:#17201c\">"
        f"<h2 style=\"margin-bottom:8px\">明日家庭菜单<br><small>Tomorrow’s Family Menu</small></h2>"
        f"<p><strong>日期</strong> {html.escape(menu['date'])}<br><small>Date</small></p>"
        f"<p><strong>厨房</strong> {html.escape(location_cn)}<br><small>Kitchen · {html.escape(location_en)}</small></p>"
        f"<p><strong>用餐成员</strong> {html.escape(diners_cn)}<br><small>Diners · {html.escape(diners_en)}</small></p>"
        + "".join(sections)
        + f"<p style=\"margin-top:24px\"><a href=\"{html.escape(base + '/tomorrow', quote=True)}\">查看正式菜单 · View menu</a></p></div>"
    )


def _ensure_push_log_columns(conn):
    existing = {row["name"] for row in conn.execute("PRAGMA table_info(push_logs)")}
    additions = {
        "confirmed_at": "TEXT",
        "triggered_by": "TEXT",
        "push_requested_at": "TEXT",
        "message_id": "TEXT",
        "response_code": "INTEGER",
        "attempt_count": "INTEGER DEFAULT 0",
    }
    for column, column_type in additions.items():
        if column not in existing:
            conn.execute(f"ALTER TABLE push_logs ADD COLUMN {column} {column_type}")
    conn.commit()


def push_confirmed_menu(menu_id, client=None, h5_base_url=None, triggered_by="system", allow_retry=False):
    menu = load_menu_for_push(menu_id)
    if menu["status"] != "confirmed":
        return False, f"菜单状态为 {menu['status']}，只有 confirmed 菜单可以推送"
    current_revision = menu_revision(menu)
    revision = menu.get("confirmed_revision") or current_revision
    if menu.get("confirmed_revision") and revision != current_revision:
        return False, "已确认菜单内容发生变化，必须回退并重新确认后才能推送"
    requested_at = datetime.now().isoformat()
    conn = get_db()
    try:
        _ensure_push_log_columns(conn)
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
        if previous and previous["status"] == "failed" and not allow_retry:
            conn.rollback()
            return False, "该版本菜单推送失败，请使用重新推送"
        if not push_is_enabled():
            message = "实际推送已禁用：仅 APP_ENV=production 且 PUSH_ENABLED=true 时允许发送"
            conn.execute(
                "INSERT INTO push_logs(menu_id,menu_revision,date,location,channel,status,error,confirmed_at,"
                "triggered_by,push_requested_at,attempt_count,updated_at) "
                "VALUES(?,?,?,?, 'pushplus','disabled',?,?,?,?,0,datetime('now')) "
                "ON CONFLICT(menu_id,menu_revision,channel) DO UPDATE SET "
                "status='disabled',error=excluded.error,pushed_at=NULL,triggered_by=excluded.triggered_by,"
                "push_requested_at=excluded.push_requested_at,updated_at=datetime('now')",
                (menu_id, revision, menu["date"], menu["location"], message,
                 menu.get("confirmed_at"), triggered_by, requested_at),
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
            "INSERT INTO push_logs(menu_id,menu_revision,date,location,channel,status,error,confirmed_at,"
            "triggered_by,push_requested_at,message_id,response_code,attempt_count,updated_at) "
            "VALUES(?,?,?,?, 'pushplus','pending',NULL,?,?,?,NULL,NULL,0,datetime('now')) "
            "ON CONFLICT(menu_id,menu_revision,channel) DO UPDATE SET status='pending',error=NULL,"
            "triggered_by=excluded.triggered_by,push_requested_at=excluded.push_requested_at,"
            "message_id=NULL,response_code=NULL,attempt_count=0,updated_at=datetime('now')",
            (menu_id, revision, menu["date"], menu["location"], menu.get("confirmed_at"),
             triggered_by, requested_at),
        )
        conn.execute("UPDATE menus SET push_status='pending', push_error=NULL WHERE id=?", (menu_id,))
        conn.commit()
    finally:
        conn.close()

    location_name = {"shenzhen": "深圳", "hongkong": "香港"}.get(menu["location"], menu["location"])
    delivery_client = client or PushPlusClient()
    result = None
    error = None
    failure_code = None
    failure_message_id = None
    attempts = 0
    for attempt in range(1, 4):
        attempts = attempt
        try:
            result = delivery_client.send(
                f"明日家庭菜单｜{location_name}", format_menu(menu, h5_base_url=h5_base_url)
            )
            error = None
            break
        except Exception as exc:
            error = str(exc)
            failure_code = getattr(exc, "response_code", None)
            failure_message_id = getattr(exc, "message_id", None)
            if attempt < 3 and client is None:
                time.sleep(attempt)

    if error is not None:
        conn = get_db()
        try:
            conn.execute(
                "UPDATE push_logs SET status='failed',error=?,attempt_count=?,response_code=?,"
                "message_id=?,updated_at=datetime('now') "
                "WHERE menu_id=? AND menu_revision=? AND channel='pushplus'",
                (error, attempts, failure_code,
                 str(failure_message_id) if failure_message_id is not None else None,
                 menu_id, revision),
            )
            conn.execute("UPDATE menus SET push_status='failed',push_error=? WHERE id=?", (error, menu_id))
            conn.commit()
        finally:
            conn.close()
        return False, error

    pushed_at = datetime.now().isoformat()
    response_code = result.get("code") if isinstance(result, dict) else None
    message_id = result.get("data") if isinstance(result, dict) else None
    if isinstance(message_id, (dict, list)):
        message_id = json.dumps(message_id, ensure_ascii=False)
    elif message_id is not None:
        message_id = str(message_id)
    conn = get_db()
    try:
        conn.execute(
            "UPDATE push_logs SET status='success',pushed_at=?,error=NULL,message_id=?,response_code=?,"
            "attempt_count=?,updated_at=datetime('now') "
            "WHERE menu_id=? AND menu_revision=? AND channel='pushplus'",
            (pushed_at, message_id, response_code, attempts, menu_id, revision),
        )
        conn.execute(
            "UPDATE menus SET pushed_at=?,push_status='success',push_error=NULL WHERE id=?",
            (pushed_at, menu_id),
        )
        conn.commit()
    finally:
        conn.close()
    return True, f"PushPlus 推送成功: {message_id or ''}"
