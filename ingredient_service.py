"""Exact-match bilingual ingredient creation and maintenance."""

import hashlib
import re
from datetime import datetime
from db import INGREDIENT_EN_NAMES


KNOWN_TRANSLATIONS = {
    "三文鱼": "Salmon",
    "三文鱼籽": "Salmon Roe",
    "鸡蛋": "Egg",
    "豆腐": "Tofu",
    "包子": "Baozi",
    "煎饺": "Pan-fried Dumplings",
    "salmon": "三文鱼",
    "salmon roe": "三文鱼籽",
    "egg": "鸡蛋",
    "tofu": "豆腐",
    "baozi": "包子",
    "pan-fried dumplings": "煎饺",
}


def _translation_maps():
    cn_to_en = {
        normalize_name(key): normalize_name(value)
        for key, value in INGREDIENT_EN_NAMES.items()
        if _has_cjk(str(key)) and normalize_name(value)
    }
    for key, value in KNOWN_TRANSLATIONS.items():
        if _has_cjk(key):
            cn_to_en[normalize_name(key)] = normalize_name(value)
    en_to_cn = {value.casefold(): key for key, value in cn_to_en.items()}
    for key, value in KNOWN_TRANSLATIONS.items():
        if not _has_cjk(key) and _has_cjk(value):
            en_to_cn[normalize_name(key).casefold()] = normalize_name(value)
    return cn_to_en, en_to_cn


def normalize_name(value):
    return " ".join(str(value or "").strip().split())


def _has_cjk(value):
    return bool(re.search(r"[\u3400-\u9fff]", value))


def _has_latin(value):
    return bool(re.search(r"[A-Za-z]", value))


def complete_bilingual_names(name_cn="", name_en=""):
    """Complete one missing language from the existing free bilingual lexicon."""
    name_cn = normalize_name(name_cn)
    name_en = normalize_name(name_en)
    if not name_cn and not name_en:
        raise ValueError("ingredient name required")
    cn_to_en, en_to_cn = _translation_maps()
    if name_cn and name_en and name_cn.casefold() != name_en.casefold():
        return name_cn, name_en, 0
    original = name_cn or name_en
    if _has_cjk(original) and not _has_latin(original):
        translated = cn_to_en.get(original)
        return original, translated or "", 0 if translated else 1
    if _has_latin(original) and not _has_cjk(original):
        translated = en_to_cn.get(original.casefold())
        return translated or original, original, 0 if translated else 1
    return name_cn or original, name_en or original, 1


def bilingual_names(raw_name):
    original = normalize_name(raw_name)
    if _has_cjk(original):
        return complete_bilingual_names(name_cn=original)
    return complete_bilingual_names(name_en=original)


def add_or_get_ingredient(
    conn, raw_name=None, category="", ingredient_group="other", name_cn="", name_en=""
):
    """Create by exact full-name equality only; similar ingredients remain distinct."""
    provided_cn = normalize_name(name_cn)
    provided_en = normalize_name(name_en)
    original = normalize_name(raw_name) or provided_cn or provided_en
    if not original:
        raise ValueError("ingredient name required")
    exact_key = original.casefold()
    rows = conn.execute(
        "SELECT ingredient_id,name_cn,name_en,translation_pending FROM ingredients"
    ).fetchall()
    for row in rows:
        if normalize_name(row["name_cn"]).casefold() == exact_key or (
            row["name_en"] and normalize_name(row["name_en"]).casefold() == exact_key
        ):
            current = dict(row)
            completed_cn, completed_en, pending = complete_bilingual_names(
                provided_cn or current["name_cn"], provided_en or current["name_en"]
            )
            if (completed_cn, completed_en, pending) != (
                current["name_cn"], current["name_en"] or "", current["translation_pending"] or 0
            ):
                conn.execute(
                    "UPDATE ingredients SET name_cn=?,name_en=?,translation_pending=?,updated_at=? "
                    "WHERE ingredient_id=?",
                    (completed_cn, completed_en, pending, datetime.now().isoformat(), current["ingredient_id"]),
                )
                current.update(name_cn=completed_cn, name_en=completed_en, translation_pending=pending)
            return current, False

    if provided_cn or provided_en:
        name_cn, name_en, pending = complete_bilingual_names(provided_cn, provided_en)
    else:
        name_cn, name_en, pending = bilingual_names(original)
    digest = hashlib.sha256(exact_key.encode("utf-8")).hexdigest()[:16]
    ingredient_id = f"custom_{digest}"
    conn.execute(
        "INSERT INTO ingredients "
        "(ingredient_id,name_cn,name_en,aliases,category,ingredient_group,translation_pending,updated_at) "
        "VALUES(?,?,?,'[]',?,?,?,?)",
        (ingredient_id, name_cn, name_en, category, ingredient_group, pending, datetime.now().isoformat()),
    )
    return {
        "ingredient_id": ingredient_id, "name_cn": name_cn, "name_en": name_en,
        "translation_pending": pending,
    }, True


def update_ingredient_names(conn, ingredient_id, name_cn, name_en):
    name_cn = normalize_name(name_cn)
    name_en = normalize_name(name_en)
    if not name_cn or not name_en:
        raise ValueError("both bilingual names are required")
    cursor = conn.execute(
        "UPDATE ingredients SET name_cn=?,name_en=?,translation_pending=0,updated_at=? "
        "WHERE ingredient_id=?",
        (name_cn, name_en, datetime.now().isoformat(), ingredient_id),
    )
    if cursor.rowcount != 1:
        raise ValueError("ingredient not found")
    return {"ingredient_id": ingredient_id, "name_cn": name_cn, "name_en": name_en,
            "translation_pending": 0}
