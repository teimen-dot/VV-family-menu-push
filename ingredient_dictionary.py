"""Owner-maintained canonical ingredient dictionary and dependency-free XLSX I/O."""

import hashlib
import io
import json
import re
import secrets
import time
import zipfile
from xml.etree import ElementTree as ET

from ingredient_resolution import (
    ensure_resolution_schema, merge_ingredient, normalize_key, register_alias,
)


HEADERS = ("ingredient_id", "标准中文名", "标准英文名", "别名", "状态", "校验结果")
PREVIEW_TTL = 15 * 60
_PREVIEWS = {}


def _invalidate_runtime_caches():
    from inventory import invalidate_all_availability_cache
    from menu_service import invalidate_catalog_cache
    invalidate_all_availability_cache()
    invalidate_catalog_cache()


def _aliases(value):
    if isinstance(value, list):
        values = value
    else:
        values = re.split(r"[\n,，;；、]+", str(value or ""))
    result = []
    seen = set()
    for value in values:
        value = " ".join(str(value).strip().split())
        key = normalize_key(value)
        if value and key not in seen:
            seen.add(key)
            result.append(value)
    return result


def dictionary_version(conn):
    ensure_resolution_schema(conn)
    rows = conn.execute(
        "SELECT i.ingredient_id,i.name_cn,i.name_en,a.alias_key,a.ingredient_id AS alias_owner,"
        "COALESCE(m.status,'canonical') AS dictionary_status "
        "FROM ingredients i LEFT JOIN ingredient_aliases a ON a.ingredient_id=i.ingredient_id "
        "LEFT JOIN ingredient_dictionary_metadata m ON m.ingredient_id=i.ingredient_id "
        "WHERE i.ingredient_id NOT LIKE 'pending_%' ORDER BY i.ingredient_id,a.alias_key"
    ).fetchall()
    payload = [tuple(row) for row in rows]
    return hashlib.sha256(json.dumps(payload, ensure_ascii=False).encode()).hexdigest()


def list_dictionary(conn):
    ensure_resolution_schema(conn)
    rows = conn.execute(
        "SELECT i.ingredient_id,i.name_cn,i.name_en,i.aliases," 
        "COALESCE(m.status,'canonical') AS status "
        "FROM ingredients i LEFT JOIN pending_ingredients p ON p.pending_id=i.ingredient_id "
        "LEFT JOIN ingredient_dictionary_metadata m ON m.ingredient_id=i.ingredient_id "
        "WHERE i.ingredient_id NOT LIKE 'pending_%' OR p.status='canonical' "
        "ORDER BY i.name_cn COLLATE NOCASE,i.name_en COLLATE NOCASE"
    ).fetchall()
    result = []
    for row in rows:
        aliases = [r[0] for r in conn.execute(
            "SELECT alias_text FROM ingredient_aliases WHERE ingredient_id=? "
            "AND alias_type NOT IN ('id','name_cn','name_en') ORDER BY alias_key",
            (row["ingredient_id"],),
        ).fetchall()]
        result.append({"ingredient_id": row["ingredient_id"], "name_cn": row["name_cn"] or "",
                       "name_en": row["name_en"] or "", "aliases": aliases,
                       "status": row["status"]})
    return result


def _new_id(name_cn, name_en, occupied):
    seed = normalize_key(name_cn) + "\0" + normalize_key(name_en)
    base = "ingredient_" + hashlib.sha256(seed.encode()).hexdigest()[:16]
    candidate, suffix = base, 1
    while candidate in occupied:
        suffix += 1
        candidate = f"{base}_{suffix}"
    occupied.add(candidate)
    return candidate


def validate_rows(conn, rows):
    current = {row["ingredient_id"]: row for row in list_dictionary(conn)}
    occupied = set(current)
    alias_owners = {row["alias_key"]: row["ingredient_id"] for row in conn.execute(
        "SELECT alias_key,ingredient_id FROM ingredient_aliases"
    ).fetchall()}
    proposed_owners = {}
    results, errors = [], []
    seen_ids = set()
    for index, source in enumerate(rows, start=2):
        ingredient_id = str(source.get("ingredient_id") or "").strip()
        name_cn = " ".join(str(source.get("name_cn") or "").strip().split())
        name_en = " ".join(str(source.get("name_en") or "").strip().split())
        aliases = _aliases(source.get("aliases"))
        status = str(source.get("status") or "canonical").strip().casefold()
        row_errors = []
        if status not in ("canonical", "default", "rule"):
            row_errors.append("状态必须是 canonical、default 或 rule")
        if ingredient_id and ingredient_id not in current:
            row_errors.append("ingredient_id 不存在；新增行请留空")
        if ingredient_id in seen_ids:
            row_errors.append("ingredient_id 在表格中重复")
        if not ingredient_id:
            ingredient_id = _new_id(name_cn, name_en, occupied)
        seen_ids.add(ingredient_id)
        before = current.get(ingredient_id)
        action = "create" if not before else (
            "unchanged" if name_cn == before["name_cn"] and name_en == before["name_en"]
            and set(map(normalize_key, aliases)) == set(map(normalize_key, before["aliases"]))
            and status == before["status"] else "update")
        # Legacy rows may be incomplete or ambiguous. An untouched exported row
        # must remain round-trippable; strict checks apply only to actual edits.
        if action != "unchanged":
            if not name_cn or not name_en:
                row_errors.append("标准中文名和英文名均为必填")
            for text in [name_cn, name_en, *aliases]:
                key = normalize_key(text)
                owner = proposed_owners.get(key) or alias_owners.get(key)
                if owner and owner != ingredient_id:
                    row_errors.append(f"名称或别名“{text}”与 {owner} 冲突")
                elif key:
                    proposed_owners[key] = ingredient_id
        result = {"row": index, "ingredient_id": ingredient_id, "name_cn": name_cn,
                  "name_en": name_en, "aliases": aliases, "status": status, "action": action,
                  "errors": row_errors}
        results.append(result)
        errors.extend({"row": index, "message": message} for message in row_errors)
    return results, errors


def apply_rows(conn, rows):
    results, errors = validate_rows(conn, rows)
    if errors:
        raise ValueError("dictionary validation failed")
    conn.execute("BEGIN IMMEDIATE")
    try:
        for row in results:
            ingredient_id = row["ingredient_id"]
            if row["action"] == "create":
                conn.execute("INSERT INTO ingredients(ingredient_id,name_cn,name_en,aliases,category,ingredient_group,is_common) "
                             "VALUES(?,?,?,?,'','other',0)",
                             (ingredient_id, row["name_cn"], row["name_en"],
                              json.dumps(row["aliases"], ensure_ascii=False)))
            elif row["action"] == "update":
                conn.execute("UPDATE ingredients SET name_cn=?,name_en=?,aliases=? WHERE ingredient_id=?",
                             (row["name_cn"], row["name_en"],
                              json.dumps(row["aliases"], ensure_ascii=False), ingredient_id))
            if row["action"] != "unchanged":
                conn.execute("DELETE FROM ingredient_aliases WHERE ingredient_id=?", (ingredient_id,))
                register_alias(conn, ingredient_id, ingredient_id, "id")
                register_alias(conn, ingredient_id, row["name_cn"], "name_cn")
                register_alias(conn, ingredient_id, row["name_en"], "name_en")
                for alias in row["aliases"]:
                    register_alias(conn, ingredient_id, alias, "owner")
            conn.execute(
                "INSERT INTO ingredient_dictionary_metadata(ingredient_id,status,updated_at) "
                "VALUES(?,?,datetime('now')) ON CONFLICT(ingredient_id) DO UPDATE SET "
                "status=excluded.status,updated_at=excluded.updated_at",
                (ingredient_id, row["status"]),
            )
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    _invalidate_runtime_caches()
    return results


def create_preview(conn, rows):
    results, errors = validate_rows(conn, rows)
    token = secrets.token_urlsafe(24)
    now = time.time()
    _PREVIEWS[token] = {"created": now, "version": dictionary_version(conn), "rows": rows}
    for key in list(_PREVIEWS):
        if now - _PREVIEWS[key]["created"] > PREVIEW_TTL:
            _PREVIEWS.pop(key, None)
    counts = {key: sum(row["action"] == key for row in results) for key in ("create", "update", "unchanged")}
    return {"ok": not errors, "preview_token": token, "items": results,
            "errors": errors, "counts": counts}


def commit_preview(conn, token):
    preview = _PREVIEWS.pop(str(token or ""), None)
    if not preview or time.time() - preview["created"] > PREVIEW_TTL:
        raise ValueError("预检已过期，请重新上传")
    if dictionary_version(conn) != preview["version"]:
        raise ValueError("食材词典已发生变化，请重新上传并预检")
    return apply_rows(conn, preview["rows"])


def _xml_text(value):
    return str(value or "").replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


def export_xlsx(rows):
    sheet_rows = [HEADERS] + [(
        row["ingredient_id"], row["name_cn"], row["name_en"], "\n".join(row["aliases"]),
        row["status"], "",
    ) for row in rows]
    xml_rows = []
    for number, values in enumerate(sheet_rows, start=1):
        cells = []
        for col, value in enumerate(values, start=1):
            letters, n = "", col
            while n:
                n, rem = divmod(n - 1, 26); letters = chr(65 + rem) + letters
            cells.append(f'<c r="{letters}{number}" t="inlineStr"><is><t xml:space="preserve">{_xml_text(value)}</t></is></c>')
        xml_rows.append(f'<row r="{number}">{"".join(cells)}</row>')
    sheet = ('<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
             '<worksheet xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main">'
             '<cols><col min="1" max="1" width="28" customWidth="1"/><col min="2" max="3" width="24" customWidth="1"/>'
             '<col min="4" max="4" width="42" customWidth="1"/><col min="5" max="6" width="16" customWidth="1"/></cols>'
             f'<sheetData>{"".join(xml_rows)}</sheetData></worksheet>')
    files = {
        '[Content_Types].xml': '<?xml version="1.0"?><Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types"><Default Extension="rels" ContentType="application/vnd.openxmlformats-package.relationships+xml"/><Default Extension="xml" ContentType="application/xml"/><Override PartName="/xl/workbook.xml" ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet.main+xml"/><Override PartName="/xl/worksheets/sheet1.xml" ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.worksheet+xml"/></Types>',
        '_rels/.rels': '<?xml version="1.0"?><Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships"><Relationship Id="rId1" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/officeDocument" Target="xl/workbook.xml"/></Relationships>',
        'xl/workbook.xml': '<?xml version="1.0"?><workbook xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main" xmlns:r="http://schemas.openxmlformats.org/officeDocument/2006/relationships"><sheets><sheet name="食材词典" sheetId="1" r:id="rId1"/></sheets></workbook>',
        'xl/_rels/workbook.xml.rels': '<?xml version="1.0"?><Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships"><Relationship Id="rId1" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/worksheet" Target="worksheets/sheet1.xml"/></Relationships>',
        'xl/worksheets/sheet1.xml': sheet,
    }
    output = io.BytesIO()
    with zipfile.ZipFile(output, 'w', zipfile.ZIP_DEFLATED) as archive:
        for name, content in files.items(): archive.writestr(name, content.encode())
    return output.getvalue()


def parse_xlsx(data):
    try:
        archive = zipfile.ZipFile(io.BytesIO(data))
        shared = []
        if 'xl/sharedStrings.xml' in archive.namelist():
            root = ET.fromstring(archive.read('xl/sharedStrings.xml'))
            shared = [''.join(node.itertext()) for node in root]
        root = ET.fromstring(archive.read('xl/worksheets/sheet1.xml'))
    except Exception as error:
        raise ValueError("无法读取 Excel 文件") from error
    namespace = {'m': 'http://schemas.openxmlformats.org/spreadsheetml/2006/main'}
    table = []
    for row in root.findall('.//m:sheetData/m:row', namespace):
        values = {}
        for cell in row.findall('m:c', namespace):
            ref = cell.attrib.get('r', '')
            col = re.match(r'[A-Z]+', ref).group(0)
            kind = cell.attrib.get('t')
            if kind == 'inlineStr':
                node = cell.find('m:is', namespace); value = ''.join(node.itertext()) if node is not None else ''
            else:
                node = cell.find('m:v', namespace); value = node.text if node is not None else ''
                if kind == 's' and value: value = shared[int(value)]
            values[col] = value
        table.append([values.get(chr(65 + index), '') for index in range(6)])
    if not table or tuple(table[0][:6]) != HEADERS:
        raise ValueError("Excel 表头不正确，请使用系统导出的模板")
    return [{"ingredient_id": row[0], "name_cn": row[1], "name_en": row[2],
             "aliases": row[3], "status": row[4] or "canonical"}
            for row in table[1:] if any(str(value).strip() for value in row[:4])]


def parse_authoritative_workbook(data):
    """Parse the user-curated first sheet plus its explicit ID migration map."""
    archive = zipfile.ZipFile(io.BytesIO(data))
    shared = []
    if 'xl/sharedStrings.xml' in archive.namelist():
        root = ET.fromstring(archive.read('xl/sharedStrings.xml'))
        shared = [''.join(node.itertext()) for node in root]

    def sheet_values(path):
        root = ET.fromstring(archive.read(path))
        ns = {'m': 'http://schemas.openxmlformats.org/spreadsheetml/2006/main'}
        result = []
        for row in root.findall('.//m:sheetData/m:row', ns):
            values = {}
            for cell in row.findall('m:c', ns):
                match = re.match(r'[A-Z]+', cell.attrib.get('r', ''))
                if not match:
                    continue
                kind = cell.attrib.get('t')
                if kind == 'inlineStr':
                    node = cell.find('m:is', ns); value = ''.join(node.itertext()) if node is not None else ''
                else:
                    node = cell.find('m:v', ns); value = node.text if node is not None else ''
                    if kind == 's' and value: value = shared[int(value)]
                values[match.group(0)] = value
            result.append([values.get(chr(65 + index), '') for index in range(6)])
        return result

    rows = sheet_values('xl/worksheets/sheet1.xml')
    if not rows or tuple(rows[0][:6]) != HEADERS:
        raise ValueError("清理版表头不正确")
    canonical = [{"ingredient_id": row[0].strip(), "name_cn": row[1].strip(),
                  "name_en": row[2].strip(), "aliases": _aliases(row[3]),
                  "status": row[4].strip() or "canonical"}
                 for row in rows[1:] if any(str(value).strip() for value in row[:4])]
    mappings = sheet_values('xl/worksheets/sheet2.xml')
    if not mappings or mappings[0][:6] != ["旧 ingredient_id", "旧中文名", "目标 canonical_id", "目标中文名", "处理", "原因"]:
        raise ValueError("ID迁移映射表头不正确")
    operations = [{"source_id": row[0].strip(), "target_id": row[2].strip(),
                   "action": row[4].strip(), "reason": row[5].strip()}
                  for row in mappings[1:] if str(row[0]).strip()]
    return canonical, operations


def apply_authoritative_workbook(conn, rows, operations):
    """Apply an explicit curated workbook atomically; standard names outrank aliases."""
    ensure_resolution_schema(conn)
    row_ids = [row["ingredient_id"] for row in rows]
    if not row_ids or len(row_ids) != len(set(row_ids)):
        raise ValueError("清理版 ingredient_id 为空或重复")
    for row in rows:
        if not row["name_cn"] or not row["name_en"]:
            raise ValueError(f"{row['ingredient_id']} 缺少标准中文名或英文名")
        if row["status"] not in ("canonical", "default", "rule"):
            raise ValueError(f"{row['ingredient_id']} 状态无效")
    conn.execute("BEGIN IMMEDIATE")
    report = {"merged": {}, "deleted": [], "created": 0, "updated": 0,
              "preserved_unlisted": [], "skipped_conflicting_aliases": []}
    try:
        for operation in operations:
            source = operation["source_id"]
            if operation["action"] == "合并":
                if conn.execute("SELECT 1 FROM ingredients WHERE ingredient_id=?", (source,)).fetchone():
                    report["merged"][source] = merge_ingredient(conn, source, operation["target_id"])
            elif operation["action"] == "删除":
                referenced = sum(conn.execute(
                    f"SELECT COUNT(*) FROM {table} WHERE ingredient_id=?", (source,)
                ).fetchone()[0] for table in ("dish_ingredients", "current_pantry", "inventory_items",
                                               "purchase_requests", "ingredient_classifications",
                                               "pantry_usage_stats", "consumed_history"))
                if referenced:
                    raise ValueError(f"不能删除仍有引用的食材: {source}")
                conn.execute("DELETE FROM ingredient_aliases WHERE ingredient_id=?", (source,))
                conn.execute("DELETE FROM ingredient_dictionary_metadata WHERE ingredient_id=?", (source,))
                conn.execute("DELETE FROM ingredients WHERE ingredient_id=?", (source,))
                report["deleted"].append(source)
            else:
                raise ValueError(f"未知迁移操作: {operation['action']}")

        for row in rows:
            exists = conn.execute("SELECT 1 FROM ingredients WHERE ingredient_id=?", (row["ingredient_id"],)).fetchone()
            if exists:
                conn.execute("UPDATE ingredients SET name_cn=?,name_en=?,aliases=? WHERE ingredient_id=?",
                             (row["name_cn"], row["name_en"], json.dumps(row["aliases"], ensure_ascii=False),
                              row["ingredient_id"]))
                report["updated"] += 1
            else:
                conn.execute("INSERT INTO ingredients(ingredient_id,name_cn,name_en,aliases,category,ingredient_group,is_common) "
                             "VALUES(?,?,?,?,?,'other',0)", (row["ingredient_id"], row["name_cn"], row["name_en"],
                             json.dumps(row["aliases"], ensure_ascii=False), "rule" if row["status"] == "rule" else ""))
                report["created"] += 1
            conn.execute("INSERT INTO ingredient_dictionary_metadata(ingredient_id,status,updated_at) VALUES(?,?,datetime('now')) "
                         "ON CONFLICT(ingredient_id) DO UPDATE SET status=excluded.status,updated_at=excluded.updated_at",
                         (row["ingredient_id"], row["status"]))

        # The workbook is authoritative. Standard Chinese/English names win over
        # aliases; equal-priority duplicates use the earlier workbook row.
        preserved = [row for row in list_dictionary(conn) if row["ingredient_id"] not in set(row_ids)]
        report["preserved_unlisted"] = [row["ingredient_id"] for row in preserved]
        ownership = {}
        candidates = []
        for order, row in enumerate([*rows, *preserved]):
            for rank, kind, text in ((3, "name_cn", row["name_cn"]), (2, "name_en", row["name_en"])):
                candidates.append((rank, -order, row["ingredient_id"], kind, text))
            for text in row["aliases"]:
                candidates.append((1, -order, row["ingredient_id"], "owner", text))
        for rank, neg_order, ingredient_id, kind, text in sorted(candidates, reverse=True):
            key = normalize_key(text)
            if key and key not in ownership:
                ownership[key] = (ingredient_id, kind, text)
            elif key and ownership[key][0] != ingredient_id:
                report["skipped_conflicting_aliases"].append({"text": text, "ingredient_id": ingredient_id,
                                                               "kept_by": ownership[key][0]})
        conn.execute("DELETE FROM ingredient_aliases WHERE ingredient_id NOT LIKE 'pending_%'")
        for ingredient_id in [*row_ids, *report["preserved_unlisted"]]:
            register_alias(conn, ingredient_id, ingredient_id, "id")
        for ingredient_id, kind, text in ownership.values():
            register_alias(conn, ingredient_id, text, kind)
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    _invalidate_runtime_caches()
    return report
