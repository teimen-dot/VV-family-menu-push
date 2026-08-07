#!/usr/bin/env python3
"""
家庭菜单管家 - H5 核心应用 (v2.0 重构版)
4个页面：Tomorrow(明日菜单) / Pantry(家中食材) / Dishes(菜品库) / History(历史菜单)
全站双语同屏，location 切换，交互式菜单管理，微信内嵌浏览器优先。
"""

import json
import os
import base64
import hashlib
import hmac
import secrets
import threading
import time
from html import escape
from datetime import date, datetime, timedelta
from http.server import ThreadingHTTPServer, BaseHTTPRequestHandler
from http.cookies import SimpleCookie
from urllib.parse import urlparse, parse_qs, quote
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
    get_inventory_version, _record_pantry_usage,
)
from menu_service import (
    get_menu_with_dishes, add_dish_to_menu, remove_dish_from_menu,
    replace_dish_in_menu, lock_dish, ai_fill_menu, repair_menu,
    confirm_menu, generate_and_store_menu, ensure_tomorrow_menu,
    get_tomorrow_date, revert_to_draft, push_menu,
)
from photo_security import PhotoValidationError, resolve_photo_path
from runtime_config import photo_dir, server_host, validate_app_startup
from ingredient_service import add_or_get_ingredient, update_ingredient_names

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
PORT = int(os.environ.get("PORT", "8090"))
HOST = server_host()
PHOTOS_DIR = photo_dir(BASE_DIR)
PWA_DIR = os.path.join(BASE_DIR, "pwa", "family")
PUBLIC_DIR = os.path.join(BASE_DIR, "public")
LOCATIONS = {"shenzhen": "深圳 Shenzhen", "hongkong": "香港 Hong Kong"}
SESSION_COOKIE_NAME = "__Host-family_session"
SESSION_TTL_SECONDS = 30 * 24 * 60 * 60
LOGIN_WINDOW_SECONDS = 5 * 60
LOGIN_MAX_FAILURES = 8
_LOGIN_FAILURES = {}
_AUTH_LOCK = threading.Lock()

OWNER_ONLY_POST_PATHS = {
    "/api/ingredients/add",
    "/api/ingredients/update",
    "/api/tomorrow/add",
    "/api/tomorrow/remove",
    "/api/tomorrow/replace",
    "/api/tomorrow/smart-replace",
    "/api/tomorrow/ai-fill",
    "/api/tomorrow/repair",
    "/api/tomorrow/confirm",
    "/api/tomorrow/revert",
    "/api/tomorrow/push",
    "/api/tomorrow/diners",
    "/api/tomorrow/meal-mode",
    "/api/meal-plan/meal-diners",
    "/api/meal-plan/note",
    "/api/meal-plan/meal-state",
    "/api/meal-plan/clear-meal",
    "/api/purchase/update",
}
PANTRY_POST_PATHS = {
    "/api/pantry/submit",
    "/api/pantry/add",
    "/api/pantry/same-as-last",
    "/api/pantry/update_status",
    "/api/pantry/remove",
    "/api/pantry/consume",
    "/api/pantry/add-by-name",
}
MENU_DRAFT_WRITE_PATHS = {
    "/api/tomorrow/add", "/api/tomorrow/remove", "/api/tomorrow/replace",
    "/api/tomorrow/smart-replace",
    "/api/tomorrow/ai-fill", "/api/tomorrow/repair", "/api/tomorrow/diners",
    "/api/tomorrow/meal-mode",
    "/api/meal-plan/meal-diners", "/api/meal-plan/note",
    "/api/meal-plan/meal-state", "/api/meal-plan/clear-meal",
}


def authenticated_role(username):
    """Map a verified application username to its role."""
    owner = os.environ.get("OWNER_AUTH_USERNAME", "").strip()
    worker = os.environ.get("WORKER_AUTH_USERNAME", "").strip()
    if username and owner and username == owner:
        return "owner"
    if username and worker and username == worker:
        return "worker"
    return "unknown"


def _apr1_to64(value, length):
    alphabet = "./0123456789ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz"
    encoded = []
    while length:
        encoded.append(alphabet[value & 0x3F])
        value >>= 6
        length -= 1
    return "".join(encoded)


def _apr1_hash(password, salt):
    """Return an Apache APR1-MD5 password hash for an existing salt."""
    password_bytes = password.encode("utf-8")
    salt = salt.split("$", 1)[0][:8]
    salt_bytes = salt.encode("ascii")
    digest = hashlib.md5(password_bytes + b"$apr1$" + salt_bytes)
    alternate = hashlib.md5(password_bytes + salt_bytes + password_bytes).digest()
    remaining = len(password_bytes)
    while remaining:
        block_size = min(16, remaining)
        digest.update(alternate[:block_size])
        remaining -= block_size
    length = len(password_bytes)
    while length:
        digest.update(b"\x00" if length & 1 else password_bytes[:1])
        length >>= 1
    final = digest.digest()
    for index in range(1000):
        round_digest = hashlib.md5()
        round_digest.update(password_bytes if index & 1 else final)
        if index % 3:
            round_digest.update(salt_bytes)
        if index % 7:
            round_digest.update(password_bytes)
        round_digest.update(final if index & 1 else password_bytes)
        final = round_digest.digest()
    encoded = "".join((
        _apr1_to64((final[0] << 16) | (final[6] << 8) | final[12], 4),
        _apr1_to64((final[1] << 16) | (final[7] << 8) | final[13], 4),
        _apr1_to64((final[2] << 16) | (final[8] << 8) | final[14], 4),
        _apr1_to64((final[3] << 16) | (final[9] << 8) | final[15], 4),
        _apr1_to64((final[4] << 16) | (final[10] << 8) | final[5], 4),
        _apr1_to64(final[11], 2),
    ))
    return f"$apr1${salt}${encoded}"


def verify_family_password(username, password):
    if not username or not password or len(password) > 1024:
        return False
    password_file = os.environ.get("FAMILY_AUTH_HTPASSWD_PATH", "").strip()
    if not password_file:
        return False
    try:
        with open(password_file, encoding="utf-8") as handle:
            for line in handle:
                stored_user, separator, stored_hash = line.rstrip("\n").partition(":")
                if stored_user != username or not separator:
                    continue
                if not stored_hash.startswith("$apr1$"):
                    return False
                salt = stored_hash.split("$", 3)[2]
                return hmac.compare_digest(_apr1_hash(password, salt), stored_hash)
    except OSError:
        return False
    return False


def _session_secret():
    secret = os.environ.get("SESSION_SECRET", "").strip()
    if not secret:
        if os.environ.get("APP_ENV", "development").strip().lower() == "production":
            raise RuntimeError("production requires SESSION_SECRET")
        secret = "development-only-session-secret"
    return secret.encode("utf-8")


def _base64url_encode(value):
    return base64.urlsafe_b64encode(value).decode("ascii").rstrip("=")


def _base64url_decode(value):
    return base64.urlsafe_b64decode(value + "=" * (-len(value) % 4))


def create_session(username, role):
    """Create a signed, restart-safe session token without server-side storage."""
    payload = json.dumps({
        "username": username,
        "role": role,
        "expires_at": int(time.time()) + SESSION_TTL_SECONDS,
        "nonce": secrets.token_urlsafe(8),
    }, separators=(",", ":"), sort_keys=True).encode("utf-8")
    encoded = _base64url_encode(payload)
    signature = hmac.new(_session_secret(), encoded.encode("ascii"), hashlib.sha256).digest()
    return f"{encoded}.{_base64url_encode(signature)}"


def session_from_cookie(cookie_header):
    if not cookie_header:
        return None, None
    cookie = SimpleCookie()
    try:
        cookie.load(cookie_header)
    except Exception:
        return None, None
    morsel = cookie.get(SESSION_COOKIE_NAME)
    if not morsel:
        return None, None
    token = morsel.value
    try:
        encoded, supplied_signature = token.split(".", 1)
        expected_signature = hmac.new(
            _session_secret(), encoded.encode("ascii"), hashlib.sha256
        ).digest()
        if not hmac.compare_digest(_base64url_decode(supplied_signature), expected_signature):
            return None, None
        session = json.loads(_base64url_decode(encoded).decode("utf-8"))
    except (ValueError, TypeError, json.JSONDecodeError, UnicodeDecodeError):
        return None, None
    username = str(session.get("username", ""))
    role = str(session.get("role", ""))
    if session.get("expires_at", 0) <= int(time.time()):
        return None, None
    if authenticated_role(username) != role or role not in ("owner", "worker"):
        return None, None
    refreshed = create_session(username, role)
    return refreshed, {"username": username, "role": role, "expires_at": session["expires_at"]}


def destroy_session(session_id):
    # Sessions are stateless; logout clears the browser cookie.
    return None


def login_is_rate_limited(client_key):
    now = time.time()
    with _AUTH_LOCK:
        failures = [stamp for stamp in _LOGIN_FAILURES.get(client_key, []) if now - stamp < LOGIN_WINDOW_SECONDS]
        _LOGIN_FAILURES[client_key] = failures
        return len(failures) >= LOGIN_MAX_FAILURES


def record_login_failure(client_key):
    now = time.time()
    with _AUTH_LOCK:
        failures = [stamp for stamp in _LOGIN_FAILURES.get(client_key, []) if now - stamp < LOGIN_WINDOW_SECONDS]
        failures.append(now)
        _LOGIN_FAILURES[client_key] = failures


def clear_login_failures(client_key):
    with _AUTH_LOCK:
        _LOGIN_FAILURES.pop(client_key, None)


def post_path_allowed(role, path):
    if role not in ("owner", "worker"):
        return False
    if path in PANTRY_POST_PATHS or path in {"/api/dishes/availability", "/api/dishes/recommend"}:
        return True
    return role == "owner"


def ensure_owner_tomorrow_draft(role, menu_id, menu_row):
    """Return a confirmed tomorrow menu to draft on the owner's first edit."""
    if menu_row["status"] == "draft":
        return True, ""
    if (
        role == "owner"
        and menu_row["status"] == "confirmed"
        and menu_row["date"] == get_tomorrow_date()
    ):
        return revert_to_draft(menu_id)
    return False, "已确认菜单不可直接修改，请先回退到草稿"


def health_result():
    try:
        conn = get_db()
        try:
            result = conn.execute("PRAGMA quick_check").fetchone()[0]
        finally:
            conn.close()
        if result != "ok":
            raise RuntimeError("database check failed")
        return 200, {"status": "ok", "database": "ok"}
    except Exception:
        return 503, {"status": "error", "database": "error"}


# ============================================================
# 数据查询
# ============================================================

def _family_photo_url(filename):
    if not filename:
        return ""
    base = os.environ.get("H5_BASE_URL", "").strip().rstrip("/")
    path = f"/photos/{quote(filename)}"
    return f"{base}{path}" if base else path


def get_all_dishes(category=None, search="", location=None):
    """Return normalized active dishes for the Family API."""
    conn = get_db()
    try:
        query = (
            "SELECT d.*,c.label_cn AS category_label_cn,c.label_en AS category_label_en "
            "FROM dishes d LEFT JOIN categories c ON c.id=d.category_id "
            "WHERE (d.is_active = 1 OR d.is_active IS NULL)"
        )
        params = []
        if category:
            query += " AND d.category_id = ?"
            params.append(category)
        if search:
            query += " AND (d.name_cn LIKE ? OR d.name_en LIKE ?)"
            params.extend([f"%{search}%", f"%{search}%"])
        query += " ORDER BY d.category_id, d.name_cn"
        if search:
            query += " LIMIT 20"
        rows = conn.execute(query, params).fetchall()
        dish_ids = [row["id"] for row in rows]
        required_map = {dish_id: [] for dish_id in dish_ids}
        if dish_ids:
            placeholders = ",".join("?" for _ in dish_ids)
            ingredients = conn.execute(
                "SELECT di.dish_id,i.ingredient_id,i.name_cn,i.name_en "
                "FROM dish_ingredients di LEFT JOIN ingredients i "
                "ON i.ingredient_id=di.ingredient_id "
                f"WHERE di.required=1 AND di.dish_id IN ({placeholders}) ORDER BY di.id",
                dish_ids,
            ).fetchall()
            for ingredient in ingredients:
                required_map[ingredient["dish_id"]].append({
                    "ingredient_id": ingredient["ingredient_id"],
                    "name_cn": ingredient["name_cn"] or "",
                    "name_en": ingredient["name_en"] or "",
                })
        availability = check_dishes_availability_batch(dish_ids, location) if location else {}
        result = []
        json_fields = {
            "protein_types", "vegetables", "meal_tags", "cooking_methods", "custom_tags",
            "meal_roles", "meal_components",
        }
        for row in rows:
            dish = dict(row)
            for field in json_fields:
                try:
                    dish[field] = json.loads(dish.get(field) or "[]")
                except (json.JSONDecodeError, TypeError):
                    dish[field] = []
            dish["required_ingredients"] = required_map[dish["id"]]
            dish["image_url"] = _family_photo_url(dish.get("image"))
            missing_fields = []
            if not dish.get("category_id"):
                missing_fields.append("category")
            if not dish.get("meal_tags"):
                missing_fields.append("meal_tags")
            if not dish["required_ingredients"]:
                missing_fields.append("required_ingredients")
            if not dish.get("image"):
                missing_fields.append("image")
            dish["missing_fields"] = missing_fields
            if location:
                dish["availability"] = availability.get(dish["id"], {})
            result.append(dish)
        return result
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
            # missing 不进入推荐（V12: incomplete 已废弃，无必选食材 = available）

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
            "SELECT i.ingredient_id, i.name_cn, i.name_en,di.required "
            "FROM dish_ingredients di JOIN ingredients i ON di.ingredient_id = i.ingredient_id "
            "WHERE di.dish_id = ?", (dish_id,)
        ).fetchall()
        d["ingredients"] = [dict(r) for r in ings]
        d["required_ingredients"] = [dict(r) for r in ings if r["required"]]
        d["image_url"] = _family_photo_url(d.get("image"))
        missing_fields = []
        if not d.get("category_id"):
            missing_fields.append("category")
        if not d.get("meal_tags"):
            missing_fields.append("meal_tags")
        if not d["required_ingredients"]:
            missing_fields.append("required_ingredients")
        if not d.get("image"):
            missing_fields.append("image")
        d["missing_fields"] = missing_fields
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


MEAL_TYPES = ("breakfast", "lunch", "afternoon_snack", "dinner")


def ensure_menu_shell(date_str, location):
    """Create an empty dated menu without generating dishes."""
    conn = get_db()
    try:
        row = conn.execute("SELECT id FROM menus WHERE date=?", (date_str,)).fetchone()
        if row:
            return row["id"]
        cursor = conn.execute(
            "INSERT INTO menus(date,location,status,diners) VALUES(?,?,'draft','[]')",
            (date_str, location),
        )
        conn.commit()
        return cursor.lastrowid
    finally:
        conn.close()


def get_meal_settings(menu_id):
    conn = get_db()
    try:
        if not conn.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name='menu_meal_settings'"
        ).fetchone():
            return {}
        rows = conn.execute(
            "SELECT meal_type,diners,note,is_skipped FROM menu_meal_settings WHERE menu_id=?",
            (menu_id,),
        ).fetchall()
        result = {}
        for row in rows:
            custom_diners = None
            if row["diners"] is not None:
                try:
                    custom_diners = json.loads(row["diners"])
                except (TypeError, ValueError):
                    custom_diners = None
            result[row["meal_type"]] = {
                "diners": custom_diners,
                "note": row["note"] or "",
                "is_skipped": bool(row["is_skipped"]),
            }
        return result
    finally:
        conn.close()


def update_meal_setting(menu_id, meal_type, *, diners_marker=False, diners=None,
                        note_marker=False, note="", skipped_marker=False, skipped=False):
    if meal_type not in MEAL_TYPES:
        return False, "invalid meal_type"
    conn = get_db()
    try:
        conn.execute(
            "INSERT OR IGNORE INTO menu_meal_settings(menu_id,meal_type) VALUES(?,?)",
            (menu_id, meal_type),
        )
        if diners_marker:
            value = None if diners is None else json.dumps(diners, ensure_ascii=False)
            conn.execute(
                "UPDATE menu_meal_settings SET diners=?,updated_at=datetime('now') WHERE menu_id=? AND meal_type=?",
                (value, menu_id, meal_type),
            )
        if note_marker:
            conn.execute(
                "UPDATE menu_meal_settings SET note=?,updated_at=datetime('now') WHERE menu_id=? AND meal_type=?",
                (note.strip()[:500], menu_id, meal_type),
            )
        if skipped_marker:
            conn.execute(
                "UPDATE menu_meal_settings SET is_skipped=?,updated_at=datetime('now') WHERE menu_id=? AND meal_type=?",
                (1 if skipped else 0, menu_id, meal_type),
            )
        conn.commit()
        log_event("meal_setting_updated", "menu", str(menu_id), {
            "meal_type": meal_type, "diners_custom": diners is not None if diners_marker else None,
            "note_updated": note_marker, "is_skipped": skipped if skipped_marker else None,
        })
        return True, "ok"
    finally:
        conn.close()


def clear_menu_meal(menu_id, meal_type):
    if meal_type not in MEAL_TYPES:
        return False, "invalid meal_type"
    conn = get_db()
    try:
        count = conn.execute(
            "SELECT COUNT(*) FROM menu_items WHERE menu_id=? AND meal_type=?",
            (menu_id, meal_type),
        ).fetchone()[0]
        conn.execute("DELETE FROM menu_items WHERE menu_id=? AND meal_type=?", (menu_id, meal_type))
        conn.commit()
        log_event("meal_cleared", "menu", str(menu_id), {"meal_type": meal_type, "count": count})
        return True, count
    finally:
        conn.close()


def get_menu_meal_mode(menu_id):
    """V11: 获取菜单的 meal_mode 和 banquet_total_diners"""
    conn = get_db()
    try:
        row = conn.execute(
            "SELECT meal_mode, banquet_total_diners FROM menus WHERE id = ?",
            (menu_id,)
        ).fetchone()
        if not row:
            return {"meal_mode": "daily", "banquet_total_diners": None}
        return {
            "meal_mode": row["meal_mode"] or "daily",
            "banquet_total_diners": row["banquet_total_diners"],
        }
    finally:
        conn.close()


def update_menu_meal_mode(menu_id, meal_mode, banquet_total_diners=None):
    """V11: 更新菜单的 meal_mode 和 banquet_total_diners"""
    conn = get_db()
    try:
        conn.execute(
            "UPDATE menus SET meal_mode = ?, banquet_total_diners = ?, "
            "updated_at = datetime('now') WHERE id = ?",
            (meal_mode, banquet_total_diners if meal_mode == "banquet" else None, menu_id)
        )
        conn.commit()
        log_event("meal_mode_updated", "menu", str(menu_id), {
            "meal_mode": meal_mode,
            "banquet_total_diners": banquet_total_diners,
        })
        return True
    finally:
        conn.close()


SLOT_LABELS = {
    "protein_main": ("蛋白质", "Protein"),
    "vegetable_dish": ("蔬菜", "Vegetable"),
    "vegetable": ("蔬菜", "Vegetable"),
    "staple": ("主食", "Staple"),
    "slow_soup": ("汤羹", "Soup"),
    "quick_soup": ("快手汤", "Quick Soup"),
    "egg": ("鸡蛋", "Egg"),
    "tofu": ("豆制品", "Soy product"),
    "porridge": ("粥", "Porridge"),
    "companion_staple": ("搭配主食", "Side Staple"),
    "coarse_grain": ("粗粮", "Coarse Grain"),
}


def validate_menu_meals(menu, diners_count, meal_diners_counts=None, skipped_meals=None):
    """Return the one shared rule-engine result used by meal hints and overview."""
    from rule_engine import RuleEngine, NutritionAnalyzer, MealState, analyze_meal_slots
    from menu_service import _load_pool

    dish_map = {dish["id"]: dish for dish in _load_pool()["dishes"]}
    day_result = {}
    meal_slots = {}
    skipped_meals = set(skipped_meals or ())
    meal_diners_counts = meal_diners_counts or {}
    for meal_type in ("breakfast", "lunch", "dinner"):
        state = MealState()
        for item in ([] if meal_type in skipped_meals else menu.get("meals", {}).get(meal_type, [])):
            dish = dish_map.get(item.get("dish_id", ""))
            if dish:
                state.add_dish(
                    NutritionAnalyzer.analyze(dish),
                    is_locked=item.get("is_locked", False),
                )
        day_result[meal_type] = {"state": state}
        if meal_type in skipped_meals:
            meal_slots[meal_type] = {}
        else:
            meal_slots[meal_type] = analyze_meal_slots(
                meal_type, state, meal_diners_counts.get(meal_type, diners_count)
            )
    review = RuleEngine.final_review(day_result, diners_count)
    missing_by_meal = {
        meal_type: {
            slot: values for slot, values in slots.items()
            if values.get("missing_min", 0) > 0
        }
        for meal_type, slots in meal_slots.items()
    }
    return {
        "meal_slots": meal_slots,
        "missing_by_meal": missing_by_meal,
        "warnings": review.get("warnings", []),
    }


def validate_menu_after_mutation(menu_id):
    """Re-read the stored menu and run the same validator used by Tomorrow."""
    conn = get_db()
    try:
        row = conn.execute("SELECT date FROM menus WHERE id=?", (menu_id,)).fetchone()
    finally:
        conn.close()
    if not row:
        return None
    menu = get_menu_with_dishes(row["date"])
    mode = get_menu_meal_mode(menu_id)
    diners = get_menu_diners(menu_id)
    diner_count = mode["banquet_total_diners"] or 8 if mode["meal_mode"] == "banquet" else max(len(diners), 1)
    return validate_menu_meals(menu, diner_count)


def smart_replace_menu_item(menu_id, menu_item_id, location):
    """Pick the next in-stock peer within the dish's real subtype."""
    conn = get_db()
    try:
        current = conn.execute(
            "SELECT mi.dish_id,mi.meal_type,d.category_id,d.carb_type,d.protein_types "
            "FROM menu_items mi "
            "JOIN dishes d ON d.id=mi.dish_id WHERE mi.id=? AND mi.menu_id=?",
            (menu_item_id, menu_id),
        ).fetchone()
        if not current:
            return False, "菜品不存在", None
        used_ids = {
            row["dish_id"] for row in conn.execute(
                "SELECT dish_id FROM menu_items WHERE menu_id=? AND meal_type=?",
                (menu_id, current["meal_type"]),
            )
        }
        used_ids.discard(current["dish_id"])
        rows = conn.execute(
            "SELECT id,meal_tags,carb_type,protein_types FROM dishes "
            "WHERE is_active=1 AND category_id=? "
            "ORDER BY updated_at DESC,id",
            (current["category_id"],),
        ).fetchall()

        def primary_protein(value):
            try:
                proteins = json.loads(value or "[]")
            except (json.JSONDecodeError, TypeError):
                proteins = []
            return proteins[0] if proteins else None

        current_primary = primary_protein(current["protein_types"])
        peers = []
        for row in rows:
            try:
                meal_tags = json.loads(row["meal_tags"] or "[]")
            except (json.JSONDecodeError, TypeError):
                meal_tags = []
            if current["meal_type"] not in meal_tags or row["id"] in used_ids:
                continue
            if (current["category_id"] == "staple_carb"
                    and row["carb_type"] != current["carb_type"]):
                continue
            if (current["category_id"] == "egg_tofu"
                    and primary_protein(row["protein_types"]) != current_primary):
                continue
            peers.append(row)

        peer_ids = [row["id"] for row in peers]
        primary_by_id = {
            row["id"]: primary_protein(row["protein_types"])
            for row in peers
        }
        if current["dish_id"] in peer_ids:
            current_index = peer_ids.index(current["dish_id"])
            candidates = peer_ids[current_index + 1:] + peer_ids[:current_index]
        else:
            candidates = peer_ids
        candidates = [dish_id for dish_id in candidates if dish_id != current["dish_id"]]
    finally:
        conn.close()
    availability = check_dishes_availability_batch(candidates, location)
    available_candidates = [
        dish_id for dish_id in candidates
        if availability.get(dish_id, {}).get("status") == "available"
    ]
    if current["category_id"] == "protein_main" and current_primary:
        same_protein_available = [
            dish_id for dish_id in available_candidates
            if primary_by_id.get(dish_id) == current_primary
        ]
        if same_protein_available:
            available_candidates = same_protein_available
    replacement_id = available_candidates[0] if available_candidates else None
    if not replacement_id:
        return False, "没有同餐次、同类别且库存可做的其他菜", None
    ok, message = replace_dish_in_menu(menu_id, menu_item_id, replacement_id)
    if ok:
        conn = get_db()
        try:
            conn.execute(
                "INSERT INTO menu_item_replace_history(menu_id,menu_item_id,dish_id) VALUES(?,?,?)",
                (menu_id, menu_item_id, replacement_id),
            )
            conn.execute(
                "DELETE FROM menu_item_replace_history WHERE menu_id=? AND menu_item_id=? "
                "AND id NOT IN (SELECT id FROM menu_item_replace_history "
                "WHERE menu_id=? AND menu_item_id=? ORDER BY id DESC LIMIT 32)",
                (menu_id, menu_item_id, menu_id, menu_item_id),
            )
            conn.commit()
        finally:
            conn.close()
    return ok, message, replacement_id if ok else None


def independent_missing_issue_keys(missing_by_meal):
    return {
        f"{meal_type}:{slot}"
        for meal_type, missing in missing_by_meal.items()
        for slot in missing
    }


def get_all_ingredients():
    """获取所有食材（含 aliases 和 ingredient_group）"""
    conn = get_db()
    try:
        rows = conn.execute(
            "SELECT ingredient_id, name_cn, name_en, aliases, category, ingredient_group, "
            "translation_pending FROM ingredients ORDER BY ingredient_group, name_cn"
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
    """从 SQLite 建立中文菜名→英文名映射（包含已下架历史菜）。"""
    conn = get_db()
    try:
        return {row["name_cn"]: row["name_en"] or "" for row in conn.execute(
            "SELECT name_cn, name_en FROM dishes WHERE name_cn IS NOT NULL"
        )}
    finally:
        conn.close()


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
            "missing_names_en": [m.get("name_en", "") for m in avail["missing_required"]],
            "matched_names": [m["name_cn"] for m in avail["available_required"]],
            "total_ingredients": len(avail["required"]),
            "status": avail["status"],
            "data_complete": avail.get("data_complete", False),
            "missing_fields": avail.get("missing_fields", []),
        }
    return result


def get_common_ingredients(location):
    """当前厨房按真实库存使用频率生成的常用食材。"""
    return get_common_ingredients_static(location)


# ============================================================
# HTML/CSS/JS 共享模板
# ============================================================

CSS = """
*{margin:0;padding:0;box-sizing:border-box}
body{font-family:-apple-system,BlinkMacSystemFont,"Segoe UI","PingFang SC","Hiragino Sans GB",sans-serif;background:#faf7f2;color:#2c2620;line-height:1.6;font-size:16px}
.header{background:#2c2620;color:#faf7f2;padding:14px 16px;display:flex;align-items:center;justify-content:space-between;position:sticky;top:0;z-index:100}
.legacy-brand{display:flex;align-items:center;gap:10px;min-width:0}.legacy-brand-logo{width:36px;height:36px;display:block;flex:0 0 36px;object-fit:contain}
.header h1{font-size:22px;font-weight:700;letter-spacing:.5px;line-height:1.12}
.header h1 span{display:block;font-size:12px;font-weight:500;letter-spacing:.02em;opacity:.72;margin-top:3px}
.loc-switch{display:flex;gap:4px}
.loc-btn{font-size:14px;padding:5px 12px;border-radius:14px;border:1px solid #a89888;background:transparent;color:#a89888;cursor:pointer}
.loc-btn.active{background:#a89888;color:#2c2620;border-color:#a89888}
.role-badge{font-size:13px;background:#a89888;padding:3px 10px;border-radius:10px}
.nav{display:flex;background:#fff;border-bottom:1px solid #e8e0d4;position:sticky;top:62px;z-index:99}
.nav a{position:relative;flex:1;text-align:center;padding:10px 2px 12px;font-size:17px;line-height:1.15;color:#a89888;text-decoration:none;font-weight:600}
.nav a.active{color:#2c2620}
.nav a.active:after{content:"";position:absolute;left:50%;bottom:0;width:34px;height:3px;border-radius:3px 3px 0 0;background:#287356;transform:translateX(-50%)}
.nav a span{display:block;font-size:13px;margin-top:1px;opacity:.6}
.content{max-width:600px;margin:0 auto;padding:12px}
.tomorrow-heading{display:flex;align-items:flex-start;justify-content:space-between;gap:16px;padding:16px 4px 14px}
.tomorrow-date{font-size:15px;font-weight:650;color:#5a4a3a;line-height:1.35}
.tomorrow-date span{display:block;color:#a89888;font-size:13px;font-weight:500;margin-top:2px}
.tomorrow-heading h2{font-size:30px;line-height:1.2;margin-top:10px}
.tomorrow-heading p{font-size:14px;color:#6f6255;margin-top:6px;max-width:42ch}
.nutrition-overview{background:#eef6f1;border:1px solid #c7ded1;border-radius:12px;padding:14px;margin-bottom:10px}
.nutrition-overview-head{display:flex;align-items:flex-start;justify-content:space-between;gap:12px;margin-bottom:12px}
.nutrition-overview-head h3{font-size:18px;line-height:1.25}
.nutrition-overview-head h3 span{display:block;font-size:12px;color:#56806b;font-weight:600;margin-bottom:3px}
.nutrition-overview-head>span{font-size:12px;color:#356b52;background:#fff;border:1px solid #c7ded1;border-radius:999px;padding:3px 9px;white-space:nowrap}
.nutrition-metric{display:grid;grid-template-columns:58px minmax(0,1fr) 46px;align-items:center;gap:10px;margin-top:9px;font-size:13px}
.nutrition-metric strong{text-align:right;font-size:12px;color:#356b52}
.nutrition-track{height:8px;background:#d9e7df;border-radius:999px;overflow:hidden}
.nutrition-track i{display:block;width:var(--value);height:100%;background:#287356;border-radius:inherit}
.nutrition-note{font-size:12px;color:#56806b;margin-top:11px}
.meal-section{background:#fff;border-radius:12px;margin-bottom:10px;overflow:hidden;box-shadow:0 1px 3px rgba(0,0,0,.05)}
.meal-header{padding:12px 14px;display:flex;align-items:center;gap:8px}
.meal-bar{width:4px;height:24px;border-radius:2px}
.meal-title{font-size:22px;font-weight:700}
.meal-title-en{font-size:15px;color:#a89888;font-style:italic;margin-left:4px}
.meal-actions{margin-left:auto;display:flex;gap:6px}
.meal-act-btn{font-size:15px;padding:8px 14px;border-radius:8px;border:1px solid #d4c9b8;background:#faf7f2;color:#5a4a3a;cursor:pointer;white-space:nowrap;min-height:44px;display:flex;align-items:center}
.meal-act-btn:active{background:#e8e0d4}
.meal-items{padding:0 14px 8px}
.slot-hint{padding:9px 12px;font-size:14px;color:#856404;background:#fff7d6;border:1px solid #f0d98a;border-radius:8px;margin:0 14px 6px}
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
.diners-section{background:#fff;border:1px solid #e5e8e3;border-radius:14px;padding:14px;margin-bottom:10px}
.diners-section .diners-title{display:flex;align-items:flex-end;justify-content:space-between;font-size:18px;font-weight:700;color:#202724;line-height:1.15;margin-bottom:12px}
.diners-section .diners-title>span>small{display:block;font-size:12px;color:#6b746f;font-weight:600;margin-top:3px}
.diner-count{font-size:14px;color:#66706b;text-align:right;line-height:1.25}
.diner-count small{display:block;font-size:12px}
.diners-row.people-cards{display:grid;grid-template-columns:repeat(5,minmax(0,1fr));gap:7px}
.diner-chip{display:inline-flex;align-items:center;gap:3px;padding:8px 14px;border-radius:14px;border:1px solid #ddd5c8;font-size:16px;cursor:pointer;transition:transform .15s,background .15s,border-color .15s;min-height:44px}
.diner-chip.person-card{min-width:0;min-height:102px;padding:10px 4px;flex-direction:column;justify-content:center;border-radius:12px;background:#fff;color:#202724;text-align:center}
.diner-chip.person-card.active{background:#e9f3ed;color:#202724;border-color:#8dbca3}
.diner-avatar{display:flex;align-items:center;justify-content:center;width:42px;height:42px;border-radius:50%;background:#eef0ee;color:#59635e;font-size:15px;font-weight:700;margin-bottom:4px}
.person-card.active .diner-avatar{background:#287356;color:#fff}
.diner-name{display:block;max-width:100%;font-weight:700;line-height:1.15;overflow:hidden;text-overflow:ellipsis}
.diner-en{display:block;font-size:12px;line-height:1.2;color:#68716d;margin-top:3px}
.tomorrow-actions{position:sticky;bottom:0;z-index:20;display:grid;grid-template-columns:1fr 1.25fr;gap:8px;background:rgba(250,247,242,.94);backdrop-filter:blur(12px);padding:10px 0 calc(10px + env(safe-area-inset-bottom));margin-top:8px}
.tomorrow-actions .btn{display:flex;flex-direction:column;align-items:center;justify-content:center;line-height:1.2}
.tomorrow-actions .btn small{font-size:11px;font-weight:500;opacity:.72;margin-top:3px}
.desktop-confirm{display:none;grid-template-columns:1fr 1.15fr;gap:8px;margin-bottom:10px;padding:12px;border:1px solid #c7ded1;border-radius:12px;background:#eef6f1}
.desktop-confirm .btn{display:flex;flex-direction:column;align-items:center;justify-content:center;line-height:1.2}
.desktop-confirm .btn small{font-size:11px;font-weight:500;opacity:.72;margin-top:3px}
.meal-items:empty:after{content:"暂无菜品 No dishes";display:block;padding:18px 0;color:#a89888;text-align:center;font-size:14px}
.btn-stepper{width:40px;height:40px;border-radius:8px;border:1px solid #ddd5c8;background:#fff;font-size:22px;cursor:pointer;display:flex;align-items:center;justify-content:center;min-height:44px}
.btn-stepper:active{background:#f5f0e8}
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
@media (max-width:767px){
html,body{max-width:100%;overflow-x:hidden}
body{padding-top:env(safe-area-inset-top);padding-bottom:env(safe-area-inset-bottom)}
.legacy-brand-logo{width:32px;height:32px;flex-basis:32px}
.content,.card,.pantry-item,.pantry-item>*{min-width:0;max-width:100%}
.pantry-item{display:grid;grid-template-columns:minmax(0,1fr);align-items:stretch;gap:10px}
.pantry-item>.pantry-name{width:100%;min-width:0}
.pantry-item .name{white-space:normal;overflow-wrap:anywhere;word-break:normal}
.pantry-item .name-en{white-space:normal;overflow-wrap:anywhere;word-break:normal}
.pantry-controls{display:grid;grid-template-columns:repeat(3,minmax(0,1fr));gap:6px;width:100%;min-width:0}
.pantry-status-group{display:contents}
.st-btn,.pantry-used-up{width:100%;min-width:0;min-height:48px;padding:6px 3px;justify-content:center;text-align:center;white-space:normal;line-height:1.15;overflow-wrap:normal}
.tomorrow-heading{padding-top:10px}
.tomorrow-heading h2{font-size:27px}
.diners-row.people-cards{gap:6px;overflow-x:auto;grid-template-columns:repeat(5,minmax(94px,1fr));padding-bottom:3px}
.diner-chip.person-card{min-height:110px}
.item-actions{gap:4px}
.item-btn{width:42px;height:42px}
}
.content.dishes-page{max-width:1480px;padding:14px 20px 32px}
.dishes-page .dish-grid{grid-template-columns:repeat(2,minmax(0,1fr));gap:12px}
.dishes-page .dish-card{height:100%;display:flex;flex-direction:column}
.dishes-page .dish-card img,.dishes-page .dish-card .no-img{aspect-ratio:4/3}
.dishes-page .dish-card .info{min-height:112px}
@media(min-width:700px){.dishes-page .dish-grid{grid-template-columns:repeat(2,minmax(0,1fr))}}
@media(min-width:1024px){.dishes-page .dish-grid{grid-template-columns:repeat(3,minmax(0,1fr))}}
@media(min-width:1400px){.dishes-page .dish-grid{grid-template-columns:repeat(4,minmax(0,1fr))}}
@media(min-width:961px){.desktop-confirm{display:grid}.tomorrow-actions{display:none}}
"""

# Preview-only Tomorrow UI. Values and component structure mirror the supplied
# reference prototype; production startup does not enable this stylesheet.
TOMORROW_PREVIEW_CSS = """
:root{
--ink:#18211d;--muted:#65706a;--line:#dfe4df;--surface:#ffffff;--canvas:#f4f6f2;
--accent:#246b4b;--accent-dark:#174b34;--accent-soft:#e7f1eb;--warning:#9a5d0b;
--warning-soft:#fff4d9;--danger:#a33b36;--radius-card:16px;--radius-control:10px;
--shadow:0 10px 32px rgba(35,58,45,.07);color-scheme:light;
font-family:"SF Pro Display","PingFang SC","Noto Sans CJK SC","Microsoft YaHei",system-ui,sans-serif
}
html{color-scheme:light;background:var(--canvas);scroll-behavior:smooth}
body{margin:0;min-width:320px;min-height:100dvh;padding:0 0 calc(82px + env(safe-area-inset-bottom));color:var(--ink);background:var(--canvas);font-family:inherit;font-size:17px;line-height:1.5}
button,input{font:inherit}button{color:inherit}
.bilingual-pair{display:inline-flex;flex-direction:column;align-items:flex-start;gap:1px;vertical-align:middle;line-height:1.2}
.bilingual-pair .lang-zh{color:inherit;font-size:1em;font-weight:inherit}
.bilingual-pair .lang-en{color:var(--muted);font-size:.72em;font-weight:550;letter-spacing:.01em}
button .bilingual-pair{align-items:center}button .bilingual-pair .lang-en{color:inherit;opacity:.62}
button:focus-visible,input:focus-visible,a:focus-visible{outline:3px solid rgba(36,107,75,.28);outline-offset:2px}
.site-header{position:sticky;top:0;z-index:30;color:white;background:#17201c;border-bottom:1px solid rgba(255,255,255,.08)}
.header-inner{width:100%;height:66px;margin:auto;padding:0 14px;display:flex;align-items:center;justify-content:space-between}
.brand{display:flex;align-items:center;gap:10px;min-width:0;color:inherit;text-decoration:none}.brand-logo{width:36px;height:36px;display:block;flex:0 0 36px;object-fit:contain}.brand-copy{min-width:0}
.brand strong,.brand small{display:block}.brand strong{font-size:13px;letter-spacing:.02em}.brand small{margin-top:1px;color:#aeb9b3;font-size:10px;letter-spacing:.08em}
.kitchen-switch{display:flex;gap:3px;padding:2px;border:1px solid rgba(255,255,255,.16);border-radius:10px;background:rgba(255,255,255,.06)}
.kitchen-button{min-height:38px;display:flex;align-items:center;gap:7px;padding:0 9px;border:0;border-radius:8px;color:#aeb9b3;background:transparent;font-size:12px;font-weight:700;white-space:nowrap;cursor:pointer}
.kitchen-button.active{color:#11261a;background:#b8d4c2;box-shadow:0 2px 8px rgba(0,0,0,.18)}
.location-dot{display:none}
.kitchen-context{position:sticky;top:66px;z-index:28;min-height:46px;display:grid;place-items:center;color:#6f3500;background:#fff0dc;border-bottom:1px solid #f0b56f;box-shadow:0 4px 16px rgba(191,91,0,.15)}
.kitchen-context-inner{display:flex;align-items:center;justify-content:center;gap:8px;padding:9px 12px;font-size:14px}
.context-pin{width:9px;height:13px;position:relative;flex:0 0 auto;border:2px solid #d95f02;border-radius:50% 50% 50% 0;transform:rotate(-45deg)}
.context-pin:after{content:"";position:absolute;width:3px;height:3px;left:1px;top:1px;border-radius:50%;background:#d95f02}
.kitchen-context strong{color:#b94f00;font-size:16px}
.primary-nav{position:sticky;top:112px;z-index:25;background:rgba(255,255,255,.94);border-bottom:1px solid var(--line);backdrop-filter:blur(14px)}
.nav-inner{width:100%;height:64px;margin:auto;display:flex;gap:0}
.nav-item{position:relative;flex:1;min-width:0;height:100%;padding:7px 4px;border:0;color:#7b847f;background:transparent;cursor:pointer;text-align:center;text-decoration:none}
.nav-item span,.nav-item small{display:block}.nav-item span{font-size:16px;font-weight:700}.nav-item small{margin-top:3px;font-size:11px}
.nav-item:after{content:"";position:absolute;left:50%;bottom:-1px;width:44px;height:3px;border-radius:3px 3px 0 0;background:var(--accent);transform:translateX(-50%) scaleX(0)}
.nav-item.active{color:var(--accent-dark)}.nav-item.active:after{transform:translateX(-50%) scaleX(1)}
.page-shell{width:100%;margin:0 auto;padding:28px 16px 34px}
.page-heading{display:block;position:relative;margin-bottom:20px;padding:0 2px}
.eyebrow{margin:0 0 8px;color:var(--accent);font-size:12px;font-weight:750;letter-spacing:.04em}
.eyebrow .lang-en{margin-top:2px;font-size:10px}
.page-heading h1{margin:0;font-size:35px;line-height:1.08;letter-spacing:-.045em}
.page-heading h1 .lang-en{margin-top:3px;font-size:20px;letter-spacing:-.02em}
.page-heading p:last-child{max-width:100%;margin:9px 0 0;padding-right:0;color:var(--muted);font-size:15px;line-height:1.65}
.page-heading p:last-child .bilingual-pair{gap:4px}.page-heading p:last-child .lang-en{font-size:11px;line-height:1.45}
.status-chip{position:absolute;top:0;right:0;min-width:112px;min-height:54px;display:flex;align-items:center;justify-content:flex-start;gap:9px;padding:9px 10px;border:1px solid #cbd6ce;border-radius:var(--radius-control);color:#435149;background:white;font-size:15px;font-weight:700}
.status-chip>i{width:7px;height:7px;flex:0 0 auto;border-radius:50%;background:#c98728}.status-chip.confirmed>i{background:var(--accent)}
.status-chip .bilingual-pair{align-items:flex-start;white-space:nowrap}
.settings-block,.nutrition-card{padding:17px;border:1px solid var(--line);border-radius:14px;background:var(--surface)}
.diners-panel{margin-bottom:16px;padding:17px 14px 20px}
.section-label{display:flex;align-items:center;justify-content:space-between;margin-bottom:15px;font-size:15px;font-weight:750}
.section-label .lang-en{font-size:10px}.section-label small{color:var(--muted);font-size:14px}.section-label small .lang-en{font-size:10px}
.people-grid{display:grid;grid-template-columns:repeat(5,minmax(0,1fr));gap:7px}
.person{min-width:0;min-height:82px;display:flex;flex-direction:column;align-items:center;justify-content:center;gap:6px;padding:7px 2px;border:1px solid var(--line);border-radius:var(--radius-control);background:white;text-align:center;cursor:pointer}
.person.selected{border-color:#8fb49d;background:var(--accent-soft)}
.person-avatar{width:32px;height:32px;display:grid;place-items:center;border-radius:50%;color:#4f5b54;background:#edf0ed;font-size:12px;font-weight:800}
.person.selected .person-avatar{color:white;background:var(--accent)}
.person-name{font-size:13px;font-weight:700;line-height:1.25}.person-name small{display:block;margin-top:2px;color:var(--muted);font-size:10px;font-weight:500}
.desktop-layout{display:grid;grid-template-columns:1fr;align-items:start;gap:14px}.planner-panel{display:grid;grid-template-columns:1fr;gap:10px}
.settings-block.compact{display:grid;grid-template-columns:1fr auto;align-items:center}.settings-block.compact .section-label{margin:0}
.segmented{display:flex;padding:3px;border-radius:var(--radius-control);background:#eef1ee}.segmented button{min-height:46px;padding:0 13px;border:0;border-radius:8px;color:var(--muted);background:transparent;font-size:14px;font-weight:700;cursor:pointer}
.segmented button.selected{color:var(--ink);background:white;box-shadow:0 1px 4px rgba(20,35,27,.1)}
.banquet-count{grid-column:1/-1;padding-top:13px;border-top:1px solid var(--line)}.banquet-count[hidden]{display:none}
.nutrition-card{background:#fbfcfa}.nutrition-heading{display:flex;align-items:flex-start;justify-content:space-between;gap:12px}
.nutrition-heading small{color:var(--muted);font-size:13px}.nutrition-heading h2{margin:4px 0 12px;font-size:19px;letter-spacing:-.02em}
.nutrition-heading h2 .lang-en{margin-top:3px;font-size:12px}.nutrition-heading>span{flex:0 0 auto;padding:5px 7px;border-radius:7px;color:var(--warning);background:var(--warning-soft);font-size:12px;font-weight:800}
.nutrition-row{display:grid;grid-template-columns:82px 1fr 70px;align-items:center;gap:9px;margin:13px 0;font-size:13px}
.nutrition-row>span .lang-en{font-size:10px}.nutrition-row strong{text-align:right;font-size:12px}.meter{height:6px;overflow:hidden;border-radius:6px;background:#e5e9e5}.meter i{display:block;width:var(--progress);height:100%;background:var(--accent)}
.nutrition-card>p{display:none}.menu-content{display:grid;gap:16px;min-width:0}
.meal-section{overflow:hidden;border:1px solid var(--line);border-radius:14px;background:var(--surface);box-shadow:none}
.meal-header{min-height:82px;display:flex;align-items:center;justify-content:space-between;gap:10px;padding:16px;border-bottom:1px solid #e8ebe8}
.meal-title{display:flex;align-items:stretch;gap:9px}.meal-accent{width:4px;border-radius:4px;background:var(--accent)}
.meal-accent.amber{background:#c78326}.meal-accent.blue{background:#447b9d}.meal-accent.green{background:#4f8b64}.meal-accent.red{background:#a54b45}
.meal-title h2{margin:0;font-size:23px;line-height:1.15;letter-spacing:-.025em}.meal-title h2 .lang-en{margin-top:3px;font-size:13px}.meal-title p{margin:5px 0 0;color:var(--muted);font-size:15px;font-weight:700;line-height:1.25}.meal-title p .lang-en{font-size:10px}
.meal-actions{display:flex;gap:5px}.text-button,.secondary-button,.primary-button{border-radius:var(--radius-control);font-weight:750;cursor:pointer;white-space:nowrap}
.text-button{min-height:50px;padding:0 10px;border:1px solid #ced6d0;color:#34463b;background:white;font-size:13px}.fill-button{color:var(--accent-dark);background:var(--accent-soft);border-color:#c7ddcf}
.dish-grid{display:grid;grid-template-columns:1fr}.dish-card{min-width:0;min-height:128px;display:grid;grid-template-columns:112px minmax(0,1fr);grid-template-rows:1fr auto;column-gap:12px;padding:8px 12px;border-bottom:1px solid #edf0ed}
.dish-card:last-child{border-bottom:0}.dish-card img,.dish-card .no-img{grid-row:1/3;width:112px;height:112px;align-self:center;border-radius:13px;object-fit:cover;background:#edf0ed}
.dish-card .no-img{display:grid;place-items:center;color:var(--muted);font-size:12px}.dish-card .no-img[hidden]{display:none!important}.dish-copy{min-width:0;align-self:center;padding-right:110px}.dish-copy h3{margin:0;font-size:17px;line-height:1.35;letter-spacing:-.015em}.dish-copy h3 .lang-en{margin-top:3px;font-size:12px;line-height:1.35;font-weight:550}
.dish-actions{grid-column:2;display:flex;justify-content:flex-end;gap:5px;margin-top:-32px;align-self:end}.dish-actions button{width:32px;min-height:32px;overflow:hidden;padding:0;border-radius:7px;background:white;color:transparent;font-size:0;cursor:pointer}
.smart-button,.search-button{border:1px solid #ccd5cf}.remove-button{border:1px solid #ead0cd}.smart-button:after{content:"↻";color:#526058;font-size:17px}.search-button:after{content:"⌕";color:#356b52;font-size:18px}.remove-button:after{content:"×";color:var(--danger);font-size:18px}
.date-band{display:flex;align-items:end;justify-content:space-between;gap:12px;margin:24px 2px 10px}.date-band h2{margin:0;font-size:22px}.date-band p{margin:2px 0 0;color:var(--muted);font-size:11px}.meal-links{display:flex;flex-wrap:wrap;align-items:center;gap:7px 10px;margin-top:7px}.meal-link{padding:0;border:0;color:var(--accent-dark);background:transparent;font-size:11px;font-weight:700;cursor:pointer}.meal-link[disabled]{color:var(--muted);cursor:default}.meal-link small{display:block;font-size:8px;font-weight:550}.meal-note-button{min-height:30px;padding:5px 9px;border:1px solid #94baa3;border-radius:7px;color:var(--accent-dark);background:var(--accent-soft);font-size:11px;font-weight:750;cursor:pointer}.meal-skip-button{width:34px;height:34px;padding:0;border:1px solid #ead0cd;border-radius:8px;color:var(--danger);background:#fff;font-size:20px;line-height:1;cursor:pointer}.meal-note{margin:0 14px 10px;padding:8px 11px;border-radius:8px;color:#526058;background:#f3f6f3;font-size:11px}.skipped-meal,.add-meal-card{margin:0 14px 14px;padding:18px;border:1px dashed #aac0b1;border-radius:11px;color:var(--accent-dark);background:var(--accent-soft);font-size:13px;font-weight:750}.skipped-meal{display:flex;align-items:center;justify-content:space-between;gap:12px}.restore-meal-button{min-height:36px;padding:6px 10px;border:1px solid #94baa3;border-radius:7px;color:var(--accent-dark);background:#fff;font-weight:750;cursor:pointer}.add-meal-card{width:calc(100% - 28px);text-align:left;cursor:pointer}.meal-bottom-sheet{width:min(560px,100%);padding:18px 16px calc(16px + env(safe-area-inset-bottom))}.meal-bottom-sheet h2{margin:0 0 14px}.sheet-diners{display:grid;grid-template-columns:repeat(2,1fr);gap:8px}.sheet-diners button.selected{border-color:var(--accent);color:var(--accent-dark);background:var(--accent-soft)}.sheet-note{width:100%;min-height:110px;padding:12px;border:1px solid var(--line);border-radius:9px;font:inherit;resize:vertical}.sheet-footer{display:flex;gap:8px;margin-top:14px}.sheet-footer button{flex:1;text-align:center}
.inline-warning{display:flex;align-items:flex-start;flex-direction:column;gap:2px;margin:12px 14px 0;padding:11px 13px;border-left:3px solid #d59129;border-radius:8px;color:var(--warning);background:var(--warning-soft);font-size:11px}.inline-warning span{color:#79531d}
.empty-state{min-height:96px;display:flex;align-items:center;gap:13px;padding:14px}.empty-icon{width:42px;height:42px;display:grid;place-items:center;border:1px dashed #aac0b1;border-radius:10px;color:var(--accent);background:var(--accent-soft);font-size:21px}.empty-state strong{font-size:13px}.empty-state p{max-width:190px;margin:4px 0 0;color:var(--muted);font-size:10px}.empty-state button{margin-left:auto}
.mobile-action-bar{position:fixed;left:0;right:0;bottom:0;z-index:40;display:grid;grid-template-columns:.85fr 1.4fr;gap:8px;padding:10px 14px calc(10px + env(safe-area-inset-bottom));border-top:1px solid #d9dfda;background:rgba(255,255,255,.96);backdrop-filter:blur(14px)}
.desktop-owner-actions{display:none;grid-template-columns:.85fr 1.4fr;gap:8px;padding:12px;border:1px solid #c7ded1;border-radius:14px;background:#eef6f1}
.banquet-count{display:flex;align-items:center;justify-content:space-between;gap:8px;margin-top:12px;padding:9px 10px;border-radius:10px;background:#f3f6f3}.banquet-count[hidden]{display:none}.banquet-stepper{display:flex;align-items:center;gap:8px}.banquet-stepper button{width:32px;height:32px;border:1px solid #cdd6ce;border-radius:9px;background:#fff;font-size:20px}.banquet-stepper output{min-width:30px;text-align:center;font-weight:800}
.secondary-button,.primary-button{width:100%;min-height:52px;padding:0 16px;border:1px solid #bfc9c2;font-size:14px}.secondary-button{color:var(--accent-dark);background:white}.primary-button{border-color:var(--accent-dark);color:white;background:var(--accent-dark)}
.snack-bar{bottom:calc(84px + env(safe-area-inset-bottom));color:white;background:#17201c}.footer,.warning-box{display:none!important}
.modal-overlay{background:rgba(12,20,15,.54);backdrop-filter:blur(5px)}.modal{border-radius:18px 18px 0 0;background:var(--surface)}
.modal-body,.modal-actions{background:var(--surface)}.modal input{color:var(--ink);background:white;border-color:var(--line)!important;font-style:normal}
.dish-picker{width:min(720px,100%);max-width:720px;max-height:88dvh;display:grid;grid-template-rows:auto auto minmax(0,1fr) auto;overflow:hidden}
.dish-picker-head{display:flex;align-items:flex-start;justify-content:space-between;gap:16px;padding:18px 16px 12px}.dish-picker-title{font-size:23px;font-weight:750;line-height:1.1}.dish-picker-title .lang-en{margin-top:5px;font-size:17px}
.dish-picker-head p{margin:8px 0 0;color:var(--muted);font-size:12px;line-height:1.35}.dish-picker-head p .lang-en{font-size:10px}.picker-close{width:40px;height:40px;flex:0 0 auto;border:0;border-radius:9px;background:#eef1ee;font-size:23px;line-height:1;cursor:pointer}
.dish-picker-search{width:auto;height:48px;margin:0 16px 12px;padding:0 14px;border:1px solid var(--line);border-radius:10px}.dish-picker-results{min-height:0;overflow:auto;border-top:1px solid var(--line)}
.rec-section-title{position:sticky;top:0;z-index:1;padding:10px 16px;background:#f1f4f1;border-radius:0;color:var(--ink);font-size:14px;font-weight:750}.rec-section-title .lang-en{font-size:10px}
.recommendation-item{width:100%;min-height:110px;display:grid;grid-template-columns:86px minmax(0,1fr);align-items:center;gap:13px;padding:10px 16px;border:0;border-bottom:1px solid var(--line);background:var(--surface);text-align:left;cursor:pointer}.recommendation-item:hover{background:#f7faf8}.recommendation-item img,.rec-no-img{width:86px;height:86px;border-radius:11px;object-fit:cover;background:#edf0ed}.rec-no-img{display:grid;place-items:center;color:var(--muted);font-size:10px}.rec-no-img[hidden]{display:none!important}
.recommendation-copy{min-width:0}.recommendation-copy strong,.recommendation-copy em,.recommendation-copy small{display:block}.recommendation-copy strong{font-size:15px}.recommendation-copy strong .bilingual-pair{align-items:flex-start;text-align:left}.recommendation-copy strong .lang-en{margin-top:3px;font-size:11px}.recommendation-copy em{width:max-content;margin-top:7px;padding:4px 6px;border-radius:5px;font-size:10px;font-style:normal}.recommendation-copy em .lang-en{font-size:8px}.recommendation-copy em.available{color:var(--accent-dark);background:var(--accent-soft)}.recommendation-copy em.missing{color:#8a5d08;background:#fff1cf}.recommendation-copy small{margin-top:4px;color:var(--muted);font-size:10px}
.dish-picker .modal-actions{position:static;padding:10px 16px calc(12px + env(safe-area-inset-bottom));border-color:var(--line)}
@media(min-width:701px){.modal-overlay:has(.dish-picker){align-items:center;padding:18px}.dish-picker{max-height:min(820px,92dvh);border-radius:18px}.dish-picker-head{padding:22px 24px 14px}.dish-picker-title{font-size:28px}.dish-picker-title .lang-en{font-size:19px}.dish-picker-search{margin:0 24px 14px}.rec-section-title{padding:11px 24px}.recommendation-item{grid-template-columns:78px minmax(0,1fr);min-height:100px;padding:11px 24px}.recommendation-item img,.rec-no-img{width:78px;height:78px}.dish-picker .modal-actions{padding:12px 24px 18px}}
@media(min-width:701px){body{padding-bottom:0;font-size:16px}.header-inner,.nav-inner,.page-shell{width:min(1180px,calc(100% - 40px))}.header-inner{height:76px;padding:0}.kitchen-context{top:76px;min-height:52px}.primary-nav{top:128px}.nav-inner{height:70px;justify-content:center}.nav-item{flex:0 0 auto;min-width:150px}.page-shell{padding:38px 0 96px}.page-heading{display:flex;align-items:flex-end;justify-content:space-between;margin-bottom:28px}.page-heading p:last-child{max-width:none;padding:0}.status-chip{position:static}.diners-panel{padding:24px 34px 30px}.person{min-height:150px}.person-avatar{width:56px;height:56px;font-size:17px}.person-name{font-size:22px}.desktop-layout{grid-template-columns:300px minmax(0,1fr);gap:24px}.planner-panel{position:sticky;top:222px;gap:14px}.meal-section{border-radius:16px;box-shadow:var(--shadow)}.dish-grid{grid-template-columns:1fr 1fr}.dish-card{grid-template-columns:88px minmax(0,1fr);min-height:auto;padding:16px 18px}.dish-card img,.dish-card .no-img{width:88px;height:88px;border-radius:12px}.mobile-action-bar{display:none}}
@media(min-width:701px) and (max-width:960px){.mobile-action-bar{display:grid}}
@media(min-width:961px){.desktop-owner-actions{display:grid}.mobile-action-bar{display:none}}
/* Preview Pantry */
.pantry-page{padding-bottom:56px}
.pantry-page .bilingual-pair{align-items:flex-start;text-align:left}
.view-heading{display:flex;align-items:flex-end;justify-content:space-between;gap:16px;margin-bottom:18px}
.view-heading>div{min-width:0}.view-heading h1{margin:0;font-size:38px;font-weight:800;line-height:1.04;letter-spacing:-.04em}.view-heading h1 .lang-en{margin-top:4px;font-size:24px;letter-spacing:-.02em}
.view-heading p{max-width:560px;margin:9px 0 0;color:var(--muted);font-size:14px;line-height:1.42}.view-heading p .bilingual-pair{gap:2px}.view-heading p .lang-en{font-size:11px}
.view-count{min-width:84px;min-height:72px;flex:0 0 auto;display:grid;place-items:center;padding:8px;border-radius:14px;color:var(--accent-dark);background:var(--accent-soft);text-align:center}.view-count .bilingual-pair{align-items:center}.view-count .lang-zh{font-size:28px;font-weight:800;line-height:1}.view-count .count-unit{margin-top:2px;font-size:12px}.view-count .lang-en{font-size:10px}
.attention-banner{display:grid;grid-template-columns:auto minmax(0,1fr);align-items:start;gap:13px;margin-bottom:14px;padding:14px 16px;border:1px solid #edc99d;border-left:4px solid #d4660b;border-radius:14px;background:#fff8ee}.attention-banner>strong{color:#d4660b;font-size:40px;line-height:1}.attention-banner-copy{min-width:0;display:grid;grid-template-columns:minmax(105px,.8fr) minmax(0,1.5fr);gap:14px}.attention-banner-copy b,.attention-banner-copy span{display:block}.attention-banner-copy b{font-size:14px;line-height:1.2}.attention-banner-copy b small{display:block;margin-top:2px;color:var(--muted);font-size:9px}.attention-banner-copy span{color:#6d4120;font-size:12px;line-height:1.35}.attention-banner-copy span small{display:block;margin-top:2px;color:var(--muted);font-size:9px}
.pantry-toolbar{display:grid;gap:10px;margin-bottom:14px;padding:14px;border:1px solid var(--line);border-radius:14px;background:var(--surface)}.pantry-toolbar label{display:grid;gap:8px;font-size:14px;font-weight:750}.pantry-toolbar label .lang-en{font-size:9px}
.pantry-toolbar input{width:100%;height:48px;padding:0 13px;border:1px solid #cbd4ce;border-radius:9px;color:var(--ink);background:#fff;font-size:14px;font-style:normal;outline:0}.pantry-toolbar input::placeholder{color:#7a827e;opacity:1}.pantry-toolbar input:focus{border-color:var(--accent)}
.pantry-add{width:100%;min-height:48px;border:1px solid var(--accent-dark);border-radius:9px;color:#fff;background:var(--accent-dark);font-size:13px;font-weight:750;cursor:pointer}.pantry-add .lang-en{color:#fff}
.pantry-search-results{display:none;overflow:hidden;border:1px solid var(--line);border-radius:9px;background:#fff}.pantry-search-results.visible{display:grid}.pantry-search-results button{min-height:44px;padding:7px 10px;border:0;border-bottom:1px solid var(--line);background:#fff;text-align:left;cursor:pointer}.pantry-search-results button:last-child{border-bottom:0}.pantry-search-results strong,.pantry-search-results small{display:block}.pantry-search-results strong{font-size:12px}.pantry-search-results small{color:var(--muted);font-size:9px}
.pantry-feedback{color:var(--muted);font-size:10px}.pantry-feedback:empty{display:none}.pantry-feedback.error{color:#a33b36}.pantry-feedback.success{color:var(--accent)}
.pantry-layout{display:grid;grid-template-columns:1fr;align-items:start;gap:12px}.inventory-panel,.pantry-aside{min-width:0;overflow:hidden;border:1px solid var(--line);border-radius:14px;background:#fff}
.inventory-panel>header{display:flex;align-items:center;justify-content:space-between;gap:12px;padding:14px;border-bottom:1px solid var(--line)}.inventory-panel>header h2,.pantry-aside h2{margin:0;font-size:18px;line-height:1.15;letter-spacing:-.02em}.inventory-panel>header h2 .lang-en,.pantry-aside h2 .lang-en{margin-top:2px;font-size:11px}.inventory-meta{margin:5px 0 0;color:var(--muted);font-size:10px;line-height:1.25}.inventory-meta .lang-en{font-size:8px}
.same-last{min-width:190px;min-height:48px;flex:0 0 auto;padding:5px 12px;border:1px solid #cbd4ce;border-radius:8px;color:#3b4941;background:#fff;font-size:13px;font-weight:700;cursor:pointer}.same-last .lang-en{font-size:11px}
.inventory-filters{display:grid;grid-template-columns:repeat(4,minmax(0,1fr));padding:8px 10px 0;border-bottom:1px solid var(--line)}.inventory-filter{position:relative;min-width:0;min-height:56px;padding:4px 2px 9px;border:0;color:var(--muted);background:#fff;font-size:14px;font-weight:700;cursor:pointer}.inventory-filter .bilingual-pair{align-items:center;text-align:center}.inventory-filter .lang-en{font-size:11px}.inventory-filter.active{color:var(--accent-dark)}.inventory-filter.active:after{content:"";position:absolute;left:20%;right:20%;bottom:-1px;height:3px;border-radius:3px;background:var(--accent)}
.inventory-list{display:grid}.ingredient-row{min-height:86px;display:grid;grid-template-columns:minmax(130px,1fr) minmax(260px,330px);align-items:center;gap:12px;padding:12px;border-bottom:1px solid #edf0ed}.ingredient-row:last-child{border-bottom:0}.ingredient-row[hidden]{display:none!important}
.ingredient-row.ingredient-highlight{background:#fff8ee;box-shadow:inset 3px 0 #d4660b;transition:background .2s ease}
.ingredient-name{min-width:0;line-height:1.05}.ingredient-name strong{font-size:16px;font-weight:700}.ingredient-name small{margin-left:7px;color:var(--muted);font-size:12px;font-weight:500}.stock-actions{display:grid;grid-template-columns:repeat(3,minmax(0,1fr));gap:5px}.stock-button{min-width:0;height:40px;padding:3px 5px;border:1px solid #cbd4ce;border-radius:7px;color:#526058;background:#fff;font-size:12px;font-weight:650;line-height:1.05;cursor:pointer}.stock-button .bilingual-pair{align-items:center;text-align:center}.stock-button .lang-en{font-size:9px}.stock-button.selected-priority{border-color:#8fb99f;color:var(--accent-dark);background:var(--accent-soft)}.stock-button.selected-expiring{border-color:#eb9c92;color:#8c302d;background:#fae0de}.stock-button.used-up{color:#8c302d}.stock-button.selected-used,.stock-button.used-up:active{border-color:#eb9c92;color:#8c302d;background:#fae0de}.stock-button[disabled]{opacity:.6;cursor:wait}
.inventory-empty{padding:30px 16px;text-align:center}.inventory-empty strong,.inventory-empty small{display:block}.inventory-empty small{margin-top:3px;color:var(--muted);font-size:10px}
.ingredient-row{grid-template-columns:minmax(150px,1fr) minmax(190px,240px)}.ingredient-copy{min-width:0}.stock-actions.two-actions{grid-template-columns:repeat(2,minmax(0,1fr))}.stock-button.attention-toggle{color:#526058;border-color:#cbd4ce;background:#fff}.stock-button.clear-attention{color:#9a4915;border-color:#e5a75f;background:#fff0dc}.stock-button.used-up-button{color:#8c302d;border-color:#eb9c92;background:#fae0de}.ingredient-row[data-status="expiring"] .stock-button.used-up-button{color:#fff;border-color:#a6403c;background:#a6403c}
.inline-action{flex-direction:row!important;align-items:baseline!important;justify-content:center;gap:4px;white-space:nowrap;text-align:center}
.inventory-status{display:flex;align-items:center;gap:9px;margin-top:7px;font-size:12px;font-weight:750}.inventory-status>.bilingual-pair{padding:5px 8px;border-radius:6px}.inventory-status .lang-en{font-size:10px}.needs-status>.bilingual-pair{color:#a84d00;background:#fff0dc}
.inventory-filters{grid-template-columns:repeat(var(--filter-count,4),minmax(0,1fr))}
body:has(.pantry-page) .nav-item span{font-size:18px}body:has(.pantry-page) .nav-item small{font-size:12px}
.view-heading p .lang-en{white-space:nowrap}.pantry-toolbar label .inline-label{flex-direction:row;align-items:baseline;gap:4px;white-space:nowrap}.meta-inline{display:flex;align-items:baseline;gap:4px;white-space:nowrap}.meta-inline small{color:var(--muted);font-size:8px;font-weight:550}.inventory-filter{font-size:14px}.inventory-filter .lang-en{font-size:11px}.stock-button .inline-action .lang-en{font-size:10px}
.pantry-main{min-width:0;display:grid;gap:12px}
.recently-used-panel{overflow:hidden;border:1px solid var(--line);border-radius:14px;background:#fff}.recently-used-panel>header{display:flex;align-items:flex-end;justify-content:space-between;gap:12px;padding:13px 14px;border-bottom:1px solid var(--line)}.recently-used-panel h2{margin:0;font-size:18px;line-height:1.15;letter-spacing:-.02em}.recently-used-panel h2 .lang-en{margin-top:2px;font-size:12px}.recently-used-panel>header p{margin:0;color:var(--muted);font-size:10px;text-align:right}.recently-used-panel>header p .bilingual-pair{align-items:flex-end}.recently-used-panel>header p .lang-en{font-size:8px}
.recently-used-list{display:grid;background:#f2f2f2}.recently-used-item{min-height:58px;display:flex;align-items:center;justify-content:space-between;gap:12px;padding:10px 14px;border-bottom:1px solid #e2e2e2}.recently-used-item:last-child{border-bottom:0}.recently-used-item p{margin:0;color:var(--muted);font-size:10px;text-align:right;white-space:nowrap}.recently-used-item p .bilingual-pair{align-items:flex-end}.recently-used-item p .lang-en{font-size:8px}.recently-used-empty{padding:20px 14px;color:var(--muted);font-size:12px;text-align:center}.recently-used-empty small{display:block;margin-top:3px;font-size:9px}
.recently-used-item p strong,.recently-used-item p time,.recently-used-item p small{display:block}.recently-used-item p strong{color:var(--ink);font-size:10px}.recently-used-item p time{margin-top:1px;font-size:10px}.recently-used-item p small{margin-top:2px;font-size:8px}
.pantry-aside section{padding:15px}.pantry-aside p{margin:6px 0 12px;color:var(--muted);font-size:10px;line-height:1.3}.pantry-aside p small{display:block;margin-top:2px;font-size:8px}.common-chips{display:flex;flex-wrap:wrap;gap:6px}.common-chip{min-height:38px;display:inline-flex;align-items:center;gap:7px;padding:5px 8px;border:1px solid #aab9af;border-radius:8px;color:#33443a;background:#fff;font-size:11px;font-weight:700;line-height:1.05;cursor:pointer}.common-chip .bilingual-pair{flex-direction:row;align-items:baseline;gap:4px;white-space:nowrap}.common-chip .lang-en{font-size:8px}.common-chip .chip-icon{width:15px;height:15px;display:grid;place-items:center;flex:0 0 auto;border-radius:50%;color:var(--accent);background:var(--accent-soft);font-size:11px}.common-chip.selected{border-color:var(--accent-dark);color:#fff;background:var(--accent-dark)}.common-chip.selected .lang-en{color:#d4e6dc}.common-chip.selected .chip-icon{color:var(--accent-dark);background:#d8eadf}.common-chip[disabled]{opacity:.6;cursor:wait}
.pantry-snack{position:fixed;left:50%;bottom:18px;z-index:80;max-width:calc(100% - 30px);padding:9px 14px;border-radius:9px;color:#fff;background:#17201c;font-size:11px;opacity:0;pointer-events:none;transform:translate(-50%,10px);transition:.18s}.pantry-snack.show{opacity:1;transform:translate(-50%,0)}
@media(max-width:700px){body:has(.pantry-page){padding-bottom:env(safe-area-inset-bottom)}.pantry-page{padding-top:20px;padding-bottom:40px}.view-heading{align-items:flex-start;gap:10px;margin-bottom:14px}.view-heading h1{font-size:30px}.view-heading h1 .lang-en{font-size:18px}.view-heading p{max-width:245px;margin-top:7px;font-size:11px}.view-heading p .lang-en{font-size:8px}.view-count{min-width:76px;min-height:64px}.view-count .lang-zh{font-size:24px}.attention-banner{grid-template-columns:38px minmax(0,1fr);gap:9px;padding:12px 13px}.attention-banner>strong{font-size:34px}.attention-banner-copy{grid-template-columns:1fr;gap:6px}.attention-banner-copy b{font-size:12px}.attention-banner-copy span{font-size:10px}.pantry-toolbar{padding:12px}.inventory-panel>header{align-items:stretch;flex-direction:column;padding:12px}.same-last{width:100%;min-width:0;min-height:44px}.inventory-filters{padding-inline:4px}.inventory-filter{font-size:14px;padding-inline:1px}.ingredient-row{grid-template-columns:1fr;gap:8px;min-height:84px;padding:12px}.stock-actions.one-action{max-width:none;margin-left:0}.stock-button{height:40px}.pantry-aside section{padding:13px}}
@media(max-width:700px){.inventory-filter .lang-en{font-size:11px}.brand-logo{width:32px;height:32px;flex-basis:32px}}
@media(min-width:701px){.pantry-page{padding-top:34px}.pantry-toolbar{grid-template-columns:minmax(0,1fr) 210px;align-items:end}.pantry-search-results,.pantry-feedback{grid-column:1/-1}.view-heading h1{font-size:42px}}
@media(min-width:961px){.pantry-layout{grid-template-columns:minmax(0,1fr) 292px;gap:16px}.pantry-aside{position:sticky;top:222px}}
@media(max-width:390px){.header-inner{padding-inline:8px}.brand strong{font-size:12px}.brand small{font-size:9px;letter-spacing:.03em}.kitchen-switch{gap:1px}.kitchen-button{padding-inline:4px;font-size:10px}.people-grid{gap:6px}.page-shell{padding-inline:14px}}
"""


def bilingual(zh, en, extra=""):
    return (
        f'<span class="bilingual-pair {extra}">'
        f'<span class="lang-zh">{zh}</span><span class="lang-en">{en}</span></span>'
    )


def favicon_links():
    return (
        '<link rel="icon" type="image/png" sizes="32x32" href="/favicon-32x32.png">'
        '<link rel="icon" type="image/png" sizes="16x16" href="/favicon-16x16.png">'
        '<link rel="shortcut icon" href="/favicon.ico">'
        '<link rel="apple-touch-icon" sizes="180x180" href="/apple-touch-icon.png">'
    )


def render_login(error=""):
    error_html = (
        f'<div class="login-error" role="alert">{escape(error)}<small>Login failed. Please try again.</small></div>'
        if error else ""
    )
    return f"""<!doctype html><html lang="zh-CN"><head>
<meta charset="UTF-8"><meta name="viewport" content="width=device-width,initial-scale=1,viewport-fit=cover">
<meta name="theme-color" content="#173f2b"><meta name="color-scheme" content="light">
{favicon_links()}<title>登录 · Family Menu</title><style>
*{{box-sizing:border-box}}html,body{{margin:0;min-height:100%}}body{{min-height:100dvh;display:grid;place-items:center;padding:24px 18px calc(24px + env(safe-area-inset-bottom));font-family:-apple-system,BlinkMacSystemFont,"Segoe UI","PingFang SC","Hiragino Sans GB",sans-serif;color:#17201c;background:#f4f6f2}}
.login-shell{{width:min(100%,420px)}}.login-brand{{display:flex;align-items:center;gap:10px;margin-bottom:28px}}.login-brand-logo{{width:46px;height:46px;display:block;flex:0 0 46px;object-fit:contain}}.login-brand strong,.login-brand small{{display:block}}.login-brand strong{{font-size:21px;line-height:1.1}}.login-brand small{{margin-top:4px;color:#65706a;font-size:13px}}
.login-panel{{padding:26px 22px;border:1px solid #d9dfda;border-radius:16px;background:#fff;box-shadow:0 18px 50px rgba(35,58,45,.10)}}h1{{margin:0;font-size:30px;line-height:1.08;letter-spacing:-.035em}}h1 small{{display:block;margin-top:7px;color:#65706a;font-size:17px;font-weight:600;letter-spacing:-.01em}}.login-intro{{margin:13px 0 22px;color:#65706a;font-size:14px;line-height:1.55}}.login-intro small{{display:block;font-size:12px}}
.login-field{{display:grid;gap:7px;margin-top:15px}}.login-field label{{font-size:14px;font-weight:750}}.login-field label small{{margin-left:5px;color:#65706a;font-size:11px;font-weight:550}}.login-field input{{width:100%;height:50px;padding:0 13px;border:1px solid #bfc9c2;border-radius:10px;color:#17201c;background:#fff;font-size:16px;outline:none;-webkit-appearance:none}}.login-field input:focus{{border-color:#245b3d;box-shadow:0 0 0 3px rgba(36,91,61,.14)}}.login-submit{{width:100%;min-height:52px;margin-top:22px;border:1px solid #173f2b;border-radius:10px;color:#fff;background:#173f2b;font-size:15px;font-weight:750;cursor:pointer;-webkit-appearance:none}}.login-submit:active{{transform:translateY(1px)}}
.login-error{{margin:0 0 18px;padding:11px 12px;border:1px solid #e7aaa3;border-radius:10px;color:#8c302d;background:#fff0ef;font-size:13px;font-weight:700}}.login-error small{{display:block;margin-top:3px;font-size:11px;font-weight:550}}.login-note{{margin:16px 2px 0;color:#77817b;font-size:11px;line-height:1.5;text-align:center}}@media(max-width:390px){{.login-panel{{padding:22px 18px}}h1{{font-size:27px}}}}
</style></head><body><main class="login-shell">
<div class="login-brand"><img class="login-brand-logo" src="/assets/family-menu-logo.png" alt="家庭菜单 Family Menu"><span><strong>家庭菜单</strong><small>Family Menu</small></span></div>
<section class="login-panel"><h1>欢迎回来<small>Welcome back</small></h1>
<p class="login-intro">登录后查看家庭菜单与库存。<small>Sign in to view the family menu and pantry.</small></p>{error_html}
<form method="post" action="/login" accept-charset="UTF-8">
<div class="login-field"><label for="username">账号 <small>Username</small></label><input id="username" name="username" type="text" autocomplete="username" autocapitalize="none" spellcheck="false" required maxlength="80"></div>
<div class="login-field"><label for="password">密码 <small>Password</small></label><input id="password" name="password" type="password" autocomplete="current-password" required maxlength="1024"></div>
<button class="login-submit" type="submit">登录 · Sign in</button></form>
</section><p class="login-note">登录状态仅保存在安全 Cookie 中。<br>Session credentials are stored in a secure cookie only.</p>
</main></body></html>"""


def logout_form(extra_style=""):
    return (
        f'<form method="post" action="/logout" style="margin:0;{extra_style}">'
        '<button type="submit" aria-label="退出登录 / Sign out" '
        'style="min-height:36px;padding:0 10px;border:1px solid rgba(255,255,255,.28);border-radius:8px;color:#eef5f0;background:transparent;font-size:12px;font-weight:700;cursor:pointer">'
        '退出 <small style="display:block;font-size:9px;font-weight:550">Sign out</small></button></form>'
    )


def tomorrow_preview_head(title, active_nav="tomorrow", location="shenzhen"):
    nav_items = [
        ("tomorrow", "餐单", "Meal Plan"), ("pantry", "食材", "Pantry"),
        ("dishes", "菜品", "Dishes"), ("history", "历史", "History"),
    ]
    nav_html = "".join(
        f'<a href="/{path}" class="nav-item {"active" if path == active_nav else ""}">'
        f'<span>{cn}</span><small>{en}</small></a>' for path, cn, en in nav_items
    )
    kitchen_buttons = "".join(
        f'<button class="kitchen-button {"active" if loc_id == location else ""}" '
        f'type="button" onclick="switchPreviewKitchen(\'{loc_id}\')">{label}</button>'
        for loc_id, label in LOCATIONS.items()
    )
    return f"""<!doctype html><html lang="zh-CN"><head>
<meta charset="UTF-8"><meta name="viewport" content="width=device-width,initial-scale=1,viewport-fit=cover">
<meta name="theme-color" content="#f4f6f2"><meta name="color-scheme" content="light">
{favicon_links()}<title>{title}</title><style>{CSS}{TOMORROW_PREVIEW_CSS}</style></head><body>
<header class="site-header"><div class="header-inner"><a class="brand" href="/tomorrow">
<img class="brand-logo" src="/assets/family-menu-logo.png" alt="家庭菜单 Family Menu"><span class="brand-copy"><strong>家庭菜单</strong><small>Family Menu</small></span></a>
<div style="display:flex;align-items:center;gap:8px"><div class="kitchen-switch">{kitchen_buttons}</div>{logout_form()}</div></div></header>
<div class="kitchen-context"><div class="kitchen-context-inner"><span class="context-pin"></span>
<span>当前厨房 Kitchen:</span><strong>{LOCATIONS.get(location, location)}</strong></div></div>
<nav class="primary-nav"><div class="nav-inner">{nav_html}</div></nav>
<script>function switchPreviewKitchen(loc){{document.cookie='loc='+loc+';path=/';location.reload();}}</script>"""

def page_head(title, active_nav="", location="shenzhen", role="owner"):
    loc_btns = ""
    for loc_id, loc_label in LOCATIONS.items():
        cls = "active" if loc_id == location else ""
        loc_btns += f'<button class="loc-btn {cls}" onclick="switchLocation(\'{loc_id}\')">{loc_label}</button>'
    nav_items = [
        ("tomorrow", "餐单", "Meal Plan"),
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
<meta name="viewport" content="width=device-width,initial-scale=1.0,maximum-scale=1.0,user-scalable=no,viewport-fit=cover">
<meta name="apple-mobile-web-app-capable" content="yes">
<meta name="apple-mobile-web-app-status-bar-style" content="default">
<meta name="apple-mobile-web-app-title" content="家庭菜单">
<meta name="theme-color" content="#2c2620">
<link rel="manifest" href="/manifest.webmanifest">{favicon_links()}
<title>{title}</title>
<style>{CSS}</style>
</head>
<body>
<div class="header">
<div class="legacy-brand"><img class="legacy-brand-logo" src="/assets/family-menu-logo.png" alt="家庭菜单 Family Menu"><h1>家庭菜单<span>Family Menu</span></h1></div>
<div style="display:flex;align-items:center;gap:8px"><div class="loc-switch">{loc_btns}</div>{logout_form()}</div>
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
function showWarningCard(title,content){{
  let card=document.getElementById('warnCard')||document.createElement('div');
  card.id='warnCard';
  card.style.cssText='position:fixed;top:50%;left:50%;transform:translate(-50%,-50%);background:#fff8e1;border:2px solid #e6a700;border-radius:12px;padding:20px;max-width:90vw;max-height:80vh;overflow-y:auto;z-index:400;box-shadow:0 4px 20px rgba(0,0,0,.15);font-size:14px;color:#2c2620';
  card.innerHTML='<div style="font-size:16px;font-weight:700;margin-bottom:12px;color:#e6a700">'+title+'</div><div style="line-height:1.6">'+content+'</div><div style="margin-top:16px;text-align:center"><button onclick="document.getElementById(\\'warnCard\\').remove()" style="padding:8px 24px;border:none;border-radius:20px;background:#2c2620;color:#faf7f2;font-size:14px;cursor:pointer">关闭 Close</button></div>';
  document.body.appendChild(card);
}}
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

def render_tomorrow_reference_preview(role="owner", location="shenzhen"):
    """Render the interactive local preview with the supplied prototype DOM."""
    tomorrow = get_tomorrow_date()
    is_owner = role == "owner"
    if is_owner:
        ensure_tomorrow_menu(location)
    menu = get_menu_with_dishes(tomorrow)
    if not menu.get("exists"):
        return tomorrow_preview_head("明日菜单 · Tomorrow Menu", "tomorrow", location) + \
            '<main class="page-shell"><div class="empty">明日菜单未生成</div></main></body></html>'

    is_editable = is_owner and menu["status"] == "draft"

    all_diners = get_all_diners()
    menu_diners = get_menu_diners(menu["menu_id"])
    if not menu_diners:
        menu_diners = [d["id"] for d in all_diners if d["default_attends"]]
    meal_mode_info = get_menu_meal_mode(menu["menu_id"])
    meal_mode = meal_mode_info["meal_mode"]
    banquet_total = meal_mode_info["banquet_total_diners"] or 8
    effective_diners = banquet_total if meal_mode == "banquet" else len(menu_diners)

    # Legacy combo rows are snapshots of whole meals, not individual dishes.
    # When rendering the live menu, only current dish records are actionable.
    valid_meals = {
        meal_type: [dish for dish in dishes if not dish.get("is_historical_combo")]
        for meal_type, dishes in menu["meals"].items()
    }

    menu_validation = validate_menu_meals(menu, effective_diners)
    slot_results = menu_validation["meal_slots"]

    def aggregate_progress(slot_by_meal):
        current = 0
        target = 0
        for meal_type, slot_name in slot_by_meal:
            slot = slot_results.get(meal_type, {}).get(slot_name, {})
            slot_target = slot.get("target_min", 0)
            current += min(slot.get("current", 0), slot_target)
            target += slot_target
        return min(100, round(current / target * 100)) if target else 100

    nutrition_values = {
        "protein": aggregate_progress((("breakfast", "protein_main"), ("lunch", "protein_main"), ("dinner", "protein_main"))),
        "vegetables": aggregate_progress((("breakfast", "vegetable"), ("lunch", "vegetable_dish"), ("dinner", "vegetable_dish"))),
        "staple": aggregate_progress((("breakfast", "staple"), ("lunch", "staple"), ("dinner", "staple"))),
    }
    missing_by_meal = menu_validation.get("missing_by_meal") or {
        meal_type: {
            slot_name: slot
            for slot_name, slot in slots.items()
            if slot.get("missing_min", 0) > 0
        }
        for meal_type, slots in slot_results.items()
    }
    issue_keys = independent_missing_issue_keys(missing_by_meal)
    pending_count = len(issue_keys)
    if any("protein_main" in missing for missing in missing_by_meal.values()):
        nutrition_title_cn, nutrition_title_en = "还差一点蛋白质", "A little more protein needed"
    elif any(missing_by_meal.values()):
        nutrition_title_cn, nutrition_title_en = "餐次还需补充", "Meals still need attention"
    elif pending_count:
        nutrition_title_cn, nutrition_title_en = "还需要补充营养搭配", "A little more balance needed"
    else:
        nutrition_title_cn, nutrition_title_en = "今日搭配已均衡", "Today's menu is balanced"

    menu_date = datetime.strptime(menu["date"], "%Y-%m-%d")
    weekday_cn = ["星期一", "星期二", "星期三", "星期四", "星期五", "星期六", "星期日"][menu_date.weekday()]
    weekday_en = ["Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday", "Sunday"][menu_date.weekday()]
    month_en = ["January", "February", "March", "April", "May", "June", "July", "August", "September", "October", "November", "December"][menu_date.month - 1]
    status_cn, status_en = {
        "draft": ("待确认", "Pending"), "confirmed": ("已确认", "Confirmed"), "pushed": ("已推送", "Pushed")
    }.get(menu["status"], (menu["status"], menu["status"]))

    people_html = ""
    for diner in all_diners:
        selected = diner["id"] in menu_diners
        display_cn = "先生" if diner["id"] == "sir" else diner["name_cn"]
        display_en = "Sir" if diner["id"] == "sir" else diner["name_en"]
        initial = (display_en or display_cn or "?")[0].upper()
        repeated_name = display_en == display_cn
        name_html = display_cn if repeated_name else f'{display_cn}<small>{display_en}</small>'
        diner_action = f' onclick="toggleDiner(\'{diner["id"]}\')"' if is_editable else " disabled"
        people_html += (
            f'<button class="person {"selected" if selected else ""}" type="button" '
            f'data-diner="{diner["id"]}" aria-pressed="{str(selected).lower()}"{diner_action}>'
            f'<span class="person-avatar">{initial}</span><span class="person-name">{name_html}</span></button>'
        )

    nutrition_rows = "".join((
        f'<div class="nutrition-row"><span>{bilingual(label_cn, label_en)}</span>'
        f'<div class="meter" role="progressbar" aria-valuemin="0" aria-valuemax="100" aria-valuenow="{nutrition_values[key]}">'
        f'<i style="--progress:{nutrition_values[key]}%"></i></div><strong>{nutrition_values[key]}%</strong></div>'
        for key, label_cn, label_en in (
            ("protein", "蛋白质", "Protein"), ("vegetables", "蔬菜", "Vegetables"), ("staple", "主食", "Staple")
        )
    ))

    def meal_warning(meal_type, cn, en):
        missing = missing_by_meal.get(meal_type, {})
        if not missing:
            return ""
        cn_parts = [f'{SLOT_LABELS.get(slot, (slot, slot))[0]} {values["missing_min"]} 份' for slot, values in missing.items()]
        en_parts = [f'{values["missing_min"]} {SLOT_LABELS.get(slot, (slot, slot))[1]}' for slot, values in missing.items()]
        return (
            f'<div class="inline-warning"><strong>{cn}缺少 {"、".join(cn_parts)}</strong>'
            f'<span>{en} needs {", ".join(en_parts)}</span></div>'
        )

    meal_meta = {
        "breakfast": ("早餐", "Breakfast", "amber"), "lunch": ("午餐", "Lunch", "blue"),
        "afternoon_snack": ("下午茶", "Afternoon Tea", "green"), "dinner": ("晚餐", "Dinner", "red"),
    }
    meal_sections = []
    for meal_type in ("breakfast", "lunch", "afternoon_snack", "dinner"):
        dishes = valid_meals.get(meal_type, [])
        cn, en, color_class = meal_meta[meal_type]
        count_html = (
            bilingual("可选", "Optional")
            if meal_type == "afternoon_snack"
            else bilingual(f"{len(dishes)} 道", f"{len(dishes)} dishes")
        )
        if not is_editable:
            actions = ""
        elif meal_type == "afternoon_snack":
            actions = f'<button class="text-button" onclick="openDishSearch(\'{meal_type}\')">{bilingual("添加餐点", "Add item")}</button>'
        else:
            actions = (
                f'<button class="text-button" onclick="openDishSearch(\'{meal_type}\')">{bilingual("添加菜品", "Add dish")}</button>'
                f'<button class="text-button fill-button" onclick="aiFillMeal(\'{meal_type}\',this)">{bilingual("智能补充", "AI fill")}</button>'
            )
        cards = ""
        for dish in dishes:
            dish_name_cn = dish.get("name_cn") or dish.get("custom_name") or "未命名菜品"
            dish_name_en = dish.get("name_en") or ""
            if dish.get("image"):
                media = f'<img src="/photos/{dish["image"]}" alt="{dish_name_cn}" onerror="this.hidden=true;this.nextElementSibling.hidden=false"><div class="no-img" hidden>No image</div>'
            else:
                media = '<div class="no-img">No image</div>'
            dish_actions = ""
            if is_editable:
                dish_actions = (
                    f'<div class="dish-actions"><button class="smart-button" type="button" aria-label="智能换一道 {dish_name_cn}" title="智能换一道 Smart replace" '
                    f'onclick="smartReplace(\'{meal_type}\',{dish["menu_item_id"]},this)">智能换一道</button>'
                    f'<button class="search-button" type="button" aria-label="搜索更换 {dish_name_cn}" title="搜索更换 Search replace" '
                    f'onclick="openDishSearch(\'{meal_type}\',{dish["menu_item_id"]},\'{dish.get("dish_id", "")}\',\'{dish.get("category_id") or ""}\')">更换</button>'
                    f'<button class="remove-button" type="button" aria-label="删除 {dish_name_cn}" title="删除 Delete" '
                    f'onclick="removeDish({dish["menu_item_id"]})">删除</button></div>'
                )
            cards += (
                f'<article class="dish-card" data-item-id="{dish["menu_item_id"]}">{media}'
                f'<div class="dish-copy"><h3>{bilingual(dish_name_cn, dish_name_en)}</h3></div>'
                f'{dish_actions}</article>'
            )
        if meal_type == "afternoon_snack" and not dishes:
            empty_action = (
                f'<button class="text-button" onclick="openDishSearch(\'{meal_type}\')">{bilingual("添加", "Add")}</button>'
                if is_editable else ""
            )
            cards = (
                f'<div class="empty-state"><div class="empty-icon">+</div><div><strong>{bilingual("还没有安排下午茶", "No afternoon tea planned")}</strong>'
                f'<p>{bilingual("需要时可加入水果、酸奶或坚果。", "Add fruit, yoghurt or nuts when needed.")}</p></div>'
                f'{empty_action}</div>'
            )
        if not cards and meal_type != "afternoon_snack":
            cards = f'<div class="empty-state"><div><strong>{bilingual("该餐尚未安排菜品", "No dishes planned")}</strong></div></div>'
        warning = "" if meal_type == "afternoon_snack" else meal_warning(meal_type, cn, en)
        meal_sections.append(
            f'<section class="meal-section" data-meal="{meal_type}"><header class="meal-header">'
            f'<div class="meal-title"><span class="meal-accent {color_class}"></span><div><h2>{bilingual(cn, en)}</h2><p>{count_html}</p></div></div>'
            f'<div class="meal-actions">{actions}</div></header>{warning}<div class="dish-grid">{cards}</div></section>'
        )

    daily_class = "selected" if meal_mode == "daily" else ""
    banquet_class = "selected" if meal_mode == "banquet" else ""
    banquet_hidden = "" if meal_mode == "banquet" else "hidden"
    meal_mode_daily_action = " onclick=\"setMealMode('daily')\"" if is_editable else " disabled"
    meal_mode_banquet_action = " onclick=\"setMealMode('banquet')\"" if is_editable else " disabled"
    push_notice = ""
    if is_owner and menu["status"] != "draft":
        if menu.get("push_status") == "failed":
            push_notice = (
                f'<section class="inline-warning"><strong>{bilingual("菜单已确认，但推送失败", "Menu confirmed, but push failed")}</strong>'
                f'<span>{bilingual("可使用下方按钮重新推送。", "Use the button below to retry.")}</span></section>'
            )
        elif menu.get("push_status") == "success":
            push_notice = f'<section class="attention-banner"><div>{bilingual("菜单已确认并推送", "Menu confirmed and pushed")}</div></section>'
    owner_action_bar = ""
    desktop_action_bar = ""
    if is_owner:
        if menu["status"] == "draft":
            desktop_action_bar = (
                f'<div class="desktop-owner-actions"><button class="secondary-button" onclick="repairMenu()">{bilingual("重新生成菜单", "Regenerate menu")}</button>'
                f'<button class="primary-button" onclick="confirmMenu()">{bilingual("确认菜单", "Confirm menu")}</button></div>'
            )
            owner_action_bar = (
                f'<div class="mobile-action-bar"><button class="secondary-button" onclick="repairMenu()">{bilingual("重新生成", "Regenerate")}</button>'
                f'<button class="primary-button" onclick="confirmMenu()">{bilingual("确认菜单", "Confirm menu")}</button></div>'
            )
        elif menu.get("push_status") == "failed":
            desktop_action_bar = (
                f'<div class="desktop-owner-actions"><button class="secondary-button" onclick="editMenu()">{bilingual("修改菜单", "Edit menu")}</button>'
                f'<button class="primary-button" onclick="retryPush()">{bilingual("重新推送", "Retry push")}</button></div>'
            )
            owner_action_bar = (
                f'<div class="mobile-action-bar"><button class="secondary-button" onclick="editMenu()">{bilingual("修改菜单", "Edit menu")}</button>'
                f'<button class="primary-button" onclick="retryPush()">{bilingual("重新推送", "Retry push")}</button></div>'
            )
        else:
            desktop_action_bar = (
                f'<div class="desktop-owner-actions"><button class="secondary-button" onclick="editMenu()">{bilingual("修改菜单", "Edit menu")}</button>'
                f'<button class="primary-button" disabled>{bilingual("已确认", "Confirmed")}</button></div>'
            )
            owner_action_bar = (
                f'<div class="mobile-action-bar"><button class="secondary-button" onclick="editMenu()">{bilingual("修改菜单", "Edit menu")}</button>'
                f'<button class="primary-button" disabled>{bilingual("已确认", "Confirmed")}</button></div>'
            )
    body = f"""
<main id="main" class="page-shell">
<section class="page-heading"><div><p class="eyebrow">{bilingual(f'{menu_date.year}年{menu_date.month}月{menu_date.day}日 · {weekday_cn}', f'{weekday_en}, {month_en} {menu_date.day}, {menu_date.year}')}</p>
<h1>{bilingual('明日菜单','Tomorrow Menu')}</h1><p>{bilingual('为一家人安排营养均衡、好执行的一日餐食。','Plan a balanced, practical day of meals for the family.')}</p></div>
<div class="status-chip {'confirmed' if menu['status'] != 'draft' else ''}"><i></i>{bilingual(status_cn,status_en)}</div></section>
{push_notice}
<section class="settings-block diners-panel"><div class="section-label"><span>{bilingual('用餐成员','Diners')}</span><small>{bilingual(f'{len(menu_diners)} 人',f'{len(menu_diners)} people')}</small></div><div class="people-grid">{people_html}</div></section>
<div class="desktop-layout"><aside class="planner-panel">
<section class="settings-block compact"><div class="section-label"><span>{bilingual('用餐模式','Meal mode')}</span></div><div class="segmented">
<button class="{daily_class}"{meal_mode_daily_action}>{bilingual('日常','Daily')}</button><button class="{banquet_class}"{meal_mode_banquet_action}>{bilingual('家宴','Banquet')}</button></div>
<div class="banquet-count" {banquet_hidden}><span>{bilingual('家宴总人数','Total diners')}</span><div class="banquet-stepper">{f'<button type="button" onclick="adjustBanquet(-1)" aria-label="减少人数">−</button>' if is_editable else ''}<output id="banquetTotal">{banquet_total}</output>{f'<button type="button" onclick="adjustBanquet(1)" aria-label="增加人数">＋</button>' if is_editable else ''}</div></div></section>
<section class="nutrition-card"><div class="nutrition-heading"><div><small>{bilingual('营养概览','Nutrition overview')}</small><h2>{bilingual(nutrition_title_cn,nutrition_title_en)}</h2></div>
<span>{bilingual(f'{pending_count} 项待补',f'{pending_count} item{"s" if pending_count != 1 else ""} needed')}</span></div>{nutrition_rows}</section>{desktop_action_bar}</aside>
<div class="menu-content">{"".join(meal_sections)}</div></div></main>
{owner_action_bar}
"""
    if not is_owner:
        return tomorrow_preview_head("明日菜单 · Tomorrow Menu", "tomorrow", location) + body + "</body></html>"
    js = f"""<div class="modal-overlay" id="dishSearchModal" onclick="if(event.target===this)closeDishSearch()"><div class="modal dish-picker" role="dialog" aria-modal="true" aria-labelledby="dishSearchTitle">
<header class="dish-picker-head"><div><div id="dishSearchTitle" class="dish-picker-title">{bilingual('添加菜品','Add dish')}</div>
<p>{bilingual('系统根据库存和当前餐位推荐，也可搜索菜品','Recommendations based on pantry and meal')}</p></div>
<button class="picker-close" type="button" aria-label="关闭 / Close" onclick="closeDishSearch()">×</button></header>
<input class="dish-picker-search" id="dishSearchInput" placeholder="搜索菜品 / Search dishes" oninput="onDishSearchInput()">
<div class="dish-picker-results" id="dishSearchResults"></div><div class="modal-actions"><button class="secondary-button" onclick="closeDishSearch()">{bilingual('取消','Cancel')}</button></div></div></div>
<div class="snack-bar" id="snackBar"></div><script>
let menuId={menu['menu_id']},currentLoc='{location}',selectedDiners={json.dumps(menu_diners)},banquetTotal={banquet_total},searchMode={{meal:null,replaceId:null,currentDishId:null,categoryId:null}},searchTimer,searchRequestId=0,pickBusy=false;
function snack(msg){{let b=document.getElementById('snackBar');b.textContent=msg;b.classList.add('show');setTimeout(()=>b.classList.remove('show'),1800)}}
function pairMarkup(zh,en){{return '<span class="bilingual-pair"><span class="lang-zh">'+zh+'</span><span class="lang-en">'+en+'</span></span>'}}
async function requestJSON(path,options){{let response;try{{response=await fetch(path,options)}}catch(e){{throw new Error('网络连接失败 Network error')}}let data;try{{data=await response.json()}}catch(e){{throw new Error('服务器返回无效响应 Invalid server response')}}if(!response.ok||data.ok===false)throw new Error(data.error||data.message||('请求失败 HTTP '+response.status));return data}}
function postJSON(path,payload){{return requestJSON(path,{{method:'POST',headers:{{'Content-Type':'application/json'}},body:JSON.stringify(payload)}})}}
async function toggleDiner(id){{let next=selectedDiners.includes(id)?selectedDiners.filter(v=>v!==id):selectedDiners.concat(id);if(!next.length){{snack('至少保留一名用餐成员 Keep at least one diner');return}}try{{await postJSON('/api/tomorrow/diners',{{menu_id:menuId,diners:next,location:currentLoc}});location.reload()}}catch(e){{snack(e.message)}}}}
async function setMealMode(mode){{try{{await postJSON('/api/tomorrow/meal-mode',{{menu_id:menuId,meal_mode:mode,banquet_total_diners:mode==='banquet'?banquetTotal:null,location:currentLoc}});location.reload()}}catch(e){{snack(e.message)}}}}
async function adjustBanquet(delta){{let next=Math.max(2,Math.min(30,banquetTotal+delta));if(next===banquetTotal)return;try{{await postJSON('/api/tomorrow/meal-mode',{{menu_id:menuId,meal_mode:'banquet',banquet_total_diners:next,location:currentLoc}});banquetTotal=next;document.getElementById('banquetTotal').textContent=next;location.reload()}}catch(e){{snack(e.message)}}}}
function pickerStatus(message,isError=false){{let c=document.getElementById('dishSearchResults'),s=c.querySelector('.picker-status');if(!s){{s=document.createElement('div');s.className='picker-status';c.prepend(s)}}s.textContent=message;s.classList.toggle('inline-warning',isError)}}
function openDishSearch(meal,replaceId,currentDishId,categoryId){{searchMode={{meal,replaceId:replaceId||null,currentDishId:currentDishId||null,categoryId:categoryId||null}};document.getElementById('dishSearchModal').classList.add('show');document.getElementById('dishSearchTitle').innerHTML=replaceId?pairMarkup('搜索更换','Search replace'):pairMarkup('添加菜品','Add dish');document.getElementById('dishSearchInput').value='';loadDishPicker()}}
function closeDishSearch(){{clearTimeout(searchTimer);searchRequestId++;document.getElementById('dishSearchModal').classList.remove('show')}}
function onDishSearchInput(){{clearTimeout(searchTimer);searchRequestId++;let q=document.getElementById('dishSearchInput').value.trim();searchTimer=setTimeout(()=>q?doDishSearch(q):loadDishPicker(),300)}}
function recommendationResult(d,state){{let media=d.image?'<img loading="lazy" decoding="async" width="86" height="86" src="/photos/'+d.image+'" alt="'+d.name_cn+'" onerror="this.hidden=true;this.nextElementSibling.hidden=false"><span class="rec-no-img" hidden>No image</span>':'<span class="rec-no-img">No image</span>';let missing=d.missing_required&&d.missing_required.length?d.missing_required:[];let badge=state==='available'?pairMarkup('库存可做','Available now'):state==='almost'?pairMarkup('差少量','Almost available'):pairMarkup('缺少食材','Missing ingredients');let details=missing.length?'<small>'+missing.join('、')+'</small>':'';return '<button type="button" class="recommendation-item" data-dish-id="'+d.id+'" onclick="pickRecommendation(\\''+d.id+'\\','+(missing.length>0)+',this)">'+media+'<span class="recommendation-copy"><strong>'+pairMarkup(d.name_cn,d.name_en||'')+'</strong><em class="'+state+'">'+badge+'</em>'+details+'</span></button>'}}
async function loadDishPicker(){{let c=document.getElementById('dishSearchResults'),requestId=++searchRequestId;pickerStatus('推荐加载中… Loading recommendations…');try{{let results=await Promise.all([postJSON('/api/dishes/recommend',{{meal_type:searchMode.meal,current_dish_id:searchMode.currentDishId,category_id:searchMode.categoryId,location:currentLoc}}),requestJSON('/api/dishes')]);if(requestId!==searchRequestId)return;let rec=results[0],all=results[1].filter(d=>d.id!==searchMode.currentDishId),availability=all.length?await postJSON('/api/dishes/availability',{{dish_ids:all.map(d=>d.id),location:currentLoc}}):{{}};if(requestId!==searchRequestId)return;window.recommendationMap={{}};all.forEach(d=>{{let a=availability[d.id]||{{}};window.recommendationMap[d.id]=Object.assign({{}},d,a,{{missing_required:a.missing_names||[],missing_required_en:a.missing_names_en||[]}})}});let available=(rec.available||[]).filter(d=>d.id!==searchMode.currentDishId).map(d=>Object.assign({{}},window.recommendationMap[d.id]||d,d));let almost=(rec.almost_available||[]).filter(d=>d.id!==searchMode.currentDishId).map(d=>Object.assign({{}},window.recommendationMap[d.id]||d,d));let section=(zh,en,rows,state)=>'<div class="rec-section-title">'+pairMarkup(zh,en)+'</div>'+(rows.length?rows.map(d=>recommendationResult(d,state)).join(''):'<div style="padding:14px 18px;color:#65706a">暂无 None</div>');c.innerHTML=section('库存可做','Available now',available,'available')+section('差少量','Almost available',almost,'almost')+section('全部菜品','All dishes',all.slice(0,20),'all')}}catch(e){{if(requestId===searchRequestId)pickerStatus('推荐加载失败：'+e.message,true)}}}}
async function doDishSearch(q){{let c=document.getElementById('dishSearchResults'),requestId=++searchRequestId;pickerStatus('搜索中… Searching…');try{{let data=await requestJSON('/api/dishes?search='+encodeURIComponent(q));if(requestId!==searchRequestId)return;data=data.filter(d=>d.id!==searchMode.currentDishId).slice(0,20);let availability=data.length?await postJSON('/api/dishes/availability',{{dish_ids:data.map(d=>d.id),location:currentLoc}}):{{}};if(requestId!==searchRequestId)return;window.recommendationMap={{}};data.forEach(d=>{{let a=availability[d.id]||{{}};window.recommendationMap[d.id]=Object.assign({{}},d,a,{{missing_required:a.missing_names||[],missing_required_en:a.missing_names_en||[]}})}});c.innerHTML='<div class="rec-section-title">'+pairMarkup('搜索结果','Search results')+'</div>'+(data.length?data.map(d=>recommendationResult(window.recommendationMap[d.id],(availability[d.id]||{{}}).status==='available'?'available':(availability[d.id]||{{}}).status==='almost_available'?'almost':'all')).join(''):'<div class="empty-state">没有找到相关菜品 No matching dishes</div>')}}catch(e){{if(requestId===searchRequestId)pickerStatus('搜索失败：'+e.message,true)}}}}
function pickRecommendation(dishId,isMissing,button){{let d=window.recommendationMap&&window.recommendationMap[dishId];if(isMissing&&d&&d.missing_required&&d.missing_required.length&&!confirm('这道菜还缺：'+d.missing_required.join('、')+'\\n\\n仍然选择? Choose anyway?'))return;doPickDish(dishId,button)}}
async function refreshTomorrowFragments(meal,itemId){{let response=await fetch('/tomorrow',{{cache:'no-store'}});if(!response.ok)throw new Error('页面更新失败');let doc=new DOMParser().parseFromString(await response.text(),'text/html'),oldSection=document.querySelector('.meal-section[data-meal="'+meal+'"]'),newSection=doc.querySelector('.meal-section[data-meal="'+meal+'"]');if(!oldSection||!newSection)throw new Error('找不到餐次区域');let oldCard=oldSection.querySelector('[data-item-id="'+itemId+'"]'),newCard=newSection.querySelector('[data-item-id="'+itemId+'"]');if(oldCard&&newCard)oldCard.replaceWith(newCard);let oldCount=oldSection.querySelector('.meal-title p'),newCount=newSection.querySelector('.meal-title p');if(oldCount&&newCount)oldCount.replaceWith(newCount);let oldWarning=oldSection.querySelector('.inline-warning,.slot-hint'),newWarning=newSection.querySelector('.inline-warning,.slot-hint');if(oldWarning&&newWarning)oldWarning.replaceWith(newWarning);else if(oldWarning)oldWarning.remove();else if(newWarning)oldSection.querySelector('.meal-header').insertAdjacentElement('afterend',newWarning);let oldNutrition=document.querySelector('.nutrition-card,.nutrition-overview'),newNutrition=doc.querySelector('.nutrition-card,.nutrition-overview');if(oldNutrition&&newNutrition)oldNutrition.replaceWith(newNutrition)}}
async function doPickDish(dishId,button){{if(pickBusy)return;let path=searchMode.replaceId?'/api/tomorrow/replace':'/api/tomorrow/add',payload=searchMode.replaceId?{{menu_id:menuId,menu_item_id:searchMode.replaceId,new_dish_id:dishId}}:{{menu_id:menuId,dish_id:dishId,meal_type:searchMode.meal}},original=button?button.innerHTML:'';pickBusy=true;if(button){{button.disabled=true;button.innerHTML='<span class="recommendation-copy"><strong>处理中… Processing…</strong></span>'}}try{{await postJSON(path,payload);if(searchMode.replaceId)await refreshTomorrowFragments(searchMode.meal,searchMode.replaceId);closeDishSearch();snack(searchMode.replaceId?'已替换 Replaced':'已添加 Added')}}catch(e){{snack(e.message);pickerStatus('操作失败：'+e.message,true);if(button){{button.disabled=false;button.innerHTML=original}}}}finally{{pickBusy=false}}}}
async function smartReplace(meal,itemId,button){{if(button.disabled)return;button.disabled=true;try{{await postJSON('/api/tomorrow/smart-replace',{{menu_id:menuId,menu_item_id:itemId,location:currentLoc}});await refreshTomorrowFragments(meal,itemId);snack('已智能更换 Smart replaced')}}catch(e){{snack(e.message);button.disabled=false}}}}
async function removeDish(itemId){{if(!confirm('确认删除? Confirm delete?'))return;let result=await fetch('/api/tomorrow/remove',{{method:'POST',headers:{{'Content-Type':'application/json'}},body:JSON.stringify({{menu_id:menuId,menu_item_id:itemId}})}}).then(r=>r.json());result.ok?location.reload():snack(result.error||'删除失败')}}
async function aiFillMeal(meal,button){{try{{if(button){{button.disabled=true;button.setAttribute('aria-busy','true');button.textContent='补充中 Filling...'}}await postJSON('/api/tomorrow/ai-fill',{{menu_id:menuId,location:currentLoc,meal_type:meal}});location.reload()}}catch(e){{snack(e.message);if(button){{button.disabled=false;button.removeAttribute('aria-busy');button.textContent='智能补充 AI fill'}}}}}}
async function repairMenu(){{if(!confirm('重新生成菜单? Regenerate menu?'))return;try{{await postJSON('/api/tomorrow/repair',{{menu_id:menuId,location:currentLoc}});location.reload()}}catch(e){{snack(e.message)}}}}
async function confirmMenu(){{try{{let result=await postJSON('/api/tomorrow/confirm',{{menu_id:menuId}});snack(result.message||'已确认 Confirmed');setTimeout(()=>location.reload(),1200)}}catch(e){{snack(e.message)}}}}
async function retryPush(){{let result=await fetch('/api/tomorrow/push',{{method:'POST',headers:{{'Content-Type':'application/json'}},body:JSON.stringify({{menu_id:menuId}})}}).then(r=>r.json());snack(result.ok?'重新推送成功 Push sent':(result.error||'重新推送失败'));if(result.ok)setTimeout(()=>location.reload(),1200)}}
async function editMenu(){{if(!confirm('修改菜单将回退到草稿状态，修改后需重新确认。 Edit and reconfirm?'))return;let result=await fetch('/api/tomorrow/revert',{{method:'POST',headers:{{'Content-Type':'application/json'}},body:JSON.stringify({{menu_id:menuId}})}}).then(r=>r.json());result.ok?location.reload():snack(result.error||'操作失败')}}
</script></body></html>"""
    return tomorrow_preview_head("明日菜单 · Tomorrow Menu", "tomorrow", location) + body + js


def render_meal_plan_reference(role="owner", location="shenzhen"):
    """Existing reference UI extended to show today's dinner and tomorrow's plan."""
    is_owner = role == "owner"
    today_str = date.today().isoformat()
    tomorrow_str = get_tomorrow_date()
    if is_owner:
        ensure_tomorrow_menu(location)
    today_menu = get_menu_with_dishes(today_str)
    tomorrow_menu = get_menu_with_dishes(tomorrow_str)
    all_diners = get_all_diners()
    default_ids = [d["id"] for d in all_diners if d["default_attends"]]

    def menu_defaults(menu):
        if not menu.get("menu_id"):
            return default_ids
        return get_menu_diners(menu["menu_id"]) or default_ids

    tomorrow_defaults = menu_defaults(tomorrow_menu)
    menus = [menu for menu in (today_menu, tomorrow_menu) if menu.get("menu_id")]
    settings_by_menu = {menu["menu_id"]: get_meal_settings(menu["menu_id"]) for menu in menus}
    effective_by_key = {}
    validation_by_menu = {}
    for menu in menus:
        # The single top selector is the inherited default for every visible meal.
        defaults = tomorrow_defaults
        settings = settings_by_menu[menu["menu_id"]]
        counts = {}
        skipped = []
        for meal_type in MEAL_TYPES:
            setting = settings.get(meal_type, {})
            diners = setting.get("diners")
            effective = defaults if diners is None else diners
            effective_by_key[f'{menu["menu_id"]}:{meal_type}'] = {
                "default": defaults, "custom": diners, "effective": effective,
                "note": setting.get("note", ""), "is_skipped": bool(setting.get("is_skipped")),
            }
            counts[meal_type] = max(len(effective), 1)
            if setting.get("is_skipped"):
                skipped.append(meal_type)
        validation_by_menu[menu["menu_id"]] = validate_menu_meals(
            menu, max(len(defaults), 1), meal_diners_counts=counts, skipped_meals=skipped
        )

    meal_meta = {
        "breakfast": ("早餐", "Breakfast", "amber"), "lunch": ("午餐", "Lunch", "blue"),
        "afternoon_snack": ("下午茶", "Afternoon Tea", "green"), "dinner": ("晚餐", "Dinner", "red"),
    }

    def meal_section(menu, meal_type):
        menu_id = menu.get("menu_id")
        cn, en, color_class = meal_meta[meal_type]
        if not menu_id:
            return f'<section class="meal-section"><div class="add-meal-card">＋ {bilingual(f"添加{cn}", f"Add {en.lower()}")}</div></section>'
        key = f"{menu_id}:{meal_type}"
        setting = effective_by_key[key]
        dishes = [d for d in menu.get("meals", {}).get(meal_type, []) if not d.get("is_historical_combo")]
        editable = (
            is_owner
            and menu_id == tomorrow_menu.get("menu_id")
            and menu.get("status") in ("draft", "confirmed")
        )
        diner_label = bilingual(f'{len(setting["effective"])}人用餐', f'{len(setting["effective"])} diners ›')
        if is_owner:
            note_label = bilingual("修改备注", "Edit note") if setting["note"] else bilingual("添加备注", "Add note")
        else:
            note_label = bilingual("查看备注", "View note") if setting["note"] else ""
        note_button = (
            f'<button class="meal-note-button" onclick="openMealNote({menu_id},\'{meal_type}\')">{note_label}</button>'
            if note_label else ""
        )
        links = (
            f'<div class="meal-links"><button class="meal-link" onclick="openMealDiners({menu_id},\'{meal_type}\')">{diner_label}</button>'
            f'{note_button}</div>'
        )
        skip_button = (
            f'<button class="meal-skip-button" type="button" aria-label="本餐不安排 Skip this meal" title="本餐不安排 Skip this meal" onclick="skipMeal({menu_id},\'{meal_type}\')">×</button>'
            if editable else ""
        )
        header = (
            f'<header class="meal-header"><div class="meal-title"><span class="meal-accent {color_class}"></span>'
            f'<div><h2>{bilingual(cn,en)}</h2><p>{bilingual(f"{len(dishes)} 道", f"{len(dishes)} dishes")}</p>{links}</div></div>'
        )
        restore_button = (
            f'<button class="restore-meal-button" onclick="restoreMeal({menu_id},\'{meal_type}\')">'
            f'{bilingual("恢复本餐", "Restore meal")}</button>' if editable else ""
        )
        if setting["is_skipped"]:
            return (
                f'<section class="meal-section" data-menu-id="{menu_id}" data-meal="{meal_type}">{header}'
                f'<div class="meal-actions"></div></header><div class="skipped-meal">'
                f'<span>{bilingual("本餐不在家用餐", "Dining out for this meal")}</span>'
                f'{restore_button}</div></section>'
            )
        actions = ""
        if editable:
            actions = (
                f'<button class="text-button" onclick="openDishSearch({menu_id},\'{meal_type}\')">{bilingual("添加菜品", "Add dish")}</button>'
                + ("" if meal_type == "afternoon_snack" else
                   f'<button class="text-button fill-button" onclick="aiFillMeal({menu_id},\'{meal_type}\',this)">{bilingual("智能补充", "AI fill")}</button>')
                + skip_button
            )
        note_html = f'<div class="meal-note">备注：{escape(setting["note"])}</div>' if setting["note"] else ""
        warning = ""
        if meal_type != "afternoon_snack":
            missing = validation_by_menu[menu_id].get("missing_by_meal", {}).get(meal_type, {})
            if missing:
                cn_parts = [f'{SLOT_LABELS.get(slot,(slot,slot))[0]} {value["missing_min"]} 份' for slot, value in missing.items()]
                en_parts = [f'{value["missing_min"]} {SLOT_LABELS.get(slot,(slot,slot))[1]}' for slot, value in missing.items()]
                warning = f'<div class="inline-warning"><strong>{cn}缺少 {"、".join(cn_parts)}</strong><span>{en} needs {", ".join(en_parts)}</span></div>'
        cards = ""
        for dish in dishes:
            dish_cn = escape(dish.get("name_cn") or dish.get("custom_name") or "未命名菜品")
            dish_en = escape(dish.get("name_en") or "")
            image_html = (
                f'<img src="/photos/{quote(dish["image"])}" alt="{dish_cn}" loading="lazy" onerror="this.hidden=true;this.nextElementSibling.hidden=false"><div class="no-img" hidden>No image</div>'
                if dish.get("image") else '<div class="no-img">No image</div>'
            )
            card_actions = ""
            if editable:
                card_actions = (
                    f'<div class="dish-actions"><button class="smart-button" type="button" title="智能换一道 Smart replace" onclick="smartReplace({menu_id},\'{meal_type}\',{dish["menu_item_id"]},this)">智能换一道</button>'
                    f'<button class="search-button" type="button" title="搜索更换 Search replace" onclick="openDishSearch({menu_id},\'{meal_type}\',{dish["menu_item_id"]},\'{dish.get("dish_id","")}\',\'{dish.get("category_id") or ""}\')">更换</button>'
                    f'<button class="remove-button" type="button" title="删除 Delete" onclick="removeDish({menu_id},{dish["menu_item_id"]})">删除</button></div>'
                )
            cards += f'<article class="dish-card" data-item-id="{dish["menu_item_id"]}">{image_html}<div class="dish-copy"><h3>{bilingual(dish_cn,dish_en)}</h3></div>{card_actions}</article>'
        if not cards:
            add_action = f' onclick="openDishSearch({menu_id},\'{meal_type}\')"' if editable else " disabled"
            cards = f'<button class="add-meal-card" type="button"{add_action}>＋ {bilingual(f"添加{cn}", f"Add {en.lower()}")}</button>'
        return (
            f'<section class="meal-section" data-menu-id="{menu_id}" data-meal="{meal_type}">{header}'
            f'<div class="meal-actions">{actions}</div></header>{note_html}{warning}<div class="dish-grid">{cards}</div></section>'
        )

    def date_heading(label_cn, label_en, date_str):
        value = datetime.strptime(date_str, "%Y-%m-%d")
        return f'<div class="date-band"><div><h2>{bilingual(label_cn,label_en)}</h2><p>{value.year}年{value.month}月{value.day}日 · {value.strftime("%A")}</p></div></div>'

    people_html = ""
    for diner in all_diners:
        selected = diner["id"] in tomorrow_defaults
        name_cn = "先生" if diner["id"] == "sir" else diner["name_cn"]
        name_en = "Sir" if diner["id"] == "sir" else diner["name_en"]
        action = (
            f' onclick="toggleDiner(\'{diner["id"]}\')"'
            if is_owner and tomorrow_menu.get("status") in ("draft", "confirmed")
            else " disabled"
        )
        people_html += f'<button class="person {"selected" if selected else ""}" type="button"{action}><span class="person-avatar">{escape((name_en or name_cn or "?")[0].upper())}</span><span class="person-name">{bilingual(escape(name_cn),escape(name_en or ""))}</span></button>'

    nutrition_html = ""
    pending_count = 0
    if tomorrow_menu.get("menu_id"):
        missing = validation_by_menu[tomorrow_menu["menu_id"]].get("missing_by_meal", {})
        pending_count = len(independent_missing_issue_keys(missing))
        nutrition_html = f'<section class="nutrition-card"><div class="nutrition-heading"><div><small>{bilingual("营养概览","Nutrition overview")}</small><h2>{bilingual("明日餐单搭配","Tomorrow balance")}</h2></div><span>{bilingual(f"{pending_count} 项待补",f"{pending_count} items needed")}</span></div></section>'
    tomorrow_editable = is_owner and tomorrow_menu.get("status") == "draft"
    action_bar = ""
    desktop_actions = ""
    if is_owner and tomorrow_menu.get("menu_id"):
        if tomorrow_editable:
            action_bar = f'<div class="mobile-action-bar"><button class="secondary-button" onclick="repairMenu()">{bilingual("重新生成","Regenerate")}</button><button class="primary-button" onclick="confirmMenu()">{bilingual("确认菜单","Confirm menu")}</button></div>'
            desktop_actions = f'<div class="desktop-owner-actions"><button class="secondary-button" onclick="repairMenu()">{bilingual("重新生成","Regenerate")}</button><button class="primary-button" onclick="confirmMenu()">{bilingual("确认菜单","Confirm menu")}</button></div>'
        else:
            action_bar = f'<div class="mobile-action-bar"><button class="secondary-button" onclick="editMenu()">{bilingual("修改菜单","Edit menu")}</button><button class="primary-button" disabled>{bilingual("已确认","Confirmed")}</button></div>'
            desktop_actions = f'<div class="desktop-owner-actions"><button class="secondary-button" onclick="editMenu()">{bilingual("修改菜单","Edit menu")}</button><button class="primary-button" disabled>{bilingual("已确认","Confirmed")}</button></div>'

    today_sections = meal_section(today_menu, "dinner")
    tomorrow_sections = "".join(meal_section(tomorrow_menu, mt) for mt in MEAL_TYPES)
    body = f"""<main id="main" class="page-shell"><section class="page-heading"><div><p class="eyebrow">{bilingual('按真实日期安排','Plan by actual date')}</p><h1>{bilingual('餐单','Meal Plan')}</h1></div></section>
<section class="settings-block diners-panel"><div class="section-label"><span>{bilingual('全日默认成员','All-day default diners')}</span><small>{bilingual(f'{len(tomorrow_defaults)} 人',f'{len(tomorrow_defaults)} people')}</small></div><div class="people-grid">{people_html}</div></section>
<div class="desktop-layout"><aside class="planner-panel">{nutrition_html}{desktop_actions}</aside><div class="menu-content">{date_heading('今天','Today',today_str)}{today_sections}{date_heading('明天','Tomorrow',tomorrow_str)}{tomorrow_sections}</div></div></main>{action_bar}"""

    settings_json = json.dumps(effective_by_key, ensure_ascii=False)
    diners_json = json.dumps([{"id": d["id"], "cn": d["name_cn"], "en": d["name_en"] or ""} for d in all_diners], ensure_ascii=False)
    owner_js = "true" if is_owner else "false"
    tomorrow_id = tomorrow_menu.get("menu_id") or "null"
    js = f"""
<div class="modal-overlay" id="mealSheet" onclick="if(event.target===this)closeMealSheet()"><div class="modal meal-bottom-sheet"><h2 id="mealSheetTitle"></h2><div id="mealSheetBody"></div></div></div>
<div class="modal-overlay" id="dishSearchModal" onclick="if(event.target===this)closeDishSearch()"><div class="modal dish-picker"><header class="dish-picker-head"><div><div id="dishSearchTitle" class="dish-picker-title">{bilingual('添加菜品','Add dish')}</div></div><button class="picker-close" onclick="closeDishSearch()">×</button></header><input class="dish-picker-search" id="dishSearchInput" placeholder="搜索菜品 / Search dishes" oninput="onDishSearchInput()"><div class="dish-picker-results" id="dishSearchResults"></div></div></div>
<div class="snack-bar" id="snackBar"></div><script>
let menuId={tomorrow_id},currentLoc='{location}',selectedDiners={json.dumps(tomorrow_defaults)},isOwner={owner_js},mealSettings={settings_json},allDiners={diners_json},sheetContext=null,searchMode={{}},searchTimer,searchRequestId=0,pickBusy=false;
function snack(m){{let b=document.getElementById('snackBar');b.textContent=m;b.classList.add('show');setTimeout(()=>b.classList.remove('show'),1800)}}
async function requestJSON(path,options){{let r=await fetch(path,options),d;try{{d=await r.json()}}catch(e){{throw Error('服务器返回无效响应')}}if(!r.ok||d.ok===false)throw Error(d.error||d.message||('HTTP '+r.status));return d}}
function postJSON(path,payload){{return requestJSON(path,{{method:'POST',headers:{{'Content-Type':'application/json'}},body:JSON.stringify(payload)}})}}
function key(mid,meal){{return mid+':'+meal}} function closeMealSheet(){{document.getElementById('mealSheet').classList.remove('show')}}
function openMealDiners(mid,meal){{sheetContext={{mid,meal}};let s=mealSettings[key(mid,meal)],chosen=s.custom===null?s.effective:s.custom;document.getElementById('mealSheetTitle').textContent='用餐成员 / Diners';let buttons=allDiners.map(d=>'<button type="button" data-id="'+d.id+'" class="'+(chosen.includes(d.id)?'selected':'')+'" onclick="this.classList.toggle(&quot;selected&quot;)">'+d.cn+' / '+d.en+'</button>').join('');document.getElementById('mealSheetBody').innerHTML='<div class="sheet-diners">'+buttons+'</div>'+(isOwner?'<div class="sheet-footer"><button onclick="inheritMealDiners()">沿用默认成员<br><small>Use default</small></button><button class="sheet-save" onclick="saveMealDiners()">保存 / Save</button></div>':'');document.getElementById('mealSheet').classList.add('show')}}
async function saveMealDiners(){{let ids=[...document.querySelectorAll('#mealSheetBody .sheet-diners button.selected')].map(b=>b.dataset.id);if(!ids.length)return snack('至少选择一人');await postJSON('/api/meal-plan/meal-diners',{{menu_id:sheetContext.mid,meal_type:sheetContext.meal,diners:ids}});location.reload()}}
async function inheritMealDiners(){{await postJSON('/api/meal-plan/meal-diners',{{menu_id:sheetContext.mid,meal_type:sheetContext.meal,inherit:true}});location.reload()}}
function openMealNote(mid,meal){{sheetContext={{mid,meal}};let note=mealSettings[key(mid,meal)].note||'';document.getElementById('mealSheetTitle').textContent='餐次备注 / Meal note';document.getElementById('mealSheetBody').innerHTML=isOwner?'<textarea class="sheet-note" id="mealNote" maxlength="500">'+note.replace(/&/g,'&amp;').replace(/</g,'&lt;')+'</textarea><div class="sheet-footer"><button onclick="closeMealSheet()">取消 / Cancel</button><button class="sheet-save" onclick="saveMealNote()">保存 / Save</button></div>':'<p>'+note+'</p>';document.getElementById('mealSheet').classList.add('show')}}
async function saveMealNote(){{await postJSON('/api/meal-plan/note',{{menu_id:sheetContext.mid,meal_type:sheetContext.meal,note:document.getElementById('mealNote').value}});location.reload()}}
async function skipMeal(mid,meal){{if(!confirm('本餐不在家用餐？可随时恢复。 / Skip this meal?'))return;await postJSON('/api/meal-plan/meal-state',{{menu_id:mid,meal_type:meal,is_skipped:true}});location.reload()}}
async function restoreMeal(mid,meal){{await postJSON('/api/meal-plan/meal-state',{{menu_id:mid,meal_type:meal,is_skipped:false}});location.reload()}}
async function toggleDiner(id){{let next=selectedDiners.includes(id)?selectedDiners.filter(v=>v!==id):selectedDiners.concat(id);if(!next.length)return snack('至少选择一人');await postJSON('/api/tomorrow/diners',{{menu_id:menuId,diners:next,location:currentLoc}});location.reload()}}
function openDishSearch(mid,meal,replaceId,currentDishId,categoryId){{searchMode={{menuId:mid,meal,replaceId:replaceId||null,currentDishId:currentDishId||null,categoryId:categoryId||null}};document.getElementById('dishSearchModal').classList.add('show');document.getElementById('dishSearchInput').value='';loadDishPicker()}} function closeDishSearch(){{searchRequestId++;document.getElementById('dishSearchModal').classList.remove('show')}} function onDishSearchInput(){{clearTimeout(searchTimer);let q=document.getElementById('dishSearchInput').value.trim();searchTimer=setTimeout(()=>q?doDishSearch(q):loadDishPicker(),300)}}
function resultRow(d,state){{return '<button class="recommendation-item" onclick="doPickDish(&quot;'+d.id+'&quot;,this)">'+(d.image?'<img loading="lazy" decoding="async" src="/photos/'+d.image+'">':'<span class="rec-no-img">No image</span>')+'<span class="recommendation-copy"><strong>'+d.name_cn+'<small>'+((d.name_en)||'')+'</small></strong><em class="'+state+'">'+(state==='available'?'Available now':state==='almost'?'Almost available':'All dishes')+'</em></span></button>'}}
async function loadDishPicker(){{let id=++searchRequestId,c=document.getElementById('dishSearchResults');try{{let rec=await postJSON('/api/dishes/recommend',{{meal_type:searchMode.meal,current_dish_id:searchMode.currentDishId,category_id:searchMode.categoryId,location:currentLoc}});if(id!==searchRequestId)return;let available=(rec.available||[]).filter(x=>x.id!==searchMode.currentDishId),almost=(rec.almost_available||[]).filter(x=>x.id!==searchMode.currentDishId);c.innerHTML='<div class="rec-section-title">库存可做 / Available now</div>'+available.map(d=>resultRow(d,'available')).join('')+'<div class="rec-section-title">差少量 / Almost available</div>'+almost.map(d=>resultRow(d,'almost')).join('')}}catch(e){{if(id===searchRequestId)c.insertAdjacentHTML('afterbegin','<div class="inline-warning">推荐加载失败：'+e.message+'</div>')}}}}
async function doDishSearch(q){{let id=++searchRequestId,c=document.getElementById('dishSearchResults');try{{let d=await requestJSON('/api/dishes?search='+encodeURIComponent(q));if(id!==searchRequestId)return;d=d.filter(x=>x.id!==searchMode.currentDishId).slice(0,20);let a=d.length?await postJSON('/api/dishes/availability',{{dish_ids:d.map(x=>x.id),location:currentLoc}}):{{}};if(id!==searchRequestId)return;c.innerHTML=d.map(x=>resultRow(x,(a[x.id]||{{}}).status==='available'?'available':(a[x.id]||{{}}).status==='almost_available'?'almost':'all')).join('')||'<div class="empty-state">没有结果 / No results</div>'}}catch(e){{if(id===searchRequestId)c.insertAdjacentHTML('afterbegin','<div class="inline-warning">搜索失败：'+e.message+'</div>')}}}}
async function doPickDish(did,b){{if(pickBusy)return;pickBusy=true;b.disabled=true;b.textContent='处理中… Processing…';try{{await postJSON(searchMode.replaceId?'/api/tomorrow/replace':'/api/tomorrow/add',searchMode.replaceId?{{menu_id:searchMode.menuId,menu_item_id:searchMode.replaceId,new_dish_id:did}}:{{menu_id:searchMode.menuId,dish_id:did,meal_type:searchMode.meal}});location.reload()}}catch(e){{snack(e.message);b.disabled=false}}finally{{pickBusy=false}}}}
async function smartReplace(mid,meal,item,b){{b.disabled=true;try{{await postJSON('/api/tomorrow/smart-replace',{{menu_id:mid,menu_item_id:item,location:currentLoc}});location.reload()}}catch(e){{snack(e.message);b.disabled=false}}}} async function removeDish(mid,item){{if(!confirm('确认删除?'))return;await postJSON('/api/tomorrow/remove',{{menu_id:mid,menu_item_id:item}});location.reload()}} async function aiFillMeal(mid,meal,b){{b.disabled=true;try{{await postJSON('/api/tomorrow/ai-fill',{{menu_id:mid,location:currentLoc,meal_type:meal}});location.reload()}}catch(e){{snack(e.message);b.disabled=false}}}}
async function repairMenu(){{if(confirm('重新生成明日菜单?')){{await postJSON('/api/tomorrow/repair',{{menu_id:menuId,location:currentLoc}});location.reload()}}}} async function confirmMenu(){{await postJSON('/api/tomorrow/confirm',{{menu_id:menuId}});location.reload()}} async function editMenu(){{await postJSON('/api/tomorrow/revert',{{menu_id:menuId}});location.reload()}}
</script></body></html>"""
    return tomorrow_preview_head("餐单 · Meal Plan", "tomorrow", location) + body + js

def render_tomorrow(role="owner", location="shenzhen"):
    if os.environ.get("LOCAL_PREVIEW_UI", "").lower() == "true":
        return render_meal_plan_reference(role, location)
    tomorrow = get_tomorrow_date()
    # Viewing as Worker must never create or regenerate menu data.
    if role == "owner":
        ensure_tomorrow_menu(location)
    menu = get_menu_with_dishes(tomorrow)
    is_editable = role == "owner" and menu.get("status") == "draft"

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

    # V11: 获取 Meal Mode
    meal_mode_info = {"meal_mode": "daily", "banquet_total_diners": None}
    if menu.get("menu_id"):
        meal_mode_info = get_menu_meal_mode(menu["menu_id"])
    meal_mode = meal_mode_info["meal_mode"]
    banquet_total = meal_mode_info["banquet_total_diners"] or 8

    # One shared rule-engine result drives meal gaps and the overview.
    menu_warnings = []
    menu_validation = {"meal_slots": {}, "warnings": []}
    if menu.get("exists") and menu.get("menu_id"):
        try:
            effective_diners = banquet_total if meal_mode == "banquet" else len(menu_diners)
            menu_validation = validate_menu_meals(menu, effective_diners)
            menu_warnings = menu_validation["warnings"]
        except Exception:
            pass

    sections = []

    if not menu.get("exists"):
        sections.append(f'<div class="empty"><h2>明日菜单未生成</h2><p>系统将自动生成</p></div>')
    else:
        # 页面标题使用与设计稿一致的中英文长日期。
        menu_date = datetime.strptime(menu["date"], "%Y-%m-%d")
        weekday_cn = ["星期一", "星期二", "星期三", "星期四", "星期五", "星期六", "星期日"][menu_date.weekday()]
        weekday_en = ["Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday", "Sunday"][menu_date.weekday()]
        month_en = ["January", "February", "March", "April", "May", "June", "July", "August", "September", "October", "November", "December"][menu_date.month - 1]
        status_cls = f"status-{menu['status']}"
        status_text = {"draft": "待确认 Pending", "confirmed": "已确认 Confirmed", "pushed": "已推送 Pushed"}.get(menu["status"], menu["status"])
        sections.append(
            f'<section class="tomorrow-heading">'
            f'<div><div class="tomorrow-date">{menu_date.year}年{menu_date.month}月{menu_date.day}日 · {weekday_cn}'
            f'<span>{weekday_en}, {month_en} {menu_date.day}, {menu_date.year}</span></div>'
            f'<h2>明日菜单</h2><p>为一家人安排营养均衡、好执行的一日餐食。</p></div>'
            f'<span class="status-tag {status_cls}">{status_text}</span>'
            f'</section>'
        )

        if is_editable:
            # Owner-only menu configuration controls.
            diners_chips = ""
            for d in all_diners:
                active = "active" if d["id"] in menu_diners else ""
                display_cn = "老板" if d["id"] == "sir" else d["name_cn"]
                show_en = d["name_en"] if d["name_en"] and d["name_en"] != display_cn else ""
                initial = (d["name_en"] or display_cn or "?")[0].upper()
                diner_en_html = f'<span class="diner-en">{show_en}</span>' if show_en else ""
                diners_chips += (
                    f'<button type="button" class="diner-chip person-card {active}" data-diner="{d["id"]}" '
                    f'aria-pressed="{str(bool(active)).lower()}" onclick="toggleDiner(\'{d["id"]}\')">'
                    f'<span class="diner-avatar" aria-hidden="true">{initial}</span>'
                    f'<span class="diner-name">{display_cn}</span>'
                    f'{diner_en_html}</button>'
                )
            diner_total = len(menu_diners)
            sections.append(
                f'<section class="diners-section"><div class="diners-title"><span>用餐成员<small>Diners</small></span>'
                f'<span class="diner-count">{diner_total} 人<small>{diner_total} people</small></span></div>'
                f'<div class="diners-row people-cards">{diners_chips}</div></section>'
            )

            daily_active = "active" if meal_mode == "daily" else ""
            banquet_active = "active" if meal_mode == "banquet" else ""
            banquet_input_display = "block" if meal_mode == "banquet" else "none"
            meal_mode_html = (
                f'<div class="diners-section">'
                f'<div class="diners-title">用餐模式 Meal Mode</div>'
                f'<div class="diners-row">'
                f'<span class="diner-chip {daily_active}" onclick="setMealMode(\'daily\')">日常 Daily</span>'
                f'<span class="diner-chip {banquet_active}" onclick="setMealMode(\'banquet\')">家宴 Banquet</span>'
                f'</div>'
                f'<div id="banquet-input" style="display:{banquet_input_display};margin-top:8px;align-items:center;gap:10px">'
                f'<span style="font-size:14px;color:#a89888">家宴总人数 Total Diners</span>'
                f'<button class="btn-stepper" onclick="adjustBanquet(-1)">−</button>'
                f'<input type="number" id="banquetTotal" value="{banquet_total}" min="2" max="30" '
                f'style="width:60px;text-align:center;font-size:18px;border:1px solid #d4c8b8;border-radius:6px;padding:4px" '
                f'onchange="setBanquetTotal(this.value)">'
                f'<button class="btn-stepper" onclick="adjustBanquet(1)">+</button>'
                f'</div>'
                f'</div>'
            )
            sections.append(meal_mode_html)

        # Aggregate the exact same configured slots shown in each meal warning.
        nutrition_slot_groups = {
            "蛋白质": {"protein_main"},
            "蔬菜": {"vegetable", "vegetable_dish"},
            "主食": {"porridge", "companion_staple", "coarse_grain", "staple"},
            "汤羹": {"quick_soup", "slow_soup"},
        }
        nutrition_values = {}
        for label, slot_names in nutrition_slot_groups.items():
            current_total = 0
            target_total = 0
            for slots in menu_validation["meal_slots"].values():
                for slot_name, slot_value in slots.items():
                    if slot_name in slot_names:
                        target_total += slot_value["target_min"]
                        current_total += min(slot_value["current"], slot_value["target_min"])
            nutrition_values[label] = 100 if target_total == 0 else min(100, round(current_total / target_total * 100))
        nutrition_rows = "".join(
            f'<div class="nutrition-metric"><span>{label}</span><div class="nutrition-track" '
            f'role="progressbar" aria-label="{label}" aria-valuemin="0" aria-valuemax="100" aria-valuenow="{value}">'
            f'<i style="--value:{value}%"></i></div><strong>{value}%</strong></div>'
            for label, value in nutrition_values.items()
        )
        pending_nutrition = sum(1 for value in nutrition_values.values() if value < 100)
        nutrition_title = "今日搭配已均衡" if pending_nutrition == 0 else "还需要补充营养搭配"
        sections.append(
            f'<section class="nutrition-overview" aria-labelledby="nutrition-title">'
            f'<div class="nutrition-overview-head"><h3 id="nutrition-title"><span>营养概览 Nutrition</span>{nutrition_title}</h3>'
            f'<span>{pending_nutrition} 项待补</span></div>{nutrition_rows}'
            f'<p class="nutrition-note">进度与早餐、午餐、晚餐的现有规则校验结果同步。</p></section>'
        )

        if role == "owner" and menu["status"] == "draft":
            sections.append(
                '<div class="desktop-confirm">'
                '<button class="btn btn-outline" onclick="repairMenu()">重新生成菜单<small>Regenerate menu</small></button>'
                '<button class="btn btn-primary" onclick="confirmMenu()">确认菜单<small>Confirm menu</small></button>'
                '</div>'
            )

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
            color, cn, en = meal_colors[mt]

            # V8/V9: 计算缺失槽位提示
            slot_hint = ""
            if mt in ("breakfast", "lunch", "dinner") and menu.get("exists"):
                slots = menu_validation["meal_slots"].get(mt, {})
                missing = {key: value for key, value in slots.items() if value["missing_min"] > 0}
                if missing:
                    meal_missing = "、".join(
                        f'{SLOT_LABELS.get(slot, (slot, slot))[0]}{value["missing_min"]}份'
                        for slot, value in missing.items()
                    )
                    meal_missing_en = ", ".join(
                        f'{SLOT_LABELS.get(slot, (slot, slot))[1]} {value["missing_min"]}'
                        for slot, value in missing.items()
                    )
                    slot_hint = (
                        f'<div class="slot-hint"><strong>{cn}缺少：{meal_missing}</strong><br>'
                        f'{en} needs: {meal_missing_en}</div>'
                    )

            # 餐次操作按钮
            meal_actions = ""
            if is_editable:
                if mt == "afternoon_snack":
                    # V3: 下午茶 VV 自选，允许添加但不进入正式推送
                    meal_actions = f'<div class="meal-actions"><button class="meal-act-btn" onclick="openDishSearch(\'{mt}\')">＋ 添加餐点 <span class="meal-title-en">Add dish</span></button></div>'
                else:
                    meal_actions = f'<div class="meal-actions"><button class="meal-act-btn" onclick="openDishSearch(\'{mt}\')">＋ 添加菜品 <span class="meal-title-en">Add dish</span></button><button class="meal-act-btn" onclick="aiFillMeal(\'{mt}\',this)">AI 补充 AI Fill</button></div>'

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

                # 操作按钮：普通切换 + 搜索更换 + 删除。
                item_actions = ""
                if is_editable:
                    item_actions = (
                        f'<div class="item-actions"><button class="item-btn" onclick="cycleDish(\'{mt}\',{d["menu_item_id"]},this)" title="普通切换 Switch">↻</button>'
                        f'<button class="item-btn" onclick="openDishSearch(\'{mt}\',{d["menu_item_id"]},\'{d.get("dish_id","")}\',\'{d.get("category_id","")}\')" title="搜索更换 Search replace">⌕</button>'
                        f'<button class="item-btn danger" onclick="removeDish({d["menu_item_id"]})" title="删除 Delete">×</button></div>'
                    )

                items_html += f"""<div class="meal-item" data-item-id="{d["menu_item_id"]}">
{img_html}{no_img}
<div class="info">
<div class="dish-name">{source_badge}{archived_badge}{shortage_badge}{d["name_cn"]}</div>
<div class="dish-name-en">{d["name_en"] or ""}</div>
<div class="dish-meta">{cat_badge} {" · ".join(d.get("protein_types", []) or [])} {("· " + " ".join((d.get("vegetables") or [])[:2])) if d.get("vegetables") else ""}</div>
</div>{item_actions}</div>"""

            optional_label = '<div style="font-size:13px;color:#a89888;line-height:1.2">可选 Optional</div>' if mt == "afternoon_snack" else ""
            sections.append(f"""<div class="meal-section" data-meal="{mt}">
<div class="meal-header"><div class="meal-bar" style="background:{color}"></div><div><span class="meal-title">{cn}</span><span class="meal-title-en">{en}</span>{optional_label}</div>{meal_actions}</div>
{slot_hint}<div class="meal-items">{items_html}</div></div>""")

        # 采购任务区（可操作）
        if purchase_reqs and role == "owner":
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
                sections.append(
                    '<div class="tomorrow-actions">'
                    '<button class="btn btn-outline" onclick="repairMenu()">重新生成<small>Regenerate menu</small></button>'
                    '<button class="btn btn-primary" onclick="confirmMenu()">确认菜单<small>Confirm menu</small></button>'
                    '</div>'
                )
            elif menu["status"] == "confirmed":
                # V3: Confirmed → Edit Menu → Reconfirm flow
                if menu.get("push_status") == "failed":
                    sections.append('<div class="card" style="text-align:center;border-color:#c45a52"><p>菜单已确认，但推送失败<br><small>Menu confirmed, but push failed</small></p><button class="btn btn-primary" style="margin-top:8px" onclick="retryPush()">重新推送<small>Retry push</small></button></div>')
                elif menu.get("push_status") == "success":
                    sections.append('<div class="card" style="text-align:center"><p>📡 菜单已确认并推送 Menu confirmed and pushed</p></div>')
                else:
                    sections.append('<div class="card" style="text-align:center"><p>✅ 菜单已确认，等待推送 Menu confirmed, awaiting push</p></div>')
                sections.append('<button class="btn btn-outline" style="margin-top:8px" onclick="editMenu()">修改菜单 / Edit menu</button>')
            elif menu["status"] == "pushed":
                sections.append('<div class="card" style="text-align:center"><p>📡 菜单已推送 Menu Pushed</p></div>')
                sections.append('<button class="btn btn-outline" style="margin-top:8px" onclick="editMenu()">修改菜单 / Edit menu</button>')
                sections.append('<p style="font-size:13px;color:#a89888;margin-top:4px;text-align:center">修改后需重新确认并通知 Reconfirm required after edit</p>')

    body = "\n".join(sections)
    js = f"""<script>
let menuId={menu.get("menu_id","null")};
let currentLoc='{location}';
let hasUnsavedChanges=false;
let selectedDiners={json.dumps(menu_diners)};
let currentMealMode='{meal_mode}';
let banquetTotal={banquet_total};
async function requestJSON(path,options={{}}){{
  let response=await fetch(path,{{credentials:'same-origin',...options}});
  let result=await response.json().catch(()=>({{}}));
  if(!response.ok||result.ok===false)throw new Error(result.message||result.error||('HTTP '+response.status));
  return result;
}}
async function postJSON(path,payload){{return requestJSON(path,{{method:'POST',headers:{{'Content-Type':'application/json'}},body:JSON.stringify(payload)}})}}
async function toggleDiner(id){{
  let i=selectedDiners.indexOf(id);
  let next=i>=0?selectedDiners.filter(x=>x!==id):selectedDiners.concat(id);
  if(!next.length){{snack('至少保留一名用餐成员 At least one diner required');return;}}
  try{{
    await postJSON('/api/tomorrow/diners',{{menu_id:menuId,diners:next,location:currentLoc}});
    selectedDiners=next;snack('用餐成员已更新 Diners updated');location.reload();
  }}catch(error){{snack(error.message||'用餐成员更新失败');}}
}}
// V11: Meal Mode toggle
async function setMealMode(mode){{
  if(mode===currentMealMode)return;
  try{{
    await postJSON('/api/tomorrow/meal-mode',{{menu_id:menuId,meal_mode:mode,banquet_total_diners:mode==='banquet'?banquetTotal:null,location:currentLoc}});
    currentMealMode=mode;snack(mode==='banquet'?'已切换到家宴模式 Banquet mode':'已切换到日常模式 Daily mode');location.reload();
  }}catch(error){{snack(error.message||'用餐模式更新失败');}}
}}
function adjustBanquet(delta){{
  let input=document.getElementById('banquetTotal');
  let v=parseInt(input.value)||8;
  v=Math.max(2,Math.min(30,v+delta));
  input.value=v;
  setBanquetTotal(v);
}}
async function setBanquetTotal(val){{
  let v=parseInt(val)||8;
  v=Math.max(2,Math.min(30,v));
  banquetTotal=v;
  try{{await postJSON('/api/tomorrow/meal-mode',{{menu_id:menuId,meal_mode:'banquet',banquet_total_diners:v,location:currentLoc}});snack('家宴总人数已更新: '+v+' Total diners updated');setTimeout(()=>location.reload(),500);}}
  catch(error){{snack(error.message||'家宴人数更新失败');}}
}}
// V7: Smart dish replacement modal
let searchMode={{meal:null,replaceId:null,currentDishId:null,categoryId:null}},_searchRequestId=0,_pickBusy=false;
function openDishSearch(mt,replaceId,currentDishId,categoryId){{
  searchMode={{meal:mt,replaceId:replaceId||null,currentDishId:currentDishId||null,categoryId:categoryId||null}};
  let m=document.getElementById('dishSearchModal');
  m.classList.add('show');
  let input=document.getElementById('dishSearchInput');
  input.value='';
  let titleEl=document.getElementById('dishSearchTitle');
  let hintEl=document.getElementById('dishSearchHint');
  if(replaceId){{
    titleEl.textContent='搜索更换 Search Replace';
    hintEl.style.display='block';
    loadRecommendations();
  }}else{{
    titleEl.textContent='添加菜品 Add Dish';
    hintEl.style.display='block';
    loadRecommendations();
  }}
  input.focus();
}}
function closeDishSearch(){{clearTimeout(_searchTimer);_searchRequestId++;document.getElementById('dishSearchModal').classList.remove('show');}}
let _searchTimer=null;
function pickerStatus(message,isError=false){{let c=document.getElementById('dishSearchResults'),s=c.querySelector('.picker-status');if(!s){{s=document.createElement('div');s.className='picker-status';c.prepend(s)}}s.textContent=message;s.classList.toggle('inline-warning',isError)}}
function onDishSearchInput(){{
  clearTimeout(_searchTimer);
  _searchRequestId++;
  let q=document.getElementById('dishSearchInput').value.trim();
  if(!q){{
    loadRecommendations();
    return;
  }}
  _searchTimer=setTimeout(()=>doDishSearch(q),300);
}}
async function loadRecommendations(){{
  let container=document.getElementById('dishSearchResults');
  let requestId=++_searchRequestId;
  pickerStatus('推荐加载中… Loading recommendations…');
  try{{
    let [data,all]=await Promise.all([
      postJSON('/api/dishes/recommend',{{meal_type:searchMode.meal,current_dish_id:searchMode.currentDishId,category_id:searchMode.categoryId,location:currentLoc}}),
      requestJSON('/api/dishes')
    ]);
    if(requestId!==_searchRequestId)return;
    all=all.filter(d=>d.id!==searchMode.currentDishId);
    let availability=all.length?await postJSON('/api/dishes/availability',{{dish_ids:all.map(d=>d.id),location:currentLoc}}):{{}};
    if(requestId!==_searchRequestId)return;
    window._recMap={{}};
    all.forEach(d=>{{let av=availability[d.id]||{{}};window._recMap[d.id]={{...d,missing_required:av.missing_names||[],missing_required_en:av.missing_names_en||[],availability:av.status||'missing'}};}});
    let available=(data.available||[]).filter(d=>d.id!==searchMode.currentDishId),almost=(data.almost_available||[]).filter(d=>d.id!==searchMode.currentDishId);
    available.forEach(d=>{{window._recMap[d.id]=d;}});almost.forEach(d=>{{window._recMap[d.id]=d;}});
    let allCards=all.slice(0,20).map(d=>{{let item=window._recMap[d.id];return renderRecCard(item,item.availability==='available'?'available':item.availability==='almost_available'?'almost':'missing');}}).join('');
    container.innerHTML='<div class="rec-section-title">库存可做 Available now</div>'+(available.map(d=>renderRecCard(d,'available')).join('')||'<div class="empty">暂无库存可做菜品 None</div>')+
      '<div class="rec-section-title">差少量 Almost available</div>'+(almost.map(d=>renderRecCard(d,'almost')).join('')||'<div class="empty">暂无差少量菜品 None</div>')+
      '<div class="rec-section-title">全部菜品 All dishes</div>'+allCards;
  }}catch(error){{if(requestId===_searchRequestId)pickerStatus('推荐加载失败：'+error.message,true);}}
}}
function renderRecCard(d,type){{
  let img=d.image?'<img loading="lazy" decoding="async" width="60" height="60" src="/photos/'+d.image+'" onerror="this.style.display=\\'none\\';this.nextElementSibling.style.display=\\'flex\\'" style="width:60px;height:60px;border-radius:8px;object-fit:cover;flex-shrink:0">':'<div style="width:60px;height:60px;border-radius:8px;background:#f5f0e8;display:flex;align-items:center;justify-content:center;font-size:24px;flex-shrink:0">🍽️</div>';
  let noImg=d.image?'<div style="width:60px;height:60px;border-radius:8px;background:#f5f0e8;display:none;align-items:center;justify-content:center;font-size:24px;flex-shrink:0">🍽️</div>':'';
  let badge=type==='available'?'<span style="font-size:13px;color:#155724;background:#d4edda;padding:2px 8px;border-radius:4px;display:inline-block;margin-top:3px">库存可做 Available</span>':'';
  let missing='';
  if(d.missing_required&&d.missing_required.length){{
    let mn=d.missing_required.join(', ');
    let mnEn=d.missing_required_en?d.missing_required_en.join(', '):'';
    missing='<span style="font-size:13px;color:#856404;background:#fff3cd;padding:2px 8px;border-radius:4px;display:inline-block;margin-top:3px">缺：'+mn+' Missing: '+mnEn+'</span>';
  }}
  return '<button type="button" data-dish-id="'+d.id+'" onclick="pickRec(\\''+d.id+'\\','+(type!=='available')+',this)" style="width:100%;display:flex;gap:10px;padding:12px;border:0;border-bottom:1px solid #f5f0e8;background:#fff;cursor:pointer;align-items:center;text-align:left">'+img+noImg+'<span style="flex:1;min-width:0"><span style="display:block;font-size:18px;font-weight:600">'+d.name_cn+'</span><span style="display:block;font-size:14px;color:#a89888;font-style:italic">'+(d.name_en||'')+'</span>'+badge+missing+'</span></button>';
}}
async function pickRec(dishId,isAlmost,button){{
  if(isAlmost){{
    let d=window._recMap&&window._recMap[dishId];
    if(d&&d.missing_required&&d.missing_required.length){{
      let mn=d.missing_required.join(', ');
      let mnEn=d.missing_required_en?d.missing_required_en.join(', '):'';
      if(!confirm('这道菜还缺：'+mn+'\\n\\nThis dish is missing:\\n'+mnEn+'\\n\\n仍然选择? Choose Anyway?'))return;
    }}
  }}
  await doPickDish(dishId,button);
}}
async function doDishSearch(q){{
  let container=document.getElementById('dishSearchResults');
  let requestId=++_searchRequestId;
  pickerStatus('搜索中… Searching…');
  try{{
    let data=await requestJSON('/api/dishes?search='+encodeURIComponent(q));
    if(requestId!==_searchRequestId)return;
    data=data.filter(d=>d.id!==searchMode.currentDishId).slice(0,20);
    let availability=data.length?await postJSON('/api/dishes/availability',{{dish_ids:data.map(d=>d.id),location:currentLoc}}):{{}};
    if(requestId!==_searchRequestId)return;
    window._recMap={{}};
    data.forEach(d=>{{let av=availability[d.id]||{{}};window._recMap[d.id]={{...d,missing_required:av.missing_names||[],missing_required_en:av.missing_names_en||[]}};}});
    container.innerHTML='<div class="rec-section-title">搜索结果 Search results</div>'+(data.length?data.map(d=>{{let av=availability[d.id]||{{}};return renderRecCard(window._recMap[d.id],av.status==='available'?'available':av.status==='almost_available'?'almost':'missing');}}).join(''):'<div class="empty">没有找到相关菜品 No matching dishes</div>');
  }}catch(error){{if(requestId===_searchRequestId)pickerStatus('搜索失败：'+error.message,true);}}
}}
async function refreshTomorrowFragments(meal,itemId){{
  let response=await fetch('/tomorrow',{{cache:'no-store'}});
  if(!response.ok)throw new Error('页面更新失败');
  let doc=new DOMParser().parseFromString(await response.text(),'text/html');
  let oldSection=document.querySelector('.meal-section[data-meal="'+meal+'"]'),newSection=doc.querySelector('.meal-section[data-meal="'+meal+'"]');
  if(!oldSection||!newSection)throw new Error('找不到餐次区域');
  let oldCard=oldSection.querySelector('[data-item-id="'+itemId+'"]'),newCard=newSection.querySelector('[data-item-id="'+itemId+'"]');
  if(oldCard&&newCard)oldCard.replaceWith(newCard);
  let oldWarning=oldSection.querySelector('.inline-warning,.slot-hint'),newWarning=newSection.querySelector('.inline-warning,.slot-hint');
  if(oldWarning&&newWarning)oldWarning.replaceWith(newWarning);else if(oldWarning)oldWarning.remove();else if(newWarning)oldSection.querySelector('.meal-header').insertAdjacentElement('afterend',newWarning);
  let oldNutrition=document.querySelector('.nutrition-card,.nutrition-overview'),newNutrition=doc.querySelector('.nutrition-card,.nutrition-overview');
  if(oldNutrition&&newNutrition)oldNutrition.replaceWith(newNutrition);
}}
async function doPickDish(dishId,button){{
  if(_pickBusy)return;
  let original=button?button.innerHTML:'';
  _pickBusy=true;
  if(button){{button.disabled=true;button.innerHTML='<span style="flex:1">处理中… Processing…</span>';}}
  try{{
    if(searchMode.replaceId){{await postJSON('/api/tomorrow/replace',{{menu_id:menuId,menu_item_id:searchMode.replaceId,new_dish_id:dishId}});await refreshTomorrowFragments(searchMode.meal,searchMode.replaceId);}}
    else await postJSON('/api/tomorrow/add',{{menu_id:menuId,dish_id:dishId,meal_type:searchMode.meal}});
    closeDishSearch();snack(searchMode.replaceId?'已替换 Replaced':'已添加 Added');
  }}catch(error){{snack(error.message||'操作失败');pickerStatus('操作失败：'+error.message,true);if(button){{button.disabled=false;button.innerHTML=original;}}}}
  finally{{_pickBusy=false;}}
}}
async function cycleDish(meal,itemId,button){{
  if(button.disabled)return;button.disabled=true;
  try{{let result=await postJSON('/api/tomorrow/cycle-replace',{{menu_id:menuId,menu_item_id:itemId,location:currentLoc}});if(!result.replaced){{snack('暂无其他可做同类菜品 / No other available dish');button.disabled=false;return;}}await refreshTomorrowFragments(meal,itemId);snack('已切换为：'+result.dish.name_cn);}}
  catch(error){{snack(error.message||'切换失败');button.disabled=false;}}
}}
async function removeDish(itemId){{
  if(!confirm('确认删除? Confirm delete?'))return;
  let r=await fetch('/api/tomorrow/remove',{{method:'POST',headers:{{'Content-Type':'application/json'}},body:JSON.stringify({{menu_id:menuId,menu_item_id:itemId}})}});
  let result=await r.json();
  if(result.ok){{snack('已删除 Removed');location.reload();}}else{{snack(result.error||'删除失败');}}
}}
async function aiFillMeal(mt,button){{
  snack('AI 补充中... AI filling...');
  if(button){{button.disabled=true;button.setAttribute('aria-busy','true');button.textContent='补充中… Filling…';}}
  try{{
    let result=await postJSON('/api/tomorrow/ai-fill',{{menu_id:menuId,location:currentLoc,meal_type:mt}});
    // V10: 用页面内 Warning Card 替代 alert()
    let unmet=result.review&&(result.review.unmet_slots||[]);
    let added=result.message||'AI 补充完成';
    if(unmet&&unmet.length>0){{
      // 去重显示
      let seen={{}};
      let uniqueUnmet=unmet.filter(u=>{{
        let k=(u.meal||'')+'|'+(u.slot||'');
        if(seen[k])return false;seen[k]=true;return true;
      }});
      let msgs=uniqueUnmet.map(u=>u.message||(''+u.slot)).join('<br>• ');
      showWarningCard('AI 补充完成，但仍有槽位未补齐：','• '+msgs+'<br><br>请手动添加缺失菜品 / Please add missing dishes manually.');
    }}else if(result.review&&result.review.reconciled){{
      snack('已重新调整 AI 推荐 Reconciled for '+result.review.reconcile_diners+' diners');
    }}else{{
      snack('AI 补充完成 AI fill done');
    }}
    location.reload();
  }}catch(error){{snack(error.message||'补充失败');if(button){{button.disabled=false;button.removeAttribute('aria-busy');button.textContent='AI 补充 AI Fill';}}}}
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
    if(result.push_failed){{
      snack('菜单已确认，但推送失败');
    }}else if(result.warnings&&result.warnings.length){{
      snack('已确认（有'+result.warnings.length+'项提示）');
    }}else{{
      snack('已确认 Confirmed');
    }}
    setTimeout(()=>location.reload(),1500);
  }}else{{
    snack(result.error||'确认失败');
  }}
}}
async function retryPush(){{
  let r=await fetch('/api/tomorrow/push',{{method:'POST',headers:{{'Content-Type':'application/json'}},body:JSON.stringify({{menu_id:menuId}})}});
  let result=await r.json();
  if(result.ok){{snack('重新推送成功 Push sent');setTimeout(()=>location.reload(),1000);}}
  else{{snack(result.error||'重新推送失败');}}
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

    return f"""{page_head("明日菜单 · Tomorrow", "tomorrow", location, role)}
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

    return f"""{page_head("菜品库 · Dishes", "dishes", location, role)}
<div class="search-bar"><input type="text" id="search" placeholder="搜索菜名 Search dishes..." oninput="loadDishes()"></div>
{cat_tabs_html}
{avail_tabs_html}
<div class="content dishes-page">
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
  // Availability always comes from the shared backend resolver.
  if(data.length){{
    let ids=data.map(d=>d.id);
    let avRes=await fetch('/api/dishes/availability',{{
      method:'POST',headers:{{'Content-Type':'application/json'}},
      body:JSON.stringify({{dish_ids:ids,location:'{location}'}})
    }});
    availData=await avRes.json();
    if(currentAvail){{
      data=data.filter(d=>{{
        let av=availData[d.id];
        if(!av)return false;
        if(currentAvail==='available')return av.status==='available';
        if(currentAvail==='almost')return av.status==='almost_available';
        return true;
      }});
    }}
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
      if(av.status==='available')avBadge='<div class="avail-badge avail-yes">库存可做 Available now</div>';
      else if(av.status==='almost_available'){{
        let mn=av.missing_names||[];
        let men=(av.missing_names_en||[]).filter(Boolean);
        avBadge='<div class="avail-badge avail-almost">缺'+av.missing_count+'项 / Missing '+av.missing_count+' ingredient'+(av.missing_count===1?'':'s')+' · 缺少：'+mn.join('、')+(men.length?' / Missing: '+men.join(', '):'')+'</div>';
      }}
      else if(av.status==='incomplete'){{
        let labels={{required_ingredients:'必需食材 required ingredients',category:'分类 category',meal_tags:'餐别 meal tags',image:'图片 image',dish:'菜品 dish'}};
        let missing=(av.missing_fields||['required_ingredients']).map(x=>labels[x]||x).join('、');
        avBadge='<div class="avail-badge" style="background:#e8e0d4;color:#6c757d">缺少：'+missing+'</div>';
      }}
      else if(av.status==='missing'&&av.missing_count>0){{
        let mn=av.missing_names||[];
        let men=(av.missing_names_en||[]).filter(Boolean);
        avBadge='<div class="avail-badge" style="background:#f8d7da;color:#721c24">缺少食材 / Missing ingredients · 缺少：'+mn.slice(0,3).join('、')+(men.length?' / Missing: '+men.slice(0,3).join(', '):'')+'</div>';
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

def _ensure_preview_consumed_history():
    """Create the consumption ledger only in the isolated local preview DB."""
    if os.environ.get("LOCAL_PREVIEW_UI", "").lower() != "true":
        return
    conn = get_db()
    try:
        conn.execute(
            "CREATE TABLE IF NOT EXISTS consumed_history ("
            "id INTEGER PRIMARY KEY AUTOINCREMENT, location TEXT NOT NULL, "
            "ingredient_id TEXT NOT NULL, consumed_at TEXT NOT NULL, "
            "consumed_by TEXT NOT NULL DEFAULT 'owner', source TEXT NOT NULL DEFAULT 'pantry_used_up', "
            "UNIQUE(location, ingredient_id, consumed_at))"
        )
        conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_consumed_history_location_date "
            "ON consumed_history(location, consumed_at DESC)"
        )
        migration_key = "preview_consumed_history_migrated_v1"
        migrated = conn.execute("SELECT 1 FROM config WHERE key = ?", (migration_key,)).fetchone()
        if not migrated:
            # Preserve the existing preview soft-deletes once; future removals
            # from Common ingredients are not consumption events.
            conn.execute(
                "INSERT OR IGNORE INTO consumed_history "
                "(location, ingredient_id, consumed_at, consumed_by, source) "
                "SELECT location, ingredient_id, updated_at, 'preview_migration', 'legacy_soft_delete' "
                "FROM current_pantry WHERE is_active = 0 AND updated_at IS NOT NULL"
            )
            conn.execute("INSERT INTO config (key, value) VALUES (?, ?)", (migration_key, datetime.now().isoformat()))
        conn.commit()
    finally:
        conn.close()


def _get_recently_consumed_items(location, limit=5):
    _ensure_preview_consumed_history()
    conn = get_db()
    try:
        rows = conn.execute(
            "SELECT ch.id, ch.ingredient_id, ch.consumed_at, i.name_cn, i.name_en "
            "FROM consumed_history ch JOIN ingredients i ON ch.ingredient_id = i.ingredient_id "
            "WHERE ch.location = ? AND datetime(ch.consumed_at) >= datetime('now', '-30 days') "
            "ORDER BY datetime(ch.consumed_at) DESC, ch.id DESC LIMIT ?",
            (location, limit),
        ).fetchall()
        return [dict(row) for row in rows]
    finally:
        conn.close()


def _get_recently_consumed_count(location):
    _ensure_preview_consumed_history()
    conn = get_db()
    try:
        row = conn.execute(
            "SELECT COUNT(*) AS count FROM consumed_history "
            "WHERE location = ? AND datetime(consumed_at) >= datetime('now', '-30 days')",
            (location,),
        ).fetchone()
        return int(row["count"] if row else 0)
    finally:
        conn.close()


def _normalize_preview_pantry_manual_states():
    """Remove the retired Priority state from the isolated preview pantry."""
    if os.environ.get("LOCAL_PREVIEW_UI", "").lower() != "true":
        return
    conn = get_db()
    try:
        locations = [row["location"] for row in conn.execute(
            "SELECT DISTINCT location FROM current_pantry "
            "WHERE is_active = 1 AND status = 'priority_use'"
        ).fetchall()]
        if not locations:
            return
        conn.execute(
            "UPDATE current_pantry SET status = 'available', updated_at = ? "
            "WHERE is_active = 1 AND status = 'priority_use'",
            (datetime.now().isoformat(),),
        )
        for loc in locations:
            _increment_inventory_version(conn, loc)
        conn.commit()
        for loc in locations:
            _invalidate_availability_cache(loc)
    finally:
        conn.close()


def _join_summary(values, language="zh"):
    clean = [value for value in values if value]
    if not clean:
        return ""
    if len(clean) == 1:
        return clean[0]
    if language == "zh":
        return "、".join(clean[:-1]) + "和" + clean[-1]
    return ", ".join(clean[:-1]) + " and " + clean[-1]


def _normalize_ingredient_name(value):
    """Normalize only spacing; exact matching remains semantically strict."""
    return " ".join(str(value or "").strip().split())


def render_pantry_reference_preview(role="owner", location="shenzhen"):
    _normalize_preview_pantry_manual_states()
    pantry = get_current_pantry(location)
    active_items = list(pantry.get("items", [])) if pantry else []
    recently_used = _get_recently_consumed_items(location, limit=4)
    recently_used_count = _get_recently_consumed_count(location)
    active_items.sort(key=lambda item: (0 if item.get("status") == "expiring" else 1, item.get("name_cn", "")))

    common = get_common_ingredients(location)
    all_ingredients = get_all_ingredients()
    active_ids = {item["ingredient_id"] for item in active_items}
    pantry_count = len(active_items)
    expiring_items = [item for item in active_items if item.get("status") == "expiring"]

    kitchen_names = {
        "shenzhen": ("深圳厨房", "Shenzhen Kitchen"),
        "hongkong": ("香港厨房", "Hong Kong Kitchen"),
    }
    kitchen_cn, kitchen_en = kitchen_names.get(location, (LOCATIONS.get(location, location), location.title()))

    def item_row(item):
        ingredient_id = escape(str(item["ingredient_id"]), quote=True)
        name_cn = escape(item.get("name_cn") or item["ingredient_id"])
        name_en = escape(item.get("name_en") or "")
        status = item.get("status", "available")
        if status == "expiring":
            status_html = f'<div class="inventory-status needs-status">{bilingual("! 快过期", "Expiring", "inline-action")}</div>'
            action_html = (
                f'<button class="stock-button attention-toggle clear-attention" type="button" onclick="toggleNeedsAttention(this,\'{ingredient_id}\',false)">{bilingual("取消快过期", "Remove expiring")}</button>'
                f'<button class="stock-button used-up-button" type="button" onclick="usedUpAndRecord(this,\'{ingredient_id}\')">{bilingual("用完了", "Used up")}</button>'
            )
            action_class = "two-actions"
        else:
            status_html = ""
            action_html = (
                f'<button class="stock-button attention-toggle" type="button" onclick="toggleNeedsAttention(this,\'{ingredient_id}\',true)">{bilingual("快过期", "Expiring")}</button>'
                f'<button class="stock-button used-up-button" type="button" onclick="usedUpAndRecord(this,\'{ingredient_id}\')">{bilingual("用完了", "Used up")}</button>'
            )
            action_class = "two-actions"
        return f"""<div class="ingredient-row" data-status="{status}" data-ingredient-id="{ingredient_id}">
<div class="ingredient-copy"><div class="ingredient-name"><strong>{name_cn}</strong><small>{name_en}</small></div>{status_html}</div>
<div class="stock-actions {action_class}">{action_html}</div></div>"""

    active_rows = "".join(item_row(item) for item in active_items)
    if not active_rows:
        active_rows = '<div class="inventory-empty"><strong>暂无库存</strong><small>No ingredients in this pantry</small></div>'

    month_names = ("", "Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec")
    recent_rows = ""
    for item in recently_used:
        raw_date = str(item.get("consumed_at") or "")
        try:
            consumed_dt = datetime.fromisoformat(raw_date.replace("Z", "+00:00"))
            date_cn = consumed_dt.strftime("%Y-%m-%d")
            date_en = f"{month_names[consumed_dt.month]} {consumed_dt.day}"
        except ValueError:
            date_cn = raw_date[:10]
            date_en = raw_date[:10]
        recent_rows += f"""<article class="recently-used-item">
<div class="ingredient-name"><strong>{escape(item.get('name_cn') or item['ingredient_id'])}</strong><small>{escape(item.get('name_en') or '')}</small></div>
<p><strong>已用完</strong><time datetime="{date_cn}">{date_cn}</time><small>Used up {date_en}</small></p></article>"""
    if not recent_rows:
        recent_rows = '<div class="recently-used-empty">暂无最近消耗<small>No ingredients used in the last 30 days</small></div>'

    attention_html = ""
    if expiring_items:
        summary_cn = escape(_join_summary([item.get("name_cn", "") for item in expiring_items], "zh"))
        summary_en = escape(_join_summary([item.get("name_en", "") or item.get("name_cn", "") for item in expiring_items], "en"))
        attention_html = f"""<section class="attention-banner" aria-label="需尽快使用食材提醒">
<strong>{len(expiring_items)}</strong><div class="attention-banner-copy">
<b>项需尽快使用<small>ingredients need attention</small></b>
<span><b>需要留意<small>Needs attention</small></b>请优先安排：{summary_cn}。<small>Use {summary_en} first.</small></span>
</div></section>"""

    common_html = "".join(
        f"""<button class="common-chip {'selected' if item['ingredient_id'] in active_ids else ''}" type="button"
data-ingredient-id="{escape(str(item['ingredient_id']), quote=True)}" onclick="toggleCommon(this)">
{bilingual(escape(item.get('name_cn') or item['ingredient_id']), escape(item.get('name_en') or ''))}
<span class="chip-icon">{'✓' if item['ingredient_id'] in active_ids else '+'}</span></button>"""
        for item in common
    )

    all_json = json.dumps([
        {
            "id": item["ingredient_id"],
            "cn": item.get("name_cn") or item["ingredient_id"],
            "en": item.get("name_en") or "",
            "aliases": item.get("aliases") or [],
        }
        for item in all_ingredients
    ], ensure_ascii=False).replace("</", "<\\/")
    active_json = json.dumps(sorted(active_ids), ensure_ascii=False)
    filter_count = 3

    return f"""{tomorrow_preview_head("食材库存 · Pantry", "pantry", location)}
<main class="page-shell pantry-page">
<section class="view-heading"><div><h1>{bilingual("食材库存", "Pantry")}</h1>
<p>{bilingual("记录库存，菜单优先使用现有食材", "Menus use available ingredients first")}</p></div>
<div class="view-count" id="pantryViewCount" aria-label="当前库存 {pantry_count} 项">{bilingual(str(pantry_count), "items", "count-value")}<span class="count-unit">项</span></div></section>
{attention_html}
<section class="pantry-toolbar"><label>{bilingual("搜索或添加食材 /", "Search or add an ingredient", "inline-label")}
<input id="pantrySearch" type="text" autocomplete="off" placeholder="输入食材名称 / Ingredient name" oninput="updateSearchResults()"></label>
<button class="pantry-add" type="button" onclick="addFromSearch()">{bilingual("添加食材", "Add ingredient")}</button>
<div class="pantry-search-results" id="pantrySearchResults"></div><div class="pantry-feedback" id="pantryFeedback" aria-live="polite"></div></section>

<div class="pantry-layout"><div class="pantry-main"><section class="inventory-panel"><header><div><h2>{bilingual("当前库存", "Current pantry")}</h2>
<p class="inventory-meta"><span class="meta-inline"><span>{kitchen_cn} · 今天更新。</span><small>{kitchen_en} · Updated today</small></span></p></div>
<button class="same-last" type="button" onclick="sameAsLast(this)">{bilingual("✓ 和上次一样 /", "Same as last update", "inline-action")}</button></header>
<nav class="inventory-filters" aria-label="库存筛选" style="--filter-count:{filter_count}">
<button class="inventory-filter active" id="pantryAllFilter" type="button" data-filter="all" onclick="filterInventory(this)">{bilingual(f"全部 {pantry_count}", "All")}</button>
<button class="inventory-filter" id="pantryNeedsFilter" type="button" data-filter="needs" onclick="filterInventory(this)">{bilingual(f"需处理 {len(expiring_items)}", "Needs attention")}</button>
<button class="inventory-filter" id="pantryRecentFilter" type="button" data-filter="recent" onclick="filterInventory(this)">{bilingual(f"最近消耗 {recently_used_count}", "Recently used")}</button>
</nav><div class="inventory-list" id="activeInventoryList">{active_rows}</div></section>
<section class="recently-used-panel" id="recentlyUsedPanel"><header><h2>{bilingual("最近消耗", "Recently used")}</h2>
<p>{bilingual("最近 30 天 · 最多 4 项", "Last 30 days · Up to 4 items")}</p></header>
<div class="recently-used-list">{recent_rows}</div></section></div>

<aside class="pantry-aside"><section><h2>{bilingual("常用食材", "Common ingredients")}</h2>
<p>点击标签即可加入或移出当前库存<small>Tap a tag to add or remove it from the pantry</small></p>
<div class="common-chips">{common_html}</div></section></aside></div></main>
<div class="pantry-snack" id="pantrySnack" aria-live="polite"></div>
<script>
const pantryIngredients={all_json};
const pantryActiveIds=new Set({active_json});
const pantryLocation={json.dumps(location)};
let selectedIngredientId=null;

function pantryMessage(message,type=''){{
  const feedback=document.getElementById('pantryFeedback');
  feedback.textContent=message;feedback.className='pantry-feedback '+type;
}}
function pantrySnack(message){{
  const bar=document.getElementById('pantrySnack');bar.textContent=message;bar.classList.add('show');
  clearTimeout(window.pantrySnackTimer);window.pantrySnackTimer=setTimeout(()=>bar.classList.remove('show'),1800);
}}
async function pantryPost(path,body){{
  const controller=new AbortController();const timer=setTimeout(()=>controller.abort(),15000);
  let response,text,result;
  try{{response=await fetch(path,{{method:'POST',headers:{{'Content-Type':'application/json'}},body:JSON.stringify(body),signal:controller.signal}});text=await response.text();}}
  finally{{clearTimeout(timer);}}
  try{{result=JSON.parse(text)}}catch(error){{throw new Error('HTTP '+response.status+' 返回无效 JSON / Invalid JSON response')}}
  if(!response.ok||!result.ok)throw new Error(result.error||result.message||('HTTP '+response.status));return result;
}}
function updatePantryCounts(result){{
  const pantryCount=Number(result.pantry_count);const expiringCount=Number(result.expiring_count);
  if(Number.isFinite(pantryCount)){{const count=document.querySelector('#pantryViewCount .lang-zh');if(count)count.textContent=pantryCount;document.getElementById('pantryAllFilter').querySelector('.lang-zh').textContent='全部 '+pantryCount;}}
  if(Number.isFinite(expiringCount))document.getElementById('pantryNeedsFilter').querySelector('.lang-zh').textContent='需处理 '+expiringCount;
}}
async function pantryStateMatches(ingredientId,expectedStatus){{
  try{{const response=await fetch('/api/pantry',{{cache:'no-store'}});if(!response.ok)return false;const data=await response.json();const item=(data.items||[]).find(value=>value.ingredient_id===ingredientId);return expectedStatus===null?!item:!!item&&item.status===expectedStatus;}}
  catch(error){{return false;}}
}}
function applyAttentionState(row,ingredientId,enabled){{
  row.dataset.status=enabled?'expiring':'available';
  const copy=row.querySelector('.ingredient-copy');let status=copy.querySelector('.inventory-status');
  if(enabled&&!status){{status=document.createElement('div');status.className='inventory-status needs-status';status.innerHTML='<span class="bilingual-pair inline-action"><span class="lang-zh">! 快过期</span><span class="lang-en">Expiring</span></span>';copy.appendChild(status);}}
  if(!enabled&&status)status.remove();
  const action=row.querySelector('.attention-toggle');action.classList.toggle('clear-attention',enabled);action.onclick=()=>toggleNeedsAttention(action,ingredientId,!enabled);action.innerHTML=enabled?'<span class="bilingual-pair"><span class="lang-zh">取消快过期</span><span class="lang-en">Remove expiring</span></span>':'<span class="bilingual-pair"><span class="lang-zh">快过期</span><span class="lang-en">Expiring</span></span>';
  row.querySelectorAll('button').forEach(item=>item.disabled=false);
}}
function normalizedSearch(value){{return String(value||'').trim().toLowerCase();}}
function matchingIngredients(query){{
  return pantryIngredients.filter(item=>[item.cn,item.en,...(item.aliases||[])].some(value=>normalizedSearch(value).includes(query))).slice(0,8);
}}
function updateSearchResults(){{
  const input=document.getElementById('pantrySearch');const query=normalizedSearch(input.value);
  const results=document.getElementById('pantrySearchResults');selectedIngredientId=null;results.replaceChildren();pantryMessage('');
  if(!query){{results.classList.remove('visible');return;}}
  const matches=matchingIngredients(query);
  matches.forEach(item=>{{
    const button=document.createElement('button');button.type='button';
    const cn=document.createElement('strong');cn.textContent=item.cn+(pantryActiveIds.has(item.id)?' · 已在库存':'');
    const en=document.createElement('small');en.textContent=item.en||'';button.append(cn,en);
    button.addEventListener('click',()=>{{input.value=item.cn;selectedIngredientId=item.id;results.classList.remove('visible');pantryMessage(pantryActiveIds.has(item.id)?'该食材已在当前库存中 / Already in pantry':'已选择 '+item.cn);}});
    results.appendChild(button);
  }});
  results.classList.toggle('visible',matches.length>0);
}}
async function addFromSearch(){{
  const input=document.getElementById('pantrySearch');const rawValue=input.value;
  const normalizedValue=rawValue.trim().replace(/\\s+/g,' ');
  if(!normalizedValue){{pantryMessage('请输入食材名称 / Enter an ingredient name','error');return;}}
  const button=document.querySelector('.pantry-add');button.disabled=true;
  try{{
    const result=await pantryPost('/api/pantry/add-by-name',{{ingredient_name:rawValue,location:pantryLocation,submitted_by:'owner'}});
    if(result.already_in_pantry){{
      pantryMessage('该食材已在库存中 / Already in pantry','error');
      const allFilter=document.querySelector('.inventory-filter[data-filter="all"]');
      if(allFilter&&!allFilter.classList.contains('active'))filterInventory(allFilter);
      const row=[...document.querySelectorAll('#activeInventoryList .ingredient-row')].find(item=>item.dataset.ingredientId===result.ingredient_id);
      if(row){{row.hidden=false;row.scrollIntoView({{behavior:'smooth',block:'center'}});row.classList.add('ingredient-highlight');setTimeout(()=>row.classList.remove('ingredient-highlight'),1800);}}
      button.disabled=false;return;
    }}
    input.value='';selectedIngredientId=null;
    const results=document.getElementById('pantrySearchResults');results.replaceChildren();results.classList.remove('visible');
    pantryMessage('已添加 / Added','success');pantrySnack('已添加 / Added');setTimeout(()=>location.reload(),650);
  }}catch(error){{pantryMessage('添加失败 / '+error.message,'error');button.disabled=false;}}
}}
async function toggleNeedsAttention(button,ingredientId,enabled){{
  const row=button.closest('.ingredient-row');const next=enabled?'expiring':'available';
  row.querySelectorAll('button').forEach(item=>item.disabled=true);
  try{{const result=await pantryPost('/api/pantry/update_status',{{ingredient_id:ingredientId,status:next,location:pantryLocation}});applyAttentionState(row,ingredientId,enabled);updatePantryCounts(result);pantrySnack('已更新 / Updated');}}
  catch(error){{if(await pantryStateMatches(ingredientId,next)){{applyAttentionState(row,ingredientId,enabled);pantrySnack('已更新 / Updated');return;}}row.querySelectorAll('button').forEach(item=>item.disabled=false);pantrySnack('更新失败 / '+error.message);}}
}}
async function usedUpAndRecord(button,ingredientId){{
  if(!confirm('确认已经用完了吗？食材会移出当前库存并记录到最近消耗。\\nMark as used up and record in Recently used?'))return;
  button.classList.add('selected-used');button.closest('.ingredient-row').querySelectorAll('button').forEach(item=>item.disabled=true);
  const row=button.closest('.ingredient-row');
  try{{const result=await pantryPost('/api/pantry/consume',{{ingredient_id:ingredientId,location:pantryLocation,consumed_by:'owner'}});row.remove();pantryActiveIds.delete(ingredientId);updatePantryCounts(result);if(Number.isFinite(Number(result.recently_used_count)))document.getElementById('pantryRecentFilter').querySelector('.lang-zh').textContent='最近消耗 '+result.recently_used_count;pantrySnack('已移出库存 / Used up');}}
  catch(error){{if(await pantryStateMatches(ingredientId,null)){{row.remove();pantryActiveIds.delete(ingredientId);pantrySnack('已移出库存 / Used up');return;}}row.querySelectorAll('button').forEach(item=>item.disabled=false);button.classList.remove('selected-used');pantrySnack('标记用完失败 / '+error.message);}}
}}
async function toggleCommon(button){{
  const ingredientId=button.dataset.ingredientId;button.disabled=true;
  try{{
    if(pantryActiveIds.has(ingredientId))await pantryPost('/api/pantry/remove',{{ingredient_id:ingredientId,location:pantryLocation}});
    else await pantryPost('/api/pantry/add',{{ingredient_id:ingredientId,location:pantryLocation}});
    location.reload();
  }}catch(error){{button.disabled=false;pantrySnack('更新失败 / Update failed');}}
}}
async function sameAsLast(button){{
  button.disabled=true;try{{await pantryPost('/api/pantry/same-as-last',{{location:pantryLocation,submitted_by:'owner'}});pantrySnack('已确认 / Confirmed');}}
  catch(error){{pantrySnack('确认失败 / Confirm failed');}}finally{{button.disabled=false;}}
}}
function filterInventory(button){{
  document.querySelectorAll('.inventory-filter').forEach(item=>item.classList.toggle('active',item===button));
  const filter=button.dataset.filter;const list=document.getElementById('activeInventoryList');
  const statusFor={{needs:'expiring'}};
  list.hidden=filter==='recent';
  document.querySelectorAll('#activeInventoryList .ingredient-row').forEach(row=>row.hidden=filter!=='all'&&filter!=='recent'&&row.dataset.status!==statusFor[filter]);
  if(filter==='recent')document.getElementById('recentlyUsedPanel').scrollIntoView({{behavior:'smooth',block:'start'}});
}}
</script></body></html>"""


def render_pantry(role="nanny", location="shenzhen"):
    if os.environ.get("LOCAL_PREVIEW_UI", "").lower() == "true":
        return render_pantry_reference_preview(role, location)
    pantry = get_current_pantry(location)
    reqs = get_purchase_requests(location=location) if location else []
    common = get_common_ingredients(location)
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
<div class="pantry-name"><div class="name">{item["name_cn"]}</div><div class="name-en">{name_en}</div></div>
<div class="pantry-controls">
<div class="pantry-status-group">
<span class="st-btn {pf_cls}" onclick="toggleStatus('{ing_id}','priority_use')">优先用<br>Use First</span>
<span class="st-btn {ex_cls}" onclick="toggleStatus('{ing_id}','expiring')">快过期<br>Expiring Soon</span>
</div>
<span class="pantry-used-up" onclick="usedUp('{ing_id}')">用完<br>Used Up</span>
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
    return f"""{page_head("食材库存 · Pantry", "pantry", location, role)}
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
    common = get_common_ingredients(location)
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

    return f"""{page_head("管理库存 · Manage Pantry", "pantry", location, role)}
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
      allIngs.push({{id:data.ingredient_id,cn:data.name_cn||name,en:data.name_en||'',aliases:[],group:'vegetable_mushroom'}});
      // Select it
      selected[data.ingredient_id]={{cn:data.name_cn||name,en:data.name_en||'',status:'available'}};
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

    return f"""{page_head("历史菜单 · History", "history", location, role)}
<div class="content">{body}</div>
{PAGE_FOOT}"""


# ============================================================
# HTTP Handler
# ============================================================

class AppHandler(BaseHTTPRequestHandler):
    def request_identity(self):
        session_id, session = session_from_cookie(self.headers.get("Cookie", ""))
        self._session_id = session_id
        self._session_refresh = session_id if session else None
        if session:
            return session["username"], session["role"]
        if os.environ.get("ALLOW_TRUSTED_AUTH_HEADER", "").lower() == "true":
            username = self.headers.get("X-Authenticated-User", "").strip()
            return username, authenticated_role(username)
        return "", "unknown"

    def request_role(self):
        return self.request_identity()[1]

    def client_key(self):
        return self.headers.get("X-Real-IP", "").strip() or self.client_address[0]

    def send_redirect(self, location, session_id=None, clear_session=False):
        self.send_response(303)
        self.send_header("Location", location)
        self.send_header("Cache-Control", "no-store")
        if session_id:
            self.send_header(
                "Set-Cookie",
                f"{SESSION_COOKIE_NAME}={session_id}; Path=/; Max-Age={SESSION_TTL_SECONDS}; Secure; HttpOnly; SameSite=Lax",
            )
        elif clear_session:
            self.send_header(
                "Set-Cookie",
                f"{SESSION_COOKIE_NAME}=; Path=/; Max-Age=0; Secure; HttpOnly; SameSite=Lax",
            )
        self.end_headers()

    def send_session_refresh_header(self):
        refreshed = getattr(self, "_session_refresh", None)
        if refreshed:
            self.send_header(
                "Set-Cookie",
                f"{SESSION_COOKIE_NAME}={refreshed}; Path=/; Max-Age={SESSION_TTL_SECONDS}; Secure; HttpOnly; SameSite=Lax",
            )

    def send_login_page(self, error=""):
        self.send_html(render_login(error))

    def send_forbidden(self):
        self.send_json({"error": "forbidden", "message": "Owner permission required"}, 403)

    def do_GET(self):
        parsed = urlparse(self.path)
        path = parsed.path
        qs = parse_qs(parsed.query)
        location = get_location_from_cookie(self.headers.get("Cookie", ""))
        role = self.request_role()

        if path == "/health":
            status, payload = health_result()
            self.send_json(payload, status)
            return

        # 静态资源
        if path.startswith("/photos/"):
            self.serve_photo(path[8:])
            return

        if path in (
            "/assets/family-menu-logo.png", "/favicon-16x16.png", "/favicon-32x32.png",
            "/favicon-48x48.png", "/favicon.ico", "/apple-touch-icon.png",
        ):
            self.serve_public_asset(path[1:])
            return

        if path in ("/manifest.webmanifest", "/icon-192.png", "/icon-512.png", "/favicon.png"):
            self.serve_pwa_asset(path[1:])
            return

        if path == "/login":
            if role == "worker":
                self.send_redirect("/pantry")
            elif role == "owner":
                self.send_redirect("/tomorrow")
            else:
                self.send_login_page()
            return

        if role == "unknown":
            if path.startswith("/api/"):
                self.send_json({"error": "authentication required"}, 401)
            else:
                self.send_redirect("/login")
            return

        # 页面路由
        if path == "/" and role == "worker":
            self.send_response(302)
            self.send_header("Location", "/pantry")
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
        elif path == "/" or path == "/tomorrow":
            self.send_html(render_tomorrow(role, location))
        elif path == "/pantry":
            self.send_html(render_pantry(role, location))
        elif path == "/pantry/submit":
            # V6: pantry submit 已合并到主页面，重定向
            self.send_response(302)
            self.send_header("Location", "/pantry")
            self.end_headers()
        elif path == "/dishes":
            self.send_html(render_dishes(role, location))
        elif path == "/history":
            self.send_html(render_history(role, location))

        # API 路由
        elif path == "/api/dishes":
            cat = qs.get("category", [None])[0]
            search = qs.get("search", [""])[0]
            dishes = get_all_dishes(category=cat, search=search, location=location)
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
            if role == "owner":
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
        if path == "/login":
            client_key = self.client_key()
            if login_is_rate_limited(client_key):
                self.send_login_page("登录尝试过多，请稍后再试。")
                return
            form = self.read_form()
            username = form.get("username", "").strip()
            password = form.get("password", "")
            role = authenticated_role(username)
            if role == "unknown" or not verify_family_password(username, password):
                record_login_failure(client_key)
                self.send_login_page("账号或密码不正确，请重试。")
                return
            clear_login_failures(client_key)
            session_id = create_session(username, role)
            self.send_redirect("/pantry" if role == "worker" else "/tomorrow", session_id=session_id)
            return

        if path == "/logout":
            session_id, _ = session_from_cookie(self.headers.get("Cookie", ""))
            destroy_session(session_id)
            self.send_redirect("/login", clear_session=True)
            return

        location = get_location_from_cookie(self.headers.get("Cookie", ""))
        username, role = self.request_identity()
        if role == "unknown":
            self.send_json({"error": "authentication required"}, 401)
            return
        if not post_path_allowed(role, path):
            self.send_forbidden()
            return
        if path in {"/api/tomorrow/confirm", "/api/tomorrow/push"}:
            owner_username = os.environ.get("OWNER_AUTH_USERNAME", "").strip()
            if role != "owner" or username != owner_username or username != "vivian":
                self.send_forbidden()
                return
        body = self.read_json()
        if path in MENU_DRAFT_WRITE_PATHS:
            menu_id = body.get("menu_id")
            conn = get_db()
            try:
                menu_row = conn.execute(
                    "SELECT status, location, date FROM menus WHERE id=?", (menu_id,)
                ).fetchone()
            finally:
                conn.close()
            if not menu_row:
                self.send_json({"ok": False, "error": "菜单不存在"}, 404)
                return
            if menu_row["location"] != location:
                self.send_json({"ok": False, "error": "menu location mismatch"}, 403)
                return
            editable, message = ensure_owner_tomorrow_draft(role, menu_id, menu_row)
            if not editable:
                self.send_json({"ok": False, "error": message}, 409)
                return

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

        elif path == "/api/pantry/add-by-name":
            if os.environ.get("LOCAL_PREVIEW_UI", "").lower() != "true":
                self.send_json({"ok": False, "error": "preview only"}, 404)
                return
            raw_name = body.get("ingredient_name", "")
            if not _normalize_ingredient_name(raw_name):
                self.send_json({"ok": False, "error": "ingredient_name required"}, 400)
                return
            loc = body.get("location", location)
            conn = get_db()
            try:
                conn.execute("BEGIN IMMEDIATE")
                ingredient, created = add_or_get_ingredient(conn, raw_name)
                ingredient_id = ingredient["ingredient_id"]

                active = conn.execute(
                    "SELECT 1 FROM current_pantry "
                    "WHERE location = ? AND ingredient_id = ? AND is_active = 1",
                    (loc, ingredient_id),
                ).fetchone()
                if active:
                    conn.rollback()
                    self.send_json({
                        "ok": True, "already_in_pantry": True,
                        **ingredient,
                    })
                    return

                now = datetime.now().isoformat()
                conn.execute(
                    "INSERT INTO current_pantry "
                    "(location, ingredient_id, status, is_active, created_at, updated_at) "
                    "VALUES (?, ?, 'available', 1, ?, ?) "
                    "ON CONFLICT(location, ingredient_id) DO UPDATE SET "
                    "status = 'available', is_active = 1, updated_at = excluded.updated_at",
                    (loc, ingredient_id, now, now),
                )
                _record_pantry_usage(conn, loc, ingredient_id, now, added=True)
                _increment_inventory_version(conn, loc)
                pantry_count = conn.execute(
                    "SELECT COUNT(*) AS count FROM current_pantry WHERE location = ? AND is_active = 1",
                    (loc,),
                ).fetchone()["count"]
                conn.commit()
                _invalidate_availability_cache(loc)
                self.send_json({
                    "ok": True, "already_in_pantry": False, "created": created,
                    **ingredient, "pantry_count": pantry_count,
                })
            except Exception:
                conn.rollback()
                raise
            finally:
                conn.close()

        elif path == "/api/pantry/same-as-last":
            # V6: "和上次一样" — 不修改库存，只记录确认时间
            loc = body.get("location", location)
            result = confirm_pantry_unchanged(loc, body.get("submitted_by", "nanny"))
            self.send_json(result)

        elif path == "/api/pantry/update_status":
            # V4: 单项状态更新
            loc = body.get("location", location)
            result = update_ingredient_status(
                loc, body["ingredient_id"], body["status"],
                submitted_by=body.get("submitted_by", "nanny"),
            )
            self.send_json(result)

        elif path == "/api/pantry/consume":
            # Preview Pantry: consumption is an event, not a persistent stock status.
            if os.environ.get("LOCAL_PREVIEW_UI", "").lower() != "true":
                self.send_json({"ok": False, "error": "preview only"}, 404)
                return
            _ensure_preview_consumed_history()
            loc = body.get("location", location)
            ingredient_id = body["ingredient_id"]
            consumed_at = datetime.now().isoformat()
            conn = get_db()
            try:
                active = conn.execute(
                    "SELECT 1 FROM current_pantry "
                    "WHERE location = ? AND ingredient_id = ? AND is_active = 1",
                    (loc, ingredient_id),
                ).fetchone()
                if not active:
                    self.send_json({"ok": False, "error": "ingredient not in pantry"}, 409)
                    return
                conn.execute(
                    "UPDATE current_pantry SET is_active = 0, updated_at = ? "
                    "WHERE location = ? AND ingredient_id = ? AND is_active = 1",
                    (consumed_at, loc, ingredient_id),
                )
                _record_pantry_usage(conn, loc, ingredient_id, consumed_at, added=False)
                cursor = conn.execute(
                    "INSERT INTO consumed_history "
                    "(location, ingredient_id, consumed_at, consumed_by, source) "
                    "VALUES (?, ?, ?, ?, 'pantry_used_up')",
                    (loc, ingredient_id, consumed_at, body.get("consumed_by", "owner")),
                )
                _increment_inventory_version(conn, loc)
                counts = conn.execute(
                    "SELECT COUNT(*) AS pantry_count, "
                    "SUM(CASE WHEN status='expiring' THEN 1 ELSE 0 END) AS expiring_count "
                    "FROM current_pantry WHERE location=? AND is_active=1",
                    (loc,),
                ).fetchone()
                recent_count = conn.execute(
                    "SELECT COUNT(*) AS count FROM consumed_history WHERE location=? "
                    "AND datetime(consumed_at)>=datetime('now','-30 days')",
                    (loc,),
                ).fetchone()["count"]
                conn.commit()
                _invalidate_availability_cache(loc)
                self.send_json({
                    "ok": True, "consumption_id": cursor.lastrowid, "consumed_at": consumed_at,
                    "pantry_count": counts["pantry_count"],
                    "expiring_count": counts["expiring_count"] or 0,
                    "recently_used_count": recent_count,
                })
            finally:
                conn.close()

        elif path == "/api/pantry/remove":
            # V4: 从当前库存移除单项
            loc = body.get("location", location)
            result = remove_ingredient_from_pantry(
                loc, body["ingredient_id"],
                submitted_by=body.get("submitted_by", "nanny"),
            )
            self.send_json(result)

        elif path == "/api/ingredients/add":
            raw_name = body.get("name") or body.get("name_cn") or body.get("name_en")
            if not _normalize_ingredient_name(raw_name):
                self.send_json({"ok": False, "error": "ingredient name required"}, 400)
                return
            conn = get_db()
            try:
                ingredient, created = add_or_get_ingredient(
                    conn, raw_name, ingredient_group=body.get("ingredient_group", "other"),
                    name_cn=body.get("name_cn", ""), name_en=body.get("name_en", ""),
                )
                conn.commit()
                if created:
                    log_event("ingredient_added", "ingredient", ingredient["ingredient_id"], ingredient)
                self.send_json({"ok": True, "created": created, **ingredient})
            finally:
                conn.close()

        elif path == "/api/ingredients/update":
            conn = get_db()
            try:
                ingredient = update_ingredient_names(
                    conn, body.get("ingredient_id", ""),
                    body.get("name_cn", ""), body.get("name_en", ""),
                )
                conn.commit()
                self.send_json({"ok": True, **ingredient})
            except ValueError as error:
                conn.rollback()
                self.send_json({"ok": False, "error": str(error)}, 400)
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
            validation = validate_menu_after_mutation(body["menu_id"]) if ok else None
            self.send_json({"ok": ok, "error": msg if not ok else None, "validation": validation})

        elif path == "/api/tomorrow/smart-replace":
            ok, msg, replacement_id = smart_replace_menu_item(
                body["menu_id"], body["menu_item_id"], body.get("location", location)
            )
            validation = validate_menu_after_mutation(body["menu_id"]) if ok else None
            self.send_json({
                "ok": ok, "error": msg if not ok else None,
                "new_dish_id": replacement_id, "validation": validation,
            })

        elif path == "/api/tomorrow/ai-fill":
            meal_type = body.get("meal_type")
            ok, msg, review = ai_fill_menu(body["menu_id"], body.get("location", location), meal_type=meal_type)
            self.send_json({"ok": ok, "error": msg if not ok else None, "review": review})

        elif path == "/api/tomorrow/repair":
            ok, msg, review = repair_menu(body["menu_id"], body.get("location", location), seed=body.get("seed"))
            self.send_json({"ok": ok, "error": msg if not ok else None, "review": review})

        elif path == "/api/tomorrow/confirm":
            ok, msg, warnings, transitioned = confirm_menu(
                body["menu_id"], triggered_by=username,
                expected_location=location, include_transition=True,
            )
            if ok and transitioned:
                from push_service import push_confirmed_menu, push_on_confirm_is_enabled
                if push_on_confirm_is_enabled():
                    push_ok, push_msg = push_confirmed_menu(body["menu_id"], triggered_by=username)
                else:
                    push_ok, push_msg = False, "菜单已确认；确认后即时推送未启用"
                self.send_json({
                    "ok": True, "confirmed": True, "transitioned": True, "pushed": push_ok,
                    "push_failed": push_on_confirm_is_enabled() and not push_ok,
                    "error": None,
                    "warnings": warnings,
                    "message": push_msg if push_ok else "菜单已确认，但推送失败" if push_on_confirm_is_enabled() else push_msg,
                })
            elif ok:
                self.send_json({
                    "ok": True, "confirmed": True, "transitioned": False,
                    "pushed": False, "push_failed": False, "error": None,
                    "warnings": warnings, "message": msg,
                })
            else:
                self.send_json({"ok": False, "confirmed": False, "pushed": False,
                                "error": msg, "warnings": warnings, "message": msg})

        elif path == "/api/tomorrow/revert":
            # V3: Confirmed → Edit Menu → Reconfirm flow
            ok, msg = revert_to_draft(body["menu_id"])
            self.send_json({"ok": ok, "error": msg if not ok else None, "message": msg})

        elif path == "/api/tomorrow/push":
            # 仅 Vivian Owner 可对同一确认版本手动重试失败记录。
            from push_service import push_confirmed_menu
            ok, msg = push_confirmed_menu(body["menu_id"], triggered_by=username, allow_retry=True)
            self.send_json({"ok": ok, "error": msg if not ok else None, "message": msg})

        elif path == "/api/purchase/update":
            update_purchase_status(body["id"], body["status"], resolved_by=body.get("by", "nanny"))
            self.send_json({"ok": True})

        elif path == "/api/tomorrow/diners":
            diners = body.get("diners")
            if not isinstance(diners, list) or not diners:
                self.send_json({"ok": False, "error": "至少保留一名用餐成员"}, 400)
                return
            diners = list(dict.fromkeys(str(value) for value in diners))
            valid_diner_ids = {item["id"] for item in get_all_diners()}
            if any(diner_id not in valid_diner_ids for diner_id in diners):
                self.send_json({"ok": False, "error": "invalid diner"}, 400)
                return
            ok = update_menu_diners(body["menu_id"], diners)
            if ok:
                # V10: Diners 变化后自动 Reconcile AI 菜品
                try:
                    from menu_service import reconcile_meal_for_diners
                    reconcile_ok, reconcile_msg, review = reconcile_meal_for_diners(
                        body["menu_id"], location=body.get("location", "shenzhen")
                    )
                    self.send_json({"ok": True, "reconciled": True, "message": reconcile_msg})
                except Exception as e:
                    self.send_json({"ok": True, "reconcile_error": str(e)})
            else:
                self.send_json({"ok": False})

        elif path == "/api/tomorrow/meal-mode":
            # V11: 设置 Meal Mode (daily/banquet) + banquet_total_diners
            meal_mode = body.get("meal_mode", "daily")
            banquet_total = body.get("banquet_total_diners")
            if meal_mode not in ("daily", "banquet"):
                self.send_json({"ok": False, "error": "invalid meal_mode"}, 400)
                return
            if meal_mode == "banquet":
                try:
                    banquet_total = int(banquet_total)
                except (TypeError, ValueError):
                    self.send_json({"ok": False, "error": "banquet_total_diners required"}, 400)
                    return
                if not 2 <= banquet_total <= 30:
                    self.send_json({"ok": False, "error": "家宴人数必须为 2–30"}, 400)
                    return
            else:
                banquet_total = None
            ok = update_menu_meal_mode(body["menu_id"], meal_mode, banquet_total)
            if ok:
                # V11: Meal Mode 变化后自动 Reconcile
                try:
                    from menu_service import reconcile_meal_for_diners
                    reconcile_ok, reconcile_msg, review = reconcile_meal_for_diners(
                        body["menu_id"], location=body.get("location", "shenzhen")
                    )
                    self.send_json({"ok": True, "reconciled": True, "message": reconcile_msg})
                except Exception as e:
                    self.send_json({"ok": True, "reconcile_error": str(e)})
            else:
                self.send_json({"ok": False})

        elif path == "/api/meal-plan/meal-diners":
            meal_type = body.get("meal_type", "")
            if body.get("inherit") is True:
                ok, message = update_meal_setting(
                    body["menu_id"], meal_type, diners_marker=True, diners=None
                )
            else:
                diners = body.get("diners")
                if not isinstance(diners, list) or not diners:
                    self.send_json({"ok": False, "error": "至少选择一名用餐成员"}, 400)
                    return
                diners = list(dict.fromkeys(str(value) for value in diners))
                valid_ids = {item["id"] for item in get_all_diners()}
                if any(diner_id not in valid_ids for diner_id in diners):
                    self.send_json({"ok": False, "error": "invalid diner"}, 400)
                    return
                ok, message = update_meal_setting(
                    body["menu_id"], meal_type, diners_marker=True, diners=diners
                )
            self.send_json({"ok": ok, "message": message})

        elif path == "/api/meal-plan/note":
            note = body.get("note", "")
            if not isinstance(note, str):
                self.send_json({"ok": False, "error": "invalid note"}, 400)
                return
            ok, message = update_meal_setting(
                body["menu_id"], body.get("meal_type", ""), note_marker=True, note=note
            )
            self.send_json({"ok": ok, "message": message})

        elif path == "/api/meal-plan/meal-state":
            if not isinstance(body.get("is_skipped"), bool):
                self.send_json({"ok": False, "error": "is_skipped must be boolean"}, 400)
                return
            ok, message = update_meal_setting(
                body["menu_id"], body.get("meal_type", ""),
                skipped_marker=True, skipped=body["is_skipped"],
            )
            self.send_json({"ok": ok, "message": message})

        elif path == "/api/meal-plan/clear-meal":
            ok, count = clear_menu_meal(body["menu_id"], body.get("meal_type", ""))
            self.send_json({"ok": ok, "cleared": count if ok else 0,
                            "error": None if ok else count})

        else:
            self.send_error(404, "Not Found")

    def serve_photo(self, filename):
        try:
            filepath = resolve_photo_path(PHOTOS_DIR, filename)
        except PhotoValidationError:
            self.send_error(404, "Photo not found")
            return
        if os.path.isfile(filepath):
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

    def serve_pwa_asset(self, filename):
        allowed = {
            "manifest.webmanifest": "application/manifest+json; charset=utf-8",
            "apple-touch-icon.png": "image/png",
            "icon-192.png": "image/png",
            "icon-512.png": "image/png",
            "favicon.png": "image/png",
        }
        content_type = allowed.get(filename)
        filepath = os.path.join(PWA_DIR, filename)
        if not content_type or not os.path.isfile(filepath):
            self.send_error(404, "Not Found")
            return
        with open(filepath, "rb") as f:
            data = f.read()
        self.send_response(200)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "public, max-age=86400" if filename.endswith(".png") else "no-cache")
        self.end_headers()
        self.wfile.write(data)

    def serve_public_asset(self, filename):
        allowed = {
            "assets/family-menu-logo.png": "image/png",
            "favicon-16x16.png": "image/png",
            "favicon-32x32.png": "image/png",
            "favicon-48x48.png": "image/png",
            "apple-touch-icon.png": "image/png",
            "favicon.ico": "image/x-icon",
        }
        content_type = allowed.get(filename)
        filepath = os.path.join(PUBLIC_DIR, filename)
        if not content_type or not os.path.isfile(filepath):
            self.send_error(404, "Not Found")
            return
        with open(filepath, "rb") as handle:
            data = handle.read()
        self.send_response(200)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "public, max-age=86400")
        self.end_headers()
        self.wfile.write(data)

    def send_html(self, content):
        data = content.encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Cache-Control", "no-store")
        self.send_session_refresh_header()
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def send_json(self, obj, status=200):
        data = json.dumps(obj, ensure_ascii=False, default=str).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Cache-Control", "no-store")
        self.send_session_refresh_header()
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def read_json(self):
        length = int(self.headers.get("Content-Length", 0))
        if length:
            return json.loads(self.rfile.read(length))
        return {}

    def read_form(self):
        try:
            length = int(self.headers.get("Content-Length", 0))
        except ValueError:
            return {}
        if length <= 0 or length > 4096:
            return {}
        content_type = self.headers.get("Content-Type", "").split(";", 1)[0].strip().lower()
        if content_type != "application/x-www-form-urlencoded":
            return {}
        try:
            raw = self.rfile.read(length).decode("utf-8")
        except UnicodeDecodeError:
            return {}
        parsed = parse_qs(raw, keep_blank_values=True, max_num_fields=8)
        return {key: values[-1] for key, values in parsed.items() if values}

    def log_message(self, *args):
        pass


def main():
    validate_app_startup()
    # 确保明天菜单存在
    ensure_tomorrow_menu("shenzhen")
    server = ThreadingHTTPServer((HOST, PORT), AppHandler)
    print(f"[OK] H5 应用已启动: http://{HOST}:{PORT}")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\n[BYE]")
        server.shutdown()


if __name__ == "__main__":
    main()
