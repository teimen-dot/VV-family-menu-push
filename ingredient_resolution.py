"""Deterministic ingredient identity, aliases, pending rows, and safe merges."""

import hashlib
import json
import re
import unicodedata
from datetime import datetime


SALAD_ALIASES = ("沙拉菜", "salad", "salad greens", "salad leaves", "mixed salad greens")
MAITAKE_ALIASES = (
    "舞茸", "maitake", "maitake mush", "maitake mushroom",
    "japan maitake mush", "japanese maitake mushroom",
)


def normalize_key(value):
    text = unicodedata.normalize("NFKC", str(value or ""))
    text = " ".join(text.strip().split()).casefold()
    return re.sub(r"\s*([,，;；、])\s*", r"\1", text)


def ensure_resolution_schema(conn):
    conn.execute("""
        CREATE TABLE IF NOT EXISTS ingredient_aliases (
            alias_key TEXT PRIMARY KEY,
            alias_text TEXT NOT NULL,
            ingredient_id TEXT NOT NULL,
            alias_type TEXT NOT NULL DEFAULT 'alias',
            created_at TEXT DEFAULT (datetime('now')),
            FOREIGN KEY (ingredient_id) REFERENCES ingredients(ingredient_id)
        )
    """)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS pending_ingredients (
            pending_id TEXT PRIMARY KEY,
            normalized_key TEXT NOT NULL UNIQUE,
            raw_input TEXT NOT NULL,
            detected_language TEXT NOT NULL,
            status TEXT NOT NULL DEFAULT 'pending',
            resolved_ingredient_id TEXT,
            created_at TEXT DEFAULT (datetime('now')),
            updated_at TEXT DEFAULT (datetime('now')),
            FOREIGN KEY (pending_id) REFERENCES ingredients(ingredient_id),
            FOREIGN KEY (resolved_ingredient_id) REFERENCES ingredients(ingredient_id)
        )
    """)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS ingredient_dictionary_metadata (
            ingredient_id TEXT PRIMARY KEY,
            status TEXT NOT NULL DEFAULT 'canonical',
            updated_at TEXT DEFAULT (datetime('now')),
            FOREIGN KEY (ingredient_id) REFERENCES ingredients(ingredient_id)
        )
    """)


def _language(value):
    return "zh" if re.search(r"[\u3400-\u9fff]", value) else "en"


def _table_exists(conn, table):
    return bool(conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (table,)
    ).fetchone())


def register_alias(conn, ingredient_id, alias_text, alias_type="alias"):
    key = normalize_key(alias_text)
    if not key:
        return
    existing = conn.execute(
        "SELECT ingredient_id FROM ingredient_aliases WHERE alias_key=?", (key,)
    ).fetchone()
    if existing and existing["ingredient_id"] != ingredient_id:
        raise ValueError(f"alias already belongs to {existing['ingredient_id']}: {alias_text}")
    if existing:
        return
    conn.execute(
        "INSERT INTO ingredient_aliases(alias_key,alias_text,ingredient_id,alias_type) "
        "VALUES(?,?,?,?) ON CONFLICT(alias_key) DO UPDATE SET "
        "alias_text=excluded.alias_text,alias_type=excluded.alias_type",
        (key, alias_text, ingredient_id, alias_type),
    )


def backfill_aliases(conn):
    ensure_resolution_schema(conn)
    for row in conn.execute(
        "SELECT ingredient_id,name_cn,name_en,aliases FROM ingredients "
        "WHERE ingredient_id NOT LIKE 'pending_%'"
    ).fetchall():
        values = [(row["ingredient_id"], "id"), (row["name_cn"], "name_cn"),
                  (row["name_en"], "name_en")]
        try:
            aliases = json.loads(row["aliases"] or "[]")
        except (TypeError, json.JSONDecodeError):
            aliases = []
        values.extend((alias, "alias") for alias in aliases)
        for value, kind in values:
            if value:
                try:
                    register_alias(conn, row["ingredient_id"], value, kind)
                except ValueError:
                    # Existing ambiguous legacy values remain unresolved rather than guessed.
                    pass
    salad = conn.execute(
        "SELECT 1 FROM ingredients WHERE ingredient_id='沙拉菜'"
    ).fetchone()
    if salad:
        for alias in SALAD_ALIASES:
            register_alias(conn, "沙拉菜", alias, "seed")
    maitake = conn.execute(
        "SELECT 1 FROM ingredients WHERE ingredient_id='maitake'"
    ).fetchone()
    if maitake:
        for alias in MAITAKE_ALIASES:
            register_alias(conn, "maitake", alias, "seed")


def resolve_ingredient_input(conn, raw_input, allow_pending=True):
    ensure_resolution_schema(conn)
    raw = unicodedata.normalize("NFKC", str(raw_input or "")).strip()
    key = normalize_key(raw)
    if not key:
        raise ValueError("ingredient name required")
    row = conn.execute(
        "SELECT a.ingredient_id,i.name_cn,i.name_en FROM ingredient_aliases a "
        "JOIN ingredients i ON i.ingredient_id=a.ingredient_id WHERE a.alias_key=?",
        (key,),
    ).fetchone()
    if row:
        return {"ingredient_id": row["ingredient_id"], "name_cn": row["name_cn"],
                "name_en": row["name_en"] or "", "resolution_status": "canonical"}
    # A common English typing variation inserts/removes spaces or hyphens
    # ("blue berry" / "blue-berry" / "blueberry").  Resolve it only when the
    # compact Latin form has exactly one canonical owner; ambiguity stays pending.
    if re.fullmatch(r"[a-z0-9\s-]+", key):
        compact = re.sub(r"[\s-]+", "", key)
        owners = {}
        for alias in conn.execute(
            "SELECT a.alias_key,a.ingredient_id,i.name_cn,i.name_en "
            "FROM ingredient_aliases a JOIN ingredients i ON i.ingredient_id=a.ingredient_id"
        ).fetchall():
            if re.sub(r"[\s-]+", "", alias["alias_key"]) == compact:
                owners[alias["ingredient_id"]] = alias
        if len(owners) == 1:
            matched = next(iter(owners.values()))
            return {"ingredient_id": matched["ingredient_id"], "name_cn": matched["name_cn"],
                    "name_en": matched["name_en"] or "", "resolution_status": "canonical"}
    pending = conn.execute(
        "SELECT p.pending_id,i.name_cn,i.name_en FROM pending_ingredients p "
        "JOIN ingredients i ON i.ingredient_id=p.pending_id "
        "WHERE p.normalized_key=? AND p.status='pending'", (key,)
    ).fetchone()
    if pending:
        return {"ingredient_id": pending["pending_id"], "name_cn": pending["name_cn"],
                "name_en": pending["name_en"] or "", "resolution_status": "pending"}
    if not allow_pending:
        return None
    pending_id = "pending_" + hashlib.sha256(key.encode("utf-8")).hexdigest()[:20]
    language = _language(raw)
    name_cn, name_en = (raw, "") if language == "zh" else ("", raw)
    conn.execute(
        "INSERT OR IGNORE INTO ingredients(ingredient_id,name_cn,name_en,aliases,category,ingredient_group,is_common) "
        "VALUES(?,?,?,'[]','','other',0)", (pending_id, name_cn, name_en),
    )
    conn.execute(
        "INSERT OR IGNORE INTO pending_ingredients"
        "(pending_id,normalized_key,raw_input,detected_language,status) VALUES(?,?,?,?, 'pending')",
        (pending_id, key, raw, language),
    )
    return {"ingredient_id": pending_id, "name_cn": name_cn, "name_en": name_en,
            "resolution_status": "pending"}


def list_pending(conn):
    ensure_resolution_schema(conn)
    return [dict(row) for row in conn.execute(
        "SELECT p.*,i.name_cn,i.name_en FROM pending_ingredients p "
        "JOIN ingredients i ON i.ingredient_id=p.pending_id "
        "WHERE p.status='pending' ORDER BY p.created_at"
    ).fetchall()]


def merge_ingredient(conn, source_id, target_id):
    if source_id == target_id:
        return {"already_merged": True}
    if not conn.execute("SELECT 1 FROM ingredients WHERE ingredient_id=?", (target_id,)).fetchone():
        raise ValueError("target ingredient not found")
    counts = {}
    # Unique dish links and classifications are copied, then source rows removed.
    for table, extra, columns in (
        ("dish_ingredients", "dish_id,required", "dish_id,ingredient_id,required"),
        ("ingredient_classifications", "class_id", "ingredient_id,class_id"),
    ):
        rows = conn.execute(f"SELECT {extra} FROM {table} WHERE ingredient_id=?", (source_id,)).fetchall()
        for row in rows:
            if table == "dish_ingredients":
                conn.execute("INSERT OR IGNORE INTO dish_ingredients(dish_id,ingredient_id,required) VALUES(?,?,?)",
                             (row["dish_id"], target_id, row["required"]))
            else:
                conn.execute("INSERT OR IGNORE INTO ingredient_classifications(ingredient_id,class_id) VALUES(?,?)",
                             (target_id, row["class_id"]))
        counts[table] = len(rows)
        conn.execute(f"DELETE FROM {table} WHERE ingredient_id=?", (source_id,))
        if table == "dish_ingredients":
            for row in rows:
                unresolved = conn.execute(
                    "SELECT 1 FROM dish_ingredients di JOIN pending_ingredients p "
                    "ON p.pending_id=di.ingredient_id "
                    "WHERE di.dish_id=? AND p.status='pending' LIMIT 1", (row["dish_id"],)
                ).fetchone()
                conn.execute(
                    "UPDATE dishes SET ingredients_pending=?,needs_review=? WHERE id=?",
                    (1 if unresolved else 0, 1 if unresolved else 0, row["dish_id"]),
                )
    # Current pantry has a location uniqueness constraint.
    pantry_rows = conn.execute("SELECT * FROM current_pantry WHERE ingredient_id=?", (source_id,)).fetchall()
    for row in pantry_rows:
        target = conn.execute("SELECT * FROM current_pantry WHERE location=? AND ingredient_id=?",
                              (row["location"], target_id)).fetchone()
        if target:
            candidates = [r for r in (target, row) if r["is_active"]] or [target, row]
            newest = max(candidates, key=lambda r: r["updated_at"] or "")
            conn.execute("UPDATE current_pantry SET status=?,quantity_level=?,is_active=?,"
                         "created_at=?,updated_at=? WHERE id=?", (
                             newest["status"], newest["quantity_level"],
                             1 if target["is_active"] or row["is_active"] else 0,
                             min(filter(None, (target["created_at"], row["created_at"]))),
                             max(filter(None, (target["updated_at"], row["updated_at"]))), target["id"]))
            conn.execute("DELETE FROM current_pantry WHERE id=?", (row["id"],))
        else:
            conn.execute("UPDATE current_pantry SET ingredient_id=? WHERE id=?", (target_id, row["id"]))
    counts["current_pantry"] = len(pantry_rows)
    for table in ("inventory_items", "purchase_requests"):
        cursor = conn.execute(f"UPDATE {table} SET ingredient_id=? WHERE ingredient_id=?",
                              (target_id, source_id))
        counts[table] = cursor.rowcount
    usage_rows = conn.execute(
        "SELECT location,add_count,last_action_at FROM pantry_usage_stats WHERE ingredient_id=?",
        (source_id,),
    ).fetchall() if _table_exists(conn, "pantry_usage_stats") else []
    for row in usage_rows:
        target = conn.execute(
            "SELECT add_count,last_action_at FROM pantry_usage_stats WHERE location=? AND ingredient_id=?",
            (row["location"], target_id),
        ).fetchone()
        if target:
            conn.execute(
                "UPDATE pantry_usage_stats SET add_count=?,last_action_at=? WHERE location=? AND ingredient_id=?",
                (target["add_count"] + row["add_count"],
                 max(target["last_action_at"] or "", row["last_action_at"] or ""),
                 row["location"], target_id),
            )
            conn.execute("DELETE FROM pantry_usage_stats WHERE location=? AND ingredient_id=?",
                         (row["location"], source_id))
        else:
            conn.execute("UPDATE pantry_usage_stats SET ingredient_id=? WHERE location=? AND ingredient_id=?",
                         (target_id, row["location"], source_id))
    counts["pantry_usage_stats"] = len(usage_rows)
    if _table_exists(conn, "consumed_history"):
        cursor = conn.execute("UPDATE consumed_history SET ingredient_id=? WHERE ingredient_id=?",
                              (target_id, source_id))
        counts["consumed_history"] = cursor.rowcount
    else:
        counts["consumed_history"] = 0
    conn.execute("UPDATE pending_ingredients SET resolved_ingredient_id=? WHERE resolved_ingredient_id=?",
                 (target_id, source_id))
    conn.execute("UPDATE pending_ingredients SET status='resolved',resolved_ingredient_id=?,"
                 "updated_at=datetime('now') WHERE pending_id=?", (target_id, source_id))
    conn.execute("DELETE FROM ingredient_aliases WHERE ingredient_id=?", (source_id,))
    conn.execute("DELETE FROM ingredient_dictionary_metadata WHERE ingredient_id=?", (source_id,))
    remaining = sum(conn.execute(f"SELECT COUNT(*) FROM {table} WHERE ingredient_id=?", (source_id,)).fetchone()[0]
                    for table in ("dish_ingredients", "current_pantry", "inventory_items", "purchase_requests",
                                  "ingredient_classifications", "pantry_usage_stats") if _table_exists(conn, table))
    is_pending = conn.execute(
        "SELECT 1 FROM pending_ingredients WHERE pending_id=?", (source_id,)
    ).fetchone()
    if remaining == 0 and not is_pending:
        conn.execute("DELETE FROM ingredients WHERE ingredient_id=?", (source_id,))
    return counts


class NullAIResolver:
    enabled = False

    def resolve(self, raw_input, candidates):
        return None
