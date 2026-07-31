#!/usr/bin/env python3
"""
家庭菜单管家 - 本地菜品管理器 v2.0
启动后在浏览器中可视化上传菜品照片、编辑/添加/删除菜品。
支持结构化字段：分类、餐别标签、家宴、蛋白质类型、蔬菜、主食类型等。

用法: python photo_manager.py
然后浏览器自动打开 http://localhost:8080
"""

import json
import os
import re
import base64
import webbrowser
from http.server import HTTPServer, ThreadingHTTPServer, BaseHTTPRequestHandler
from urllib.parse import urlparse

# ========== 路径配置 ==========
BASE_DIR = os.path.dirname(os.path.abspath(__file__))
DISH_POOL_FILE = os.path.join(BASE_DIR, "dish_pool.json")
MANIFEST_FILE = os.path.join(BASE_DIR, "photo_manifest.json")
PHOTOS_DIR = os.path.join(BASE_DIR, "photos")
PORT = 8080


def slugify(en_name):
    """英文名转文件名 slug"""
    slug = en_name.lower().strip()
    slug = re.sub(r"[^a-z0-9\s]", "", slug)
    slug = re.sub(r"[\s]+", "_", slug)
    slug = re.sub(r"_+", "_", slug).strip("_")
    return slug if slug else "unnamed"


def load_dish_pool():
    with open(DISH_POOL_FILE, "r", encoding="utf-8") as f:
        return json.load(f)


def save_dish_pool(pool):
    with open(DISH_POOL_FILE, "w", encoding="utf-8") as f:
        json.dump(pool, f, ensure_ascii=False, indent=2)


def load_manifest():
    if os.path.exists(MANIFEST_FILE):
        with open(MANIFEST_FILE, "r", encoding="utf-8") as f:
            return json.load(f)
    return {}


def save_manifest(manifest):
    with open(MANIFEST_FILE, "w", encoding="utf-8") as f:
        json.dump(manifest, f, ensure_ascii=False, indent=2)


def ensure_dirs():
    os.makedirs(PHOTOS_DIR, exist_ok=True)
    if not os.path.exists(MANIFEST_FILE):
        save_manifest({})


def generate_dish_id(pool):
    max_num = 0
    for d in pool.get("dishes", []):
        did = d.get("id", "")
        if did.startswith("dish_"):
            try:
                num = int(did[5:])
                if num > max_num:
                    max_num = num
            except ValueError:
                pass
    return f"dish_{max_num + 1:04d}"


def get_all_dishes():
    """V7: 从 SQLite 读菜品 (is_active=1)，photo manifest 用于 has_photo/photo_file
    SQLite 是唯一真相源，dish_pool.json 已废弃为只读快照。"""
    from db import get_db
    conn = get_db()
    try:
        rows = conn.execute("""
            SELECT d.id, d.name_cn, d.name_en, d.category_id, d.meal_tags, d.banquet,
                   d.protein_types, d.vegetables, d.vegetable_count, d.carb_type,
                   d.meal_components, d.taste, d.cooking_methods, d.can_serve_warm,
                   d.custom_tags, d.needs_review, d.quick_soup, d.slow_soup,
                   d.manual_only_for_breakfast, d.image,
                   c.label_cn AS category_label
            FROM dishes d
            LEFT JOIN categories c ON d.category_id = c.id
            WHERE d.is_active = 1
            ORDER BY d.category_id, d.name_cn
        """).fetchall()

        manifest = load_manifest()
        result = []
        for r in rows:
            name_cn = r["name_cn"] or ""
            has_photo = name_cn in manifest
            photo_file = manifest.get(name_cn, {}).get("file", "") if has_photo else (r["image"] or "")

            # 解析 JSON 字段
            def _parse(v):
                if not v:
                    return []
                if isinstance(v, list):
                    return v
                import json as _json
                try:
                    return _json.loads(v)
                except (TypeError, ValueError):
                    return []

            result.append({
                "id": r["id"],
                "name_cn": name_cn,
                "name_en": r["name_en"] or "",
                "category_id": r["category_id"] or "",
                "category_label": r["category_label"] or r["category_id"] or "",
                "meal_tags": _parse(r["meal_tags"]),
                "banquet": bool(r["banquet"]),
                "protein_types": _parse(r["protein_types"]),
                "vegetables": _parse(r["vegetables"]),
                "vegetable_count": r["vegetable_count"] or 0,
                "carb_type": r["carb_type"],
                "meal_components": _parse(r["meal_components"]),
                "taste": r["taste"] or "normal",
                "cooking_methods": _parse(r["cooking_methods"]),
                "can_serve_warm": bool(r["can_serve_warm"]),
                "custom_tags": _parse(r["custom_tags"]),
                "needs_review": bool(r["needs_review"]),
                "quick_soup": int(r["quick_soup"] or 0),
                "slow_soup": int(r["slow_soup"] or 0),
                "manual_only_for_breakfast": int(r["manual_only_for_breakfast"] or 0),
                "has_photo": has_photo,
                "photo_file": photo_file,
                "slug": slugify(r["name_en"] or ""),
            })
        return result
    finally:
        conn.close()


def get_categories():
    """V7: 从 SQLite categories 表读分类（带每个分类的菜品数量）"""
    from db import get_db
    conn = get_db()
    try:
        rows = conn.execute("""
            SELECT c.id, c.label_cn, c.label_en, c.sort_order, c.active,
                   (SELECT COUNT(*) FROM dishes d WHERE d.category_id = c.id AND d.is_active = 1) AS dish_count
            FROM categories c
            ORDER BY c.sort_order
        """).fetchall()
        cats = []
        for r in rows:
            cats.append({
                "id": r["id"],
                "label_cn": r["label_cn"] or r["id"],
                "label_en": r["label_en"] or "",
                "order": r["sort_order"] or 0,
                "active": bool(r["active"]),
                "type": "category",
                "dish_count": r["dish_count"] or 0,
            })
        return cats
    finally:
        conn.close()


# ========== HTML 界面 ==========
HTML_PAGE = r"""<!DOCTYPE html>
<html lang="zh">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>菜品管理器 v2.0</title>
<style>
* { margin: 0; padding: 0; box-sizing: border-box; }
body {
  font-family: -apple-system, "Helvetica Neue", "PingFang SC", "Microsoft YaHei", sans-serif;
  background: #f5f5f7;
  color: #1d1d1f;
  line-height: 1.6;
}
.header {
  background: #fff;
  padding: 20px 30px;
  border-bottom: 1px solid #e0e0e0;
  position: sticky;
  top: 0;
  z-index: 100;
  box-shadow: 0 1px 6px rgba(0,0,0,0.04);
}
.header-inner {
  max-width: 1200px;
  margin: 0 auto;
  display: flex;
  align-items: center;
  justify-content: space-between;
  flex-wrap: wrap;
  gap: 12px;
}
.header h1 { font-size: 22px; font-weight: 700; }
.header h1 .emoji { margin-right: 8px; }
.header-actions { display: flex; align-items: center; gap: 8px; flex-wrap: wrap; }
.stats {
  font-size: 14px;
  color: #6e6e73;
  background: #f0f0f5;
  padding: 6px 16px;
  border-radius: 20px;
  font-weight: 600;
}
.stats .done { color: #34c759; }
.btn-add {
  background: #34c759;
  color: #fff;
  border: none;
  padding: 8px 18px;
  border-radius: 20px;
  font-size: 14px;
  font-weight: 600;
  cursor: pointer;
  transition: background 0.15s;
}
.btn-add:hover { background: #2db84e; }
.btn-secondary {
  background: #fff;
  color: #1d1d1f;
  border: 1.5px solid #d0d0d5;
  padding: 7px 16px;
  border-radius: 20px;
  font-size: 13px;
  font-weight: 600;
  cursor: pointer;
  transition: all 0.15s;
}
.btn-secondary:hover { border-color: #007aff; color: #007aff; }
.search-box {
  max-width: 1200px;
  margin: 20px auto 0;
  padding: 0 30px;
}
.search-box input {
  width: 100%;
  padding: 12px 20px;
  font-size: 15px;
  border: 2px solid #e0e0e0;
  border-radius: 12px;
  outline: none;
  transition: border-color 0.2s;
}
.search-box input:focus { border-color: #007aff; }
.filter-bar {
  max-width: 1200px;
  margin: 10px auto 0;
  padding: 0 30px;
  display: flex;
  gap: 8px;
  flex-wrap: wrap;
  align-items: center;
}
.filter-bar .filter-label {
  font-size: 12px;
  color: #8e8e93;
  font-weight: 600;
  margin-right: 4px;
  white-space: nowrap;
}
.filter-btn {
  padding: 5px 14px;
  border: 1.5px solid #d0d0d5;
  border-radius: 16px;
  background: #fff;
  font-size: 13px;
  cursor: pointer;
  transition: all 0.15s;
  color: #6e6e73;
}
.filter-btn:hover { border-color: #007aff; color: #007aff; }
.filter-btn.active { background: #007aff; color: #fff; border-color: #007aff; }
.container {
  max-width: 1200px;
  margin: 0 auto;
  padding: 20px 30px 60px;
}
.category-section { margin-bottom: 30px; }
.category-title {
  font-size: 16px;
  font-weight: 700;
  color: #1d1d1f;
  margin-bottom: 12px;
  padding-bottom: 6px;
  border-bottom: 2px solid #e8e8ed;
}
.dish-grid {
  display: grid;
  grid-template-columns: repeat(auto-fill, minmax(220px, 1fr));
  gap: 16px;
}
.dish-card {
  background: #fff;
  border-radius: 14px;
  overflow: hidden;
  border: 2px solid #e8e8ed;
  transition: all 0.2s;
  position: relative;
}
.dish-card:hover { border-color: #c7c7cc; box-shadow: 0 4px 16px rgba(0,0,0,0.06); }
.dish-card.has-photo { border-color: #34c759; }
.dish-photo-area {
  width: 100%;
  aspect-ratio: 1 / 1;
  height: auto;
  background: #f0f0f5;
  display: flex;
  align-items: center;
  justify-content: center;
  position: relative;
  overflow: hidden;
  cursor: pointer;
}
.dish-photo-area img {
  width: 100%;
  height: 100%;
  object-fit: cover;
}
.photo-placeholder {
  font-size: 36px;
  opacity: 0.3;
}
.has-photo-badge {
  position: absolute;
  top: 8px;
  right: 8px;
  background: #34c759;
  color: #fff;
  font-size: 11px;
  padding: 2px 8px;
  border-radius: 10px;
  font-weight: 600;
}
.dish-info {
  padding: 10px 14px;
}
.dish-zh { font-size: 14px; font-weight: 600; line-height: 1.3; }
.dish-en { font-size: 12px; color: #8e8e93; margin-top: 2px; line-height: 1.3; }
.card-badges {
  display: flex;
  flex-wrap: wrap;
  gap: 4px;
  margin-top: 6px;
}
.badge {
  font-size: 11px;
  padding: 2px 8px;
  border-radius: 10px;
  font-weight: 600;
  white-space: nowrap;
}
.badge-category { background: #e8f0ff; color: #007aff; }
.badge-meal { background: #fff3e0; color: #f97316; }
.badge-banquet { background: #fce4ec; color: #e91e63; }
.badge-review { background: #fff8e1; color: #ff9800; }
.card-actions {
  display: flex;
  gap: 0;
  border-top: 1px solid #e8e8ed;
}
.card-actions button {
  flex: 1;
  padding: 8px;
  border: none;
  font-size: 12px;
  font-weight: 600;
  cursor: pointer;
  transition: background 0.15s;
  background: #f8f8fa;
}
.card-actions button:not(:last-child) { border-right: 1px solid #e8e8ed; }
.card-actions .btn-upload { color: #007aff; }
.card-actions .btn-upload:hover { background: #e8f0ff; }
.card-actions .btn-upload.uploading { background: #ffd60a; color: #1d1d1f; pointer-events: none; }
.card-actions .btn-edit { color: #5856d6; }
.card-actions .btn-edit:hover { background: #eeeaf8; }
.card-actions .btn-delete { color: #ff3b30; }
.card-actions .btn-delete:hover { background: #ffeeed; }
.dish-card.dragover .dish-photo-area { background: #d0e8ff; border: 3px dashed #007aff; }

/* Modal */
.modal-overlay {
  position: fixed;
  top: 0; left: 0; right: 0; bottom: 0;
  background: rgba(0,0,0,0.4);
  display: none;
  align-items: center;
  justify-content: center;
  z-index: 1000;
}
.modal-overlay.show { display: flex; }
.modal {
  background: #fff;
  border-radius: 16px;
  padding: 28px;
  width: 90%;
  max-width: 420px;
  max-height: 85vh;
  overflow-y: auto;
  box-shadow: 0 8px 32px rgba(0,0,0,0.15);
}
.modal-large { max-width: 540px; }
.modal h3 {
  font-size: 18px;
  margin-bottom: 18px;
  font-weight: 700;
}
.modal label, .modal .form-label {
  display: block;
  font-size: 13px;
  font-weight: 600;
  color: #6e6e73;
  margin-bottom: 4px;
  margin-top: 14px;
}
.modal label:first-of-type, .modal .form-section:first-child .form-label { margin-top: 0; }
.modal input[type="text"], .modal select {
  width: 100%;
  padding: 10px 14px;
  font-size: 15px;
  border: 2px solid #e0e0e0;
  border-radius: 10px;
  outline: none;
  transition: border-color 0.2s;
}
.modal input[type="text"]:focus, .modal select:focus { border-color: #007aff; }
.modal input[type="checkbox"] {
  width: 18px;
  height: 18px;
  cursor: pointer;
}
.modal-actions {
  display: flex;
  gap: 10px;
  margin-top: 22px;
}
.modal-actions button {
  flex: 1;
  padding: 10px;
  border: none;
  border-radius: 10px;
  font-size: 15px;
  font-weight: 600;
  cursor: pointer;
}
.btn-save { background: #007aff; color: #fff; }
.btn-save:hover { background: #0066d6; }
.btn-cancel { background: #f0f0f5; color: #6e6e73; }
.btn-cancel:hover { background: #e0e0e5; }

/* Form sections */
.form-section { margin-bottom: 16px; }
.section-title {
  font-size: 12px;
  font-weight: 700;
  color: #8e8e93;
  margin-bottom: 8px;
  text-transform: uppercase;
  letter-spacing: 0.5px;
}
.section-title.collapsible {
  cursor: pointer;
  user-select: none;
  display: flex;
  align-items: center;
  gap: 4px;
  padding: 8px 12px;
  background: #f0f0f5;
  border-radius: 8px;
  transition: background 0.15s;
}
.section-title.collapsible:hover { background: #e8e8ed; color: #007aff; }
.checkbox-group {
  display: flex;
  flex-wrap: wrap;
  gap: 12px;
}
.checkbox-label {
  display: flex;
  align-items: center;
  gap: 4px;
  font-size: 14px;
  cursor: pointer;
  font-weight: 500;
  color: #1d1d1f;
}
.tag-input-container {
  display: flex;
  flex-wrap: wrap;
  gap: 6px;
  padding: 8px;
  border: 2px solid #e0e0e0;
  border-radius: 10px;
  min-height: 42px;
  align-items: center;
}
.tag-input-container:focus-within { border-color: #007aff; }
.tag-chip {
  background: #e8f0ff;
  color: #007aff;
  padding: 3px 10px;
  border-radius: 12px;
  font-size: 13px;
  font-weight: 600;
  display: inline-flex;
  align-items: center;
  gap: 4px;
}
.tag-remove {
  cursor: pointer;
  font-weight: 700;
  opacity: 0.7;
  font-size: 15px;
  line-height: 1;
}
.tag-remove:hover { opacity: 1; }
.tag-input {
  border: none;
  outline: none;
  flex: 1;
  min-width: 100px;
  font-size: 14px;
  background: transparent;
}
.modal-note {
  font-size: 13px;
  color: #8e8e93;
  margin-bottom: 12px;
  padding: 8px 12px;
  background: #f0f0f5;
  border-radius: 8px;
}

/* Manager modals */
.manager-item {
  display: flex;
  align-items: center;
  gap: 8px;
  padding: 8px 0;
  border-bottom: 1px solid #e8e8ed;
  flex-wrap: wrap;
}
.manager-item input[type="text"] {
  flex: 1;
  min-width: 120px;
  padding: 6px 10px;
  font-size: 14px;
  border: 1.5px solid #e0e0e0;
  border-radius: 8px;
  outline: none;
}
.manager-item input[type="text"]:focus { border-color: #007aff; }
.manager-item input[type="text"]:disabled {
  background: #f0f0f5;
  color: #8e8e93;
}
.mgr-btn {
  padding: 4px 10px;
  border: 1.5px solid #d0d0d5;
  border-radius: 6px;
  background: #fff;
  cursor: pointer;
  font-size: 14px;
  font-weight: 700;
  color: #1d1d1f;
}
.mgr-btn:hover { border-color: #007aff; color: #007aff; }
.mgr-toggle {
  padding: 4px 12px;
  border: none;
  border-radius: 6px;
  cursor: pointer;
  font-size: 12px;
  font-weight: 600;
}
.mgr-toggle.active { background: #34c759; color: #fff; }
.mgr-toggle.inactive { background: #e0e0e5; color: #8e8e93; }
.mgr-delete {
  padding: 4px 10px;
  border: none;
  border-radius: 6px;
  background: #ffeeed;
  color: #ff3b30;
  cursor: pointer;
  font-size: 12px;
  font-weight: 600;
}
.mgr-delete:hover { background: #ff3b30; color: #fff; }
.mgr-info {
  font-size: 12px;
  color: #8e8e93;
  white-space: nowrap;
}
.mgr-count {
  font-size: 12px;
  color: #8e8e93;
  white-space: nowrap;
}
.mgr-add-btn {
  margin-top: 10px;
  padding: 6px 16px;
  background: #e8f0ff;
  color: #007aff;
  border: 1.5px solid #007aff;
  border-radius: 8px;
  cursor: pointer;
  font-size: 13px;
  font-weight: 600;
}
.mgr-add-btn:hover { background: #007aff; color: #fff; }

/* Toast */
.toast {
  position: fixed;
  bottom: 30px;
  left: 50%;
  transform: translateX(-50%);
  background: #1d1d1f;
  color: #fff;
  padding: 12px 28px;
  border-radius: 12px;
  font-size: 14px;
  font-weight: 600;
  z-index: 999;
  opacity: 0;
  transition: opacity 0.3s;
  pointer-events: none;
}
.toast.show { opacity: 1; }
.toast.success { background: #34c759; }
.toast.error { background: #ff3b30; }
.empty-msg {
  text-align: center;
  padding: 60px 20px;
  color: #8e8e93;
  font-size: 15px;
}
</style>
</head>
<body>

<div class="header">
  <div class="header-inner">
    <h1><span class="emoji">🍳</span>菜品管理器</h1>
    <div class="header-actions">
      <div class="stats" id="stats">加载中...</div>
      <button class="btn-secondary" onclick="openCategoryManager()">管理分类</button>
      <button class="btn-secondary" onclick="openTagManager()">管理标签</button>
      <button class="btn-add" onclick="openAddModal()">+ 添加菜品</button>
    </div>
  </div>
</div>

<div class="search-box">
  <input type="text" id="searchInput" placeholder="搜索菜名、分类、食材、蔬菜、标签..." oninput="render()">
</div>

<div class="filter-bar" id="mealFilterBar"></div>
<div class="filter-bar" id="categoryFilterBar"></div>

<div class="container" id="container">
  <div class="empty-msg">正在加载菜品数据...</div>
</div>

<!-- Edit Modal -->
<div class="modal-overlay" id="editModal">
  <div class="modal modal-large">
    <h3>编辑菜品</h3>

    <div class="form-section">
      <div class="section-title">基础信息</div>
      <label>中文名</label>
      <input type="text" id="editZh" placeholder="中文名">
      <label>英文名</label>
      <input type="text" id="editEn" placeholder="英文名">
      <label>分类</label>
      <select id="editCategory" onchange="onCategoryChange()"></select>
    </div>

    <div class="form-section">
      <div class="section-title">适合餐别</div>
      <div class="checkbox-group">
        <label class="checkbox-label"><input type="checkbox" id="editBreakfast"> 早餐</label>
        <label class="checkbox-label"><input type="checkbox" id="editLunch"> 午餐</label>
        <label class="checkbox-label"><input type="checkbox" id="editDinner"> 晚餐</label>
      </div>
    </div>

    <div class="form-section">
      <div class="section-title">特殊标签</div>
      <label class="checkbox-label"><input type="checkbox" id="editBanquet"> 家宴推荐</label>
    </div>

    <div class="form-section">
      <div class="section-title">自定义标签</div>
      <div class="tag-input-container" id="editCustomTags"></div>
    </div>

    <div class="form-section">
      <div class="section-title collapsible" id="advancedTitle" onclick="toggleAdvanced()">更多信息 / AI 配餐信息 ▼</div>
      <div id="advancedSection" style="display:none;">

        <label>主要蛋白质</label>
        <div class="checkbox-group" id="editProteinTypes"></div>

        <label>包含蔬菜</label>
        <div class="tag-input-container" id="editVegetables"></div>

        <div id="carbTypeSection" style="display:none;">
          <label>主食类型</label>
          <select id="editCarbType">
            <option value="">请选择</option>
            <option value="rice">米饭</option>
            <option value="porridge">粥</option>
            <option value="noodle">面</option>
            <option value="dim_sum">包点 / 饺子</option>
            <option value="coarse_grain">粗粮（含薯类）</option>
            <option value="other">其他</option>
          </select>
        </div>

        <div id="mealComponentsSection" style="display:none;">
          <label>这道菜已经包含</label>
          <div class="checkbox-group" id="editMealComponents">
            <label class="checkbox-label"><input type="checkbox" value="protein"> 蛋白质</label>
            <label class="checkbox-label"><input type="checkbox" value="vegetable"> 蔬菜</label>
            <label class="checkbox-label"><input type="checkbox" value="carb"> 主食 / 碳水</label>
          </div>
        </div>

        <label>口味</label>
        <select id="editTaste">
          <option value="light">清淡</option>
          <option value="normal">正常</option>
          <option value="rich">浓味</option>
          <option value="spicy">辣</option>
        </select>

        <label>烹饪方式（可多选）</label>
        <div class="checkbox-group" id="editCookingMethods"></div>

        <div id="canServeWarmSection" style="display:none;">
          <label class="checkbox-label"><input type="checkbox" id="editCanServeWarm"> 可改造成温热版本</label>
        </div>

        <label style="margin-top:14px;">V3 汤类标签 / Soup Tags</label>
        <div class="checkbox-group">
          <label class="checkbox-label"><input type="checkbox" id="editQuickSoup"> 快手汤 Quick Soup（午餐）</label>
          <label class="checkbox-label"><input type="checkbox" id="editSlowSoup"> 慢火汤 Slow Soup（晚餐）</label>
          <label class="checkbox-label"><input type="checkbox" id="editManualOnlyBreakfast"> 早餐手选 Manual Only（不自动推荐）</label>
        </div>

      </div>
    </div>

    <div class="modal-actions">
      <button class="btn-cancel" onclick="closeModal('editModal')">取消</button>
      <button class="btn-save" onclick="saveEdit()">保存</button>
    </div>
  </div>
</div>

<!-- Add Modal -->
<div class="modal-overlay" id="addModal">
  <div class="modal">
    <h3>添加菜品</h3>
    <label>中文名</label>
    <input type="text" id="addZh" placeholder="中文名">
    <label>英文名</label>
    <input type="text" id="addEn" placeholder="英文名">
    <label>分类</label>
    <select id="addCategory"></select>
    <label>适合餐别</label>
    <div class="checkbox-group">
      <label class="checkbox-label"><input type="checkbox" id="addBreakfast"> 早餐</label>
      <label class="checkbox-label"><input type="checkbox" id="addLunch"> 午餐</label>
      <label class="checkbox-label"><input type="checkbox" id="addDinner"> 晚餐</label>
    </div>
    <label class="checkbox-label" style="margin-top: 14px;"><input type="checkbox" id="addBanquet"> 家宴推荐</label>
    <label style="margin-top: 14px; display:block;">V3 汤类标签 / Soup Tags</label>
    <div class="checkbox-group">
      <label class="checkbox-label"><input type="checkbox" id="addQuickSoup"> 快手汤 Quick Soup</label>
      <label class="checkbox-label"><input type="checkbox" id="addSlowSoup"> 慢火汤 Slow Soup</label>
      <label class="checkbox-label"><input type="checkbox" id="addManualOnlyBreakfast"> 早餐手选 Manual Only</label>
    </div>
    <div class="modal-actions">
      <button class="btn-cancel" onclick="closeModal('addModal')">取消</button>
      <button class="btn-save" onclick="saveAdd()">添加</button>
    </div>
  </div>
</div>

<!-- Category Manager Modal -->
<div class="modal-overlay" id="categoryModal">
  <div class="modal modal-large">
    <h3>管理分类</h3>
    <div id="categoryList"></div>
    <div class="modal-actions">
      <button class="btn-cancel" onclick="closeModal('categoryModal')">取消</button>
      <button class="btn-save" onclick="saveCategories()">保存</button>
    </div>
  </div>
</div>

<!-- Tag Manager Modal -->
<div class="modal-overlay" id="tagModal">
  <div class="modal modal-large">
    <h3>管理标签</h3>
    <div class="form-section">
      <div class="section-title">系统标签（不可删除）</div>
      <div id="systemTagList"></div>
    </div>
    <div class="form-section">
      <div class="section-title">自定义标签</div>
      <div id="customTagList"></div>
      <button class="mgr-add-btn" onclick="addCustomTagRow()">+ 添加自定义标签</button>
    </div>
    <div class="modal-actions">
      <button class="btn-cancel" onclick="closeModal('tagModal')">取消</button>
      <button class="btn-save" onclick="saveTags()">保存</button>
    </div>
  </div>
</div>

<div class="toast" id="toast"></div>

<script>
// ========== 常量 ==========
const PROTEIN_TYPES = {
  fish: '鱼', shrimp: '虾', other_seafood: '其他海鲜',
  beef: '牛肉', pork: '猪肉', chicken: '鸡肉',
  egg: '鸡蛋', tofu: '豆制品', other: '其他', none: '无'
};
const COOKING_METHODS = {
  steam: '蒸', boil: '煮', stir_fry: '炒', stew: '炖',
  braise: '焖', pan_fry: '煎', roast: '烤', cold_mix: '凉拌', other: '其他'
};
const MEAL_TAG_LABELS = {
  breakfast: '早餐', lunch: '午餐', dinner: '晚餐'
};
const SYSTEM_TAGS = [
  { id: 'breakfast', label_cn: '早餐' },
  { id: 'lunch', label_cn: '午餐' },
  { id: 'dinner', label_cn: '晚餐' },
  { id: 'banquet', label_cn: '家宴推荐' }
];

// ========== 状态 ==========
let allDishes = [];
let allCategories = [];
let allCustomTags = [];
let currentMealFilter = 'all';
let currentCategoryFilter = 'all';
let editingDish = null;

// ========== 初始化 ==========
async function loadDishes() {
  try {
    const [dishResp, catResp, tagResp] = await Promise.all([
      fetch('/api/dishes'), fetch('/api/categories'), fetch('/api/custom_tags')
    ]);
    allDishes = await dishResp.json();
    allCategories = await catResp.json();
    allCustomTags = await tagResp.json();
    renderFilters();
    renderAddCategories();
    render();
  } catch(e) {
    console.error('Load error:', e);
    showToast('加载失败: ' + e.message, 'error');
  }
}

// ========== 筛选 ==========
function renderFilters() {
  const mealBar = document.getElementById('mealFilterBar');
  mealBar.innerHTML = '<span class="filter-label">餐别：</span>' +
    ['all','breakfast','lunch','dinner','banquet'].map(m => {
      const labels = { all: '全部', breakfast: '早餐', lunch: '午餐', dinner: '晚餐', banquet: '家宴' };
      const active = currentMealFilter === m ? 'active' : '';
      return '<button class="filter-btn ' + active + '" onclick="setMealFilter(\'' + m + '\')">' + labels[m] + '</button>';
    }).join('');

  const catBar = document.getElementById('categoryFilterBar');
  let html = '<span class="filter-label">分类：</span>';
  html += '<button class="filter-btn ' + (currentCategoryFilter === 'all' ? 'active' : '') + '" onclick="setCategoryFilter(\'all\')">全部分类</button>';
  allCategories.forEach(c => {
    const active = currentCategoryFilter === c.id ? 'active' : '';
    html += '<button class="filter-btn ' + active + '" onclick="setCategoryFilter(\'' + escAttr(c.id) + '\')">' + escHtml(c.label_cn) + '</button>';
  });
  catBar.innerHTML = html;
}

function setMealFilter(m) {
  currentMealFilter = m;
  renderFilters();
  render();
}

function setCategoryFilter(c) {
  currentCategoryFilter = c;
  renderFilters();
  render();
}

// ========== 搜索 ==========
function getSearchableText(d) {
  const parts = [
    d.name_cn, d.name_en, d.category_label,
    (d.meal_tags || []).map(t => MEAL_TAG_LABELS[t] || t).join(' '),
    (d.protein_types || []).map(t => PROTEIN_TYPES[t] || t).join(' '),
    (d.vegetables || []).join(' '),
    (d.custom_tags || []).join(' '),
    d.banquet ? '家宴' : ''
  ];
  return parts.join(' ').toLowerCase();
}

// ========== 渲染 ==========
function render() {
  const search = document.getElementById('searchInput').value.toLowerCase().trim();
  const container = document.getElementById('container');

  const grouped = {};
  let totalCount = 0;

  allDishes.forEach((d, idx) => {
    // Meal filter
    if (currentMealFilter !== 'all') {
      if (currentMealFilter === 'banquet') {
        if (!d.banquet) return;
      } else {
        if (!(d.meal_tags || []).includes(currentMealFilter)) return;
      }
    }
    // Category filter
    if (currentCategoryFilter !== 'all' && d.category_id !== currentCategoryFilter) return;
    // Search
    if (search && !getSearchableText(d).includes(search)) return;

    if (!grouped[d.category_label]) grouped[d.category_label] = [];
    grouped[d.category_label].push({ dish: d, idx: idx });
    totalCount++;
  });

  // Stats
  const totalAll = allDishes.length;
  const photoAll = allDishes.filter(d => d.has_photo).length;
  document.getElementById('stats').innerHTML =
    '<span class="done">' + photoAll + '</span> / ' + totalAll + ' 道菜已上传';

  if (totalCount === 0) {
    container.innerHTML = '<div class="empty-msg">没有匹配的菜品</div>';
    return;
  }

  let html = '';
  Object.entries(grouped).forEach(([label, items]) => {
    html += '<div class="category-section">';
    html += '<div class="category-title">' + escHtml(label) + '（' + items.length + '）</div>';
    html += '<div class="dish-grid">';
    items.forEach(({ dish, idx }) => {
      html += renderDishCard(dish, idx);
    });
    html += '</div></div>';
  });

  container.innerHTML = html;
}

function renderDishCard(d, idx) {
  const photoHtml = d.has_photo
    ? '<img src="/photos/' + escAttr(d.photo_file) + '?t=' + (d._t || Date.now()) + '" alt="' + escAttr(d.name_cn) + '">' +
      '<div class="has-photo-badge">已上传</div>'
    : '<div class="photo-placeholder">🍽️</div>';

  let badges = '';
  badges += '<span class="badge badge-category">' + escHtml(d.category_label) + '</span>';
  if (d.meal_tags && d.meal_tags.length > 0) {
    const mealLabels = d.meal_tags.map(t => MEAL_TAG_LABELS[t] || t).join(' · ');
    badges += '<span class="badge badge-meal">' + escHtml(mealLabels) + '</span>';
  }
  if (d.banquet) badges += '<span class="badge badge-banquet">家宴</span>';
  if (d.quick_soup) badges += '<span class="badge badge-review">快手汤</span>';
  if (d.slow_soup) badges += '<span class="badge" style="background:#e8f5e9;color:#2e7d32">慢火汤</span>';
  if (d.manual_only_for_breakfast) badges += '<span class="badge" style="background:#fff3e0;color:#e65100">早手选</span>';
  if (d.needs_review) badges += '<span class="badge badge-review">待审核</span>';

  return '<div class="dish-card ' + (d.has_photo ? 'has-photo' : '') + '" data-idx="' + idx + '" data-slug="' + escAttr(d.slug) + '" data-name-cn="' + escAttr(d.name_cn) + '">' +
    '<div class="dish-photo-area" onclick="triggerUpload(this)" ondragover="onDragOver(event,this)" ondragleave="onDragLeave(event,this)" ondrop="onDrop(event,this)">' +
      photoHtml +
    '</div>' +
    '<div class="dish-info">' +
      '<div class="dish-zh">' + escHtml(d.name_cn) + '</div>' +
      '<div class="dish-en">' + escHtml(d.name_en) + '</div>' +
      '<div class="card-badges">' + badges + '</div>' +
    '</div>' +
    '<div class="card-actions">' +
      '<button class="btn-upload" onclick="event.stopPropagation();triggerUpload(this.closest(\'.dish-card\').querySelector(\'.dish-photo-area\'))">' +
        (d.has_photo ? '更换' : '上传') +
      '</button>' +
      '<button class="btn-edit" onclick="event.stopPropagation();openEditByIndex(' + idx + ')">编辑</button>' +
      '<button class="btn-delete" onclick="event.stopPropagation();deleteByIndex(' + idx + ')">删除</button>' +
    '</div>' +
  '</div>';
}

function renderAddCategories() {
  const sel = document.getElementById('addCategory');
  sel.innerHTML = allCategories
    .filter(c => c.type === 'category' && c.active)
    .map(c => '<option value="' + escAttr(c.id) + '">' + escHtml(c.label_cn) + '</option>')
    .join('');
}

// ========== 编辑菜品 ==========
function openEditByIndex(idx) {
  const d = allDishes[idx];
  if (!d) return;
  openEditModal(d);
}

function openEditModal(d) {
  editingDish = d;

  document.getElementById('editZh').value = d.name_cn;
  document.getElementById('editEn').value = d.name_en;

  const catSelect = document.getElementById('editCategory');
  catSelect.innerHTML = allCategories
    .filter(c => c.type === 'category')
    .map(c => '<option value="' + escAttr(c.id) + '"' + (c.id === d.category_id ? ' selected' : '') + '>' + escHtml(c.label_cn) + '</option>')
    .join('');

  document.getElementById('editBreakfast').checked = (d.meal_tags || []).includes('breakfast');
  document.getElementById('editLunch').checked = (d.meal_tags || []).includes('lunch');
  document.getElementById('editDinner').checked = (d.meal_tags || []).includes('dinner');
  document.getElementById('editBanquet').checked = d.banquet;

  initTagInput('editCustomTags', d.custom_tags || [], '输入标签后按回车添加');

  initCheckboxGroup('editProteinTypes', PROTEIN_TYPES, d.protein_types || []);
  initTagInput('editVegetables', d.vegetables || [], '输入蔬菜名后按回车添加');
  document.getElementById('editCarbType').value = d.carb_type || '';
  setCheckboxGroup('editMealComponents', d.meal_components || []);
  document.getElementById('editTaste').value = d.taste || 'normal';
  initCheckboxGroup('editCookingMethods', COOKING_METHODS, d.cooking_methods || []);
  document.getElementById('editCanServeWarm').checked = d.can_serve_warm;
  document.getElementById('editQuickSoup').checked = !!d.quick_soup;
  document.getElementById('editSlowSoup').checked = !!d.slow_soup;
  document.getElementById('editManualOnlyBreakfast').checked = !!d.manual_only_for_breakfast;

  onCategoryChange();
  document.getElementById('editModal').classList.add('show');
}

function onCategoryChange() {
  const catId = document.getElementById('editCategory').value;
  document.getElementById('carbTypeSection').style.display = (catId === 'staple_carb') ? 'block' : 'none';
  document.getElementById('mealComponentsSection').style.display = (catId === 'one_pot_meal') ? 'block' : 'none';
  document.getElementById('canServeWarmSection').style.display = (catId === 'cold_dish') ? 'block' : 'none';
}

function toggleAdvanced() {
  const section = document.getElementById('advancedSection');
  const title = document.getElementById('advancedTitle');
  if (section.style.display === 'none') {
    section.style.display = 'block';
    title.innerHTML = '更多信息 / AI 配餐信息 ▲';
  } else {
    section.style.display = 'none';
    title.innerHTML = '更多信息 / AI 配餐信息 ▼';
  }
}

async function saveEdit() {
  const newNameCn = document.getElementById('editZh').value.trim();
  const newNameEn = document.getElementById('editEn').value.trim();
  if (!newNameCn || !newNameEn) { showToast('中英文名都不能为空', 'error'); return; }

  const mealTags = [];
  if (document.getElementById('editBreakfast').checked) mealTags.push('breakfast');
  if (document.getElementById('editLunch').checked) mealTags.push('lunch');
  if (document.getElementById('editDinner').checked) mealTags.push('dinner');

  const data = {
    id: editingDish.id,
    old_name_cn: editingDish.name_cn,
    name_cn: newNameCn,
    name_en: newNameEn,
    category_id: document.getElementById('editCategory').value,
    meal_tags: mealTags,
    banquet: document.getElementById('editBanquet').checked,
    protein_types: getCheckboxValues('editProteinTypes'),
    vegetables: getTagValues('editVegetables'),
    carb_type: document.getElementById('editCarbType').value || null,
    meal_components: getCheckboxValues('editMealComponents'),
    taste: document.getElementById('editTaste').value,
    cooking_methods: getCheckboxValues('editCookingMethods'),
    can_serve_warm: document.getElementById('editCanServeWarm').checked,
    custom_tags: getTagValues('editCustomTags'),
    quick_soup: document.getElementById('editQuickSoup').checked ? 1 : 0,
    slow_soup: document.getElementById('editSlowSoup').checked ? 1 : 0,
    manual_only_for_breakfast: document.getElementById('editManualOnlyBreakfast').checked ? 1 : 0,
  };

  try {
    const resp = await fetch('/api/edit_dish', {
      method: 'POST', headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(data)
    });
    const result = await resp.json();
    if (result.success) {
      // Update local
      editingDish.name_cn = newNameCn;
      editingDish.name_en = newNameEn;
      editingDish.category_id = data.category_id;
      editingDish.meal_tags = mealTags;
      editingDish.banquet = data.banquet;
      editingDish.protein_types = data.protein_types;
      editingDish.vegetables = data.vegetables;
      editingDish.vegetable_count = data.vegetables.length;
      editingDish.carb_type = data.carb_type;
      editingDish.meal_components = data.meal_components;
      editingDish.taste = data.taste;
      editingDish.cooking_methods = data.cooking_methods;
      editingDish.can_serve_warm = data.can_serve_warm;
      editingDish.custom_tags = data.custom_tags;
      editingDish.quick_soup = data.quick_soup;
      editingDish.slow_soup = data.slow_soup;
      editingDish.manual_only_for_breakfast = data.manual_only_for_breakfast;
      editingDish.slug = result.new_slug || slugify(newNameEn);
      const cat = allCategories.find(c => c.id === data.category_id);
      editingDish.category_label = cat ? cat.label_cn : data.category_id;
      if (result.has_photo) {
        editingDish.has_photo = true;
        editingDish.photo_file = result.photo_file;
        editingDish._t = Date.now();
      }
      closeModal('editModal');
      render();
      renderFilters();
      showToast('已更新：「' + newNameCn + '」', 'success');
    } else {
      throw new Error(result.error || '更新失败');
    }
  } catch(err) {
      showToast('更新失败: ' + err.message, 'error');
  }
}

// ========== 添加菜品 ==========
function openAddModal() {
  document.getElementById('addZh').value = '';
  document.getElementById('addEn').value = '';
  document.getElementById('addBreakfast').checked = false;
  document.getElementById('addLunch').checked = false;
  document.getElementById('addDinner').checked = false;
  document.getElementById('addBanquet').checked = false;
  document.getElementById('addQuickSoup').checked = false;
  document.getElementById('addSlowSoup').checked = false;
  document.getElementById('addManualOnlyBreakfast').checked = false;
  document.getElementById('addModal').classList.add('show');
  document.getElementById('addZh').focus();
}

async function saveAdd() {
  const nameCn = document.getElementById('addZh').value.trim();
  const nameEn = document.getElementById('addEn').value.trim();
  const categoryId = document.getElementById('addCategory').value;
  if (!nameCn || !nameEn) { showToast('中英文名都不能为空', 'error'); return; }

  const mealTags = [];
  if (document.getElementById('addBreakfast').checked) mealTags.push('breakfast');
  if (document.getElementById('addLunch').checked) mealTags.push('lunch');
  if (document.getElementById('addDinner').checked) mealTags.push('dinner');

  try {
    const resp = await fetch('/api/add_dish', {
      method: 'POST', headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({
        name_cn: nameCn, name_en: nameEn, category_id: categoryId,
        meal_tags: mealTags,
        banquet: document.getElementById('addBanquet').checked,
        quick_soup: document.getElementById('addQuickSoup').checked ? 1 : 0,
        slow_soup: document.getElementById('addSlowSoup').checked ? 1 : 0,
        manual_only_for_breakfast: document.getElementById('addManualOnlyBreakfast').checked ? 1 : 0,
      })
    });
    const result = await resp.json();
    if (result.success) {
      const cat = allCategories.find(c => c.id === categoryId);
      allDishes.push({
        id: result.id, name_cn: nameCn, name_en: nameEn,
        category_id: categoryId, category_label: cat ? cat.label_cn : categoryId,
        meal_tags: mealTags,
        banquet: document.getElementById('addBanquet').checked,
        protein_types: [], vegetables: [], vegetable_count: 0,
        carb_type: null, meal_components: [], taste: 'normal',
        cooking_methods: [], can_serve_warm: false, custom_tags: [],
        needs_review: false,
        quick_soup: document.getElementById('addQuickSoup').checked ? 1 : 0,
        slow_soup: document.getElementById('addSlowSoup').checked ? 1 : 0,
        manual_only_for_breakfast: document.getElementById('addManualOnlyBreakfast').checked ? 1 : 0,
        has_photo: false, photo_file: '', slug: result.slug
      });
      closeModal('addModal');
      render();
      renderFilters();
      showToast('已添加：「' + nameCn + '」', 'success');
    } else {
      throw new Error(result.error || '添加失败');
    }
  } catch(err) {
    showToast('添加失败: ' + err.message, 'error');
  }
}

// ========== 删除菜品 ==========
function deleteByIndex(idx) {
  const d = allDishes[idx];
  if (!d) return;
  if (!confirm('确定删除「' + d.name_cn + '」吗？\n如果有照片，照片也会一起删除。')) return;

  const body = { id: d.id, name_cn: d.name_cn };

  fetch('/api/delete_dish', {
    method: 'POST', headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify(body)
  }).then(r => r.json()).then(result => {
    if (result.success) {
      allDishes.splice(idx, 1);
      render();
      renderFilters();
      showToast('已删除：「' + d.name_cn + '」', 'success');
    } else {
      showToast('删除失败: ' + (result.error || ''), 'error');
    }
  }).catch(err => showToast('删除失败: ' + err.message, 'error'));
}

// ========== 照片上传 ==========
function triggerUpload(photoArea) {
  const card = photoArea.closest('.dish-card');
  const idx = parseInt(card.dataset.idx);
  const d = allDishes[idx];
  if (!d) return;
  const input = document.createElement('input');
  input.type = 'file';
  input.accept = 'image/*';
  input.onchange = (e) => { if (e.target.files[0]) handleFile(e.target.files[0], card, photoArea, d); };
  input.click();
}

function onDragOver(e, area) { e.preventDefault(); area.closest('.dish-card').classList.add('dragover'); }
function onDragLeave(e, area) { e.preventDefault(); area.closest('.dish-card').classList.remove('dragover'); }
function onDrop(e, area) {
  e.preventDefault();
  area.closest('.dish-card').classList.remove('dragover');
  const card = area.closest('.dish-card');
  const idx = parseInt(card.dataset.idx);
  const d = allDishes[idx];
  if (d && e.dataTransfer.files[0]) handleFile(e.dataTransfer.files[0], card, area, d);
}

function handleFile(file, card, photoArea, d) {
  if (!file.type.startsWith('image/')) { showToast('请选择图片文件', 'error'); return; }
  const btn = card.querySelector('.btn-upload');
  btn.classList.add('uploading');
  btn.textContent = '处理中...';
  const reader = new FileReader();
  reader.onload = (e) => {
    const img = new Image();
    img.onload = () => {
      const canvas = document.createElement('canvas');
      canvas.width = 400; canvas.height = 400;
      const ctx = canvas.getContext('2d');
      const scale = Math.max(400 / img.width, 400 / img.height);
      const sw = img.width * scale, sh = img.height * scale;
      const sx = (sw - 400) / 2, sy = (sh - 400) / 2;
      ctx.drawImage(img, -sx, -sy, sw, sh);
      const dataUrl = canvas.toDataURL('image/jpeg', 0.85);
      const base64 = dataUrl.split(',')[1];
      uploadPhoto(d.slug, d.name_cn, base64, card, photoArea, btn, d);
    };
    img.src = e.target.result;
  };
  reader.readAsDataURL(file);
}

async function uploadPhoto(slug, zhName, base64, card, photoArea, btn, d) {
  try {
    const resp = await fetch('/api/upload', {
      method: 'POST', headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ slug, zh_name: zhName, image_base64: base64 })
    });
    const result = await resp.json();
    if (result.success) {
      d.has_photo = true;
      d.photo_file = result.file;
      d._t = Date.now();
      card.classList.add('has-photo');
      photoArea.innerHTML = '<img src="/photos/' + result.file + '?t=' + Date.now() + '" alt="' + escAttr(zhName) + '"><div class="has-photo-badge">已上传</div>';
      btn.classList.remove('uploading');
      btn.textContent = '更换';
      const photoAll = allDishes.filter(x => x.has_photo).length;
      document.getElementById('stats').innerHTML = '<span class="done">' + photoAll + '</span> / ' + allDishes.length + ' 道菜已上传';
      showToast('「' + zhName + '」照片上传成功', 'success');
    } else {
      throw new Error(result.error || '上传失败');
    }
  } catch(err) {
    btn.classList.remove('uploading');
    btn.textContent = '重试';
    showToast('上传失败: ' + err.message, 'error');
  }
}

// ========== Tag Input ==========
function initTagInput(containerId, values, placeholder) {
  const container = document.getElementById(containerId);
  container.innerHTML = '';
  container.className = 'tag-input-container';
  values.forEach(v => container.appendChild(createTagChip(v)));
  const input = document.createElement('input');
  input.type = 'text';
  input.className = 'tag-input';
  input.placeholder = placeholder || '输入后按回车添加';
  input.onkeydown = (e) => {
    if (e.key === 'Enter') {
      e.preventDefault();
      const val = input.value.trim();
      if (val) { container.insertBefore(createTagChip(val), input); input.value = ''; }
    } else if (e.key === 'Backspace' && !input.value) {
      const chips = container.querySelectorAll('.tag-chip');
      if (chips.length > 0) chips[chips.length - 1].remove();
    }
  };
  container.appendChild(input);
}

function createTagChip(value) {
  const chip = document.createElement('span');
  chip.className = 'tag-chip';
  chip.dataset.value = value;
  chip.innerHTML = escHtml(value) + ' <span class="tag-remove" onclick="this.parentElement.remove()">&times;</span>';
  return chip;
}

function getTagValues(containerId) {
  return Array.from(document.getElementById(containerId).querySelectorAll('.tag-chip')).map(c => c.dataset.value);
}

// ========== Checkbox Group ==========
function initCheckboxGroup(containerId, options, selectedValues) {
  const container = document.getElementById(containerId);
  container.className = 'checkbox-group';
  container.innerHTML = Object.entries(options).map(([value, label]) => {
    const checked = selectedValues.includes(value) ? 'checked' : '';
    return '<label class="checkbox-label"><input type="checkbox" value="' + value + '" ' + checked + '> ' + label + '</label>';
  }).join('');
}

function setCheckboxGroup(containerId, selectedValues) {
  const container = document.getElementById(containerId);
  container.querySelectorAll('input[type="checkbox"]').forEach(cb => {
    cb.checked = selectedValues.includes(cb.value);
  });
}

function getCheckboxValues(containerId) {
  return Array.from(document.getElementById(containerId).querySelectorAll('input[type="checkbox"]:checked')).map(cb => cb.value);
}

// ========== 分类管理 ==========
function openCategoryManager() {
  const list = document.getElementById('categoryList');
  const cats = allCategories.filter(c => c.type === 'category');
  list.innerHTML = cats.map((c, i) => {
    return '<div class="manager-item" data-id="' + escAttr(c.id) + '">' +
      '<input type="text" class="mgr-cn" value="' + escAttr(c.label_cn) + '" placeholder="中文名">' +
      '<input type="text" class="mgr-en" value="' + escAttr(c.label_en) + '" placeholder="英文名">' +
      '<button class="mgr-btn" onclick="moveCategory(' + i + ', -1)">&uarr;</button>' +
      '<button class="mgr-btn" onclick="moveCategory(' + i + ', 1)">&darr;</button>' +
      '<button class="mgr-toggle ' + (c.active ? 'active' : 'inactive') + '" onclick="toggleCategoryActive(' + i + ')">' + (c.active ? '启用' : '停用') + '</button>' +
      '<button class="mgr-delete" onclick="deleteCategory(' + i + ')">删除</button>' +
      '<span class="mgr-count">' + (c.dish_count || 0) + ' 道菜</span>' +
    '</div>';
  }).join('');
  document.getElementById('categoryModal').classList.add('show');
}

function moveCategory(index, direction) {
  const cats = allCategories.filter(c => c.type === 'category');
  const newIndex = index + direction;
  if (newIndex < 0 || newIndex >= cats.length) return;
  const tempOrder = cats[index].order;
  cats[index].order = cats[newIndex].order;
  cats[newIndex].order = tempOrder;
  allCategories.sort((a, b) => (a.order || 0) - (b.order || 0));
  openCategoryManager();
}

function toggleCategoryActive(index) {
  const cats = allCategories.filter(c => c.type === 'category');
  cats[index].active = !cats[index].active;
  openCategoryManager();
}

function deleteCategory(index) {
  const cats = allCategories.filter(c => c.type === 'category');
  const cat = cats[index];
  if (cat.dish_count > 0) {
    showToast('该分类下还有 ' + cat.dish_count + ' 道菜，请先移动菜品后再删除。', 'error');
    return;
  }
  if (!confirm('确定删除分类「' + cat.label_cn + '」吗？')) return;
  const idx = allCategories.indexOf(cat);
  allCategories.splice(idx, 1);
  openCategoryManager();
}

async function saveCategories() {
  const items = document.querySelectorAll('#categoryList .manager-item');
  const categories = [];
  items.forEach((item, i) => {
    categories.push({
      id: item.dataset.id,
      label_cn: item.querySelector('.mgr-cn').value.trim(),
      label_en: item.querySelector('.mgr-en').value.trim(),
      order: i + 1,
      active: item.querySelector('.mgr-toggle').classList.contains('active')
    });
  });
  try {
    const resp = await fetch('/api/save_categories', {
      method: 'POST', headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ categories })
    });
    const result = await resp.json();
    if (result.success) {
      await loadDishes();
      closeModal('categoryModal');
      showToast('分类设置已保存', 'success');
    } else {
      throw new Error(result.error || '保存失败');
    }
  } catch(err) {
    showToast('保存失败: ' + err.message, 'error');
  }
}

// ========== 标签管理 ==========
function openTagManager() {
  const systemList = document.getElementById('systemTagList');
  systemList.innerHTML = SYSTEM_TAGS.map(t =>
    '<div class="manager-item"><input type="text" value="' + escAttr(t.label_cn) + '" disabled><span class="mgr-info">系统标签 · 不可删除</span></div>'
  ).join('');

  const customList = document.getElementById('customTagList');
  customList.innerHTML = (allCustomTags || []).map((t, i) => {
    const label = typeof t === 'string' ? t : (t.label || '');
    return '<div class="manager-item"><input type="text" class="mgr-tag-name" value="' + escAttr(label) + '" placeholder="标签名"><button class="mgr-delete" onclick="this.parentElement.remove()">删除</button></div>';
  }).join('');

  document.getElementById('tagModal').classList.add('show');
}

function addCustomTagRow() {
  const list = document.getElementById('customTagList');
  const div = document.createElement('div');
  div.className = 'manager-item';
  div.innerHTML = '<input type="text" class="mgr-tag-name" value="" placeholder="标签名"><button class="mgr-delete" onclick="this.parentElement.remove()">删除</button>';
  list.appendChild(div);
  div.querySelector('.mgr-tag-name').focus();
}

async function saveTags() {
  const items = document.querySelectorAll('#customTagList .manager-item');
  const customTags = [];
  items.forEach(item => {
    const name = item.querySelector('.mgr-tag-name').value.trim();
    if (name) customTags.push({ label: name });
  });
  try {
    const resp = await fetch('/api/save_custom_tags', {
      method: 'POST', headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ custom_tags: customTags })
    });
    const result = await resp.json();
    if (result.success) {
      allCustomTags = customTags;
      closeModal('tagModal');
      showToast('标签设置已保存', 'success');
    } else {
      throw new Error(result.error || '保存失败');
    }
  } catch(err) {
    showToast('保存失败: ' + err.message, 'error');
  }
}

// ========== 工具函数 ==========
function slugify(en) {
  let s = en.toLowerCase().trim();
  s = s.replace(/[^a-z0-9\s]/g, '');
  s = s.replace(/[\s]+/g, '_');
  s = s.replace(/_+/g, '_').replace(/^_|_$/g, '');
  return s || 'unnamed';
}

function escAttr(s) { return String(s || '').replace(/&/g,'&amp;').replace(/"/g,'&quot;').replace(/'/g,'&#39;').replace(/</g,'&lt;').replace(/>/g,'&gt;'); }
function escHtml(s) { return String(s || '').replace(/&/g,'&amp;').replace(/</g,'&lt;').replace(/>/g,'&gt;'); }

function closeModal(id) { document.getElementById(id).classList.remove('show'); }

document.addEventListener('keydown', (e) => {
  if (e.key === 'Escape') {
    ['editModal','addModal','categoryModal','tagModal'].forEach(id => closeModal(id));
  }
});

let toastTimer;
function showToast(msg, type) {
  const toast = document.getElementById('toast');
  toast.textContent = msg;
  toast.className = 'toast show ' + (type || '');
  clearTimeout(toastTimer);
  toastTimer = setTimeout(() => toast.classList.remove('show'), 2500);
}

loadDishes();
</script>

</body>
</html>
"""


# ========== HTTP 服务器 ==========
class PhotoManagerHandler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, format, *args):
        pass

    def _safe_dispatch(self, method):
        """统一异常保护，防止连接中断等错误崩溃服务器"""
        try:
            method()
        except (BrokenPipeError, ConnectionResetError, ConnectionAbortedError):
            pass  # 客户端断开，忽略
        except Exception as e:
            print(f"  [ERROR] {self.path}: {e}")
            try:
                self._json_response(500, {"error": str(e)})
            except Exception:
                pass

    def do_GET(self):
        self._safe_dispatch(self._do_get)

    def _do_get(self):
        parsed = urlparse(self.path)
        path = parsed.path
        if path == "/" or path == "/index.html":
            self._serve_html()
        elif path == "/api/dishes":
            self._serve_dishes()
        elif path == "/api/categories":
            self._serve_categories()
        elif path == "/api/custom_tags":
            self._serve_custom_tags()
        elif path.startswith("/photos/"):
            self._serve_photo(path)
        else:
            self._json_response(404, {"error": "Not found"})

    def do_POST(self):
        self._safe_dispatch(self._do_post)

    def _do_post(self):
        parsed = urlparse(self.path)
        if parsed.path == "/api/upload":
            self._handle_upload()
        elif parsed.path == "/api/edit_dish":
            self._handle_edit_dish()
        elif parsed.path == "/api/add_dish":
            self._handle_add_dish()
        elif parsed.path == "/api/delete_dish":
            self._handle_delete_dish()
        elif parsed.path == "/api/save_categories":
            self._handle_save_categories()
        elif parsed.path == "/api/save_custom_tags":
            self._handle_save_custom_tags()
        else:
            self._json_response(404, {"error": "Not found"})

    def _serve_html(self):
        body = HTML_PAGE.encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _serve_dishes(self):
        dishes = get_all_dishes()
        self._json_response(200, dishes)

    def _serve_categories(self):
        self._json_response(200, get_categories())

    def _serve_custom_tags(self):
        pool = load_dish_pool()
        tags = pool.get("custom_tags_def", [])
        self._json_response(200, tags)

    def _serve_photo(self, path):
        filename = os.path.basename(path)
        filepath = os.path.join(PHOTOS_DIR, filename)
        if not os.path.exists(filepath):
            self.send_response(404)
            self.send_header("Content-Length", "0")
            self.end_headers()
            return
        with open(filepath, "rb") as f:
            body = f.read()
        self.send_response(200)
        self.send_header("Content-Type", "image/jpeg")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-cache")
        self.end_headers()
        self.wfile.write(body)

    def _read_body(self):
        content_length = int(self.headers.get("Content-Length", 0))
        body = self.rfile.read(content_length)
        return json.loads(body.decode("utf-8"))

    def _handle_upload(self):
        try:
            data = self._read_body()
            slug = data.get("slug", "")
            zh_name = data.get("zh_name", "")
            image_b64 = data.get("image_base64", "")

            if not slug or not zh_name or not image_b64:
                self._json_response(400, {"success": False, "error": "缺少参数"})
                return

            image_data = base64.b64decode(image_b64)
            filename = f"{slug}.jpg"
            filepath = os.path.join(PHOTOS_DIR, filename)

            with open(filepath, "wb") as f:
                f.write(image_data)

            manifest = load_manifest()
            from datetime import date as _date
            manifest[zh_name] = {
                "file": filename,
                "uploaded": _date.today().isoformat(),
            }
            save_manifest(manifest)

            print(f"  [OK] 照片已保存: {filename} ({len(image_data) // 1024}KB) -> {zh_name}")
            self._json_response(200, {"success": True, "file": filename})

        except Exception as e:
            print(f"  [ERROR] 上传失败: {e}")
            self._json_response(500, {"success": False, "error": str(e)})

    def _handle_edit_dish(self):
        try:
            data = self._read_body()
            dish_id = data.get("id", "")

            if not dish_id:
                self._json_response(400, {"success": False, "error": "缺少 id"})
                return

            # V7: 写入 SQLite（唯一真相源）
            from db import get_db, log_event
            conn = get_db()
            try:
                row = conn.execute("SELECT name_cn FROM dishes WHERE id = ?", (dish_id,)).fetchone()
                if not row:
                    self._json_response(404, {"success": False, "error": f"菜品不存在: {dish_id}"})
                    return
                old_name = row["name_cn"]

                # 提取字段
                name_cn = data.get("name_cn", "").strip()
                name_en = data.get("name_en", "").strip()
                category_id = data.get("category_id", "")
                meal_tags = data.get("meal_tags", [])
                banquet = data.get("banquet", False)
                protein_types = data.get("protein_types", [])
                vegetables = data.get("vegetables", [])
                carb_type = data.get("carb_type")
                meal_components = data.get("meal_components", [])
                taste = data.get("taste", "normal")
                cooking_methods = data.get("cooking_methods", [])
                can_serve_warm = data.get("can_serve_warm", False)
                custom_tags = data.get("custom_tags", [])
                quick_soup = int(data.get("quick_soup", 0))
                slow_soup = int(data.get("slow_soup", 0))
                manual_only_for_breakfast = int(data.get("manual_only_for_breakfast", 0))

                conn.execute("""
                    UPDATE dishes SET
                        name_cn = ?, name_en = ?, category_id = ?,
                        meal_tags = ?, banquet = ?,
                        protein_types = ?, vegetables = ?,
                        vegetable_count = ?, carb_type = ?,
                        meal_components = ?, taste = ?,
                        cooking_methods = ?, can_serve_warm = ?,
                        custom_tags = ?,
                        quick_soup = ?, slow_soup = ?,
                        manual_only_for_breakfast = ?,
                        updated_at = datetime('now')
                    WHERE id = ?
                """, (
                    name_cn, name_en, category_id,
                    json.dumps(meal_tags, ensure_ascii=False), 1 if banquet else 0,
                    json.dumps(protein_types, ensure_ascii=False), json.dumps(vegetables, ensure_ascii=False),
                    len(vegetables), carb_type,
                    json.dumps(meal_components, ensure_ascii=False), taste,
                    json.dumps(cooking_methods, ensure_ascii=False), 1 if can_serve_warm else 0,
                    json.dumps(custom_tags, ensure_ascii=False),
                    quick_soup, slow_soup, manual_only_for_breakfast,
                    dish_id
                ))
                conn.commit()
                log_event("dish_edited", "dishes", dish_id, {
                    "old_name": old_name, "new_name": name_cn, "via": "photo_manager"
                })
            finally:
                conn.close()

            # 同步 photo manifest（重命名照片映射）
            manifest = load_manifest()
            has_photo = False
            photo_file = ""
            if old_name != name_cn and old_name in manifest:
                manifest[name_cn] = manifest.pop(old_name)
                save_manifest(manifest)
                has_photo = True
                photo_file = manifest[name_cn].get("file", "")
            elif name_cn in manifest:
                has_photo = True
                photo_file = manifest[name_cn].get("file", "")

            new_slug = slugify(name_en)
            print(f"  [OK] 编辑菜品 (SQLite): {old_name} -> {name_cn}")
            self._json_response(200, {
                "success": True,
                "new_slug": new_slug,
                "has_photo": has_photo,
                "photo_file": photo_file,
            })

        except Exception as e:
            print(f"  [ERROR] 编辑失败: {e}")
            self._json_response(500, {"success": False, "error": str(e)})

    def _handle_add_dish(self):
        try:
            data = self._read_body()
            name_cn = data.get("name_cn", "").strip()
            name_en = data.get("name_en", "").strip()
            category_id = data.get("category_id", "")
            meal_tags = data.get("meal_tags", [])
            banquet = data.get("banquet", False)

            if not name_cn or not name_en or not category_id:
                self._json_response(400, {"success": False, "error": "缺少参数"})
                return

            # V7: 写入 SQLite（唯一真相源）
            from db import get_db, log_event
            conn = get_db()
            try:
                # 重名检查（is_active=1）
                existing = conn.execute(
                    "SELECT id FROM dishes WHERE name_cn = ? AND is_active = 1",
                    (name_cn,)
                ).fetchone()
                if existing:
                    self._json_response(400, {"success": False, "error": f"菜品已存在: {name_cn}"})
                    return

                # 生成新 ID
                cur = conn.execute("SELECT id FROM dishes WHERE id LIKE 'dish_%' ORDER BY id DESC LIMIT 1")
                last_id = cur.fetchone()
                if last_id:
                    last_num = int(last_id["id"].split("_")[1])
                    new_id = f"dish_{last_num + 1:04d}"
                else:
                    new_id = "dish_0001"

                quick_soup = int(data.get("quick_soup", 0))
                slow_soup = int(data.get("slow_soup", 0))
                manual_only_for_breakfast = int(data.get("manual_only_for_breakfast", 0))

                conn.execute("""
                    INSERT INTO dishes (
                        id, name_cn, name_en, category_id, meal_tags, banquet,
                        protein_types, vegetables, vegetable_count, carb_type,
                        meal_components, taste, cooking_methods, can_serve_warm,
                        custom_tags, needs_review, image, image_uploaded,
                        quick_soup, slow_soup, manual_only_for_breakfast,
                        is_active, created_at, updated_at
                    ) VALUES (
                        ?, ?, ?, ?, ?, ?,
                        '[]', '[]', 0, NULL,
                        '[]', 'normal', '[]', 0,
                        '[]', 0, NULL, 0,
                        ?, ?, ?,
                        1, datetime('now'), datetime('now')
                    )
                """, (
                    new_id, name_cn, name_en, category_id,
                    json.dumps(meal_tags, ensure_ascii=False), 1 if banquet else 0,
                    quick_soup, slow_soup, manual_only_for_breakfast
                ))
                conn.commit()
                log_event("dish_added", "dishes", new_id, {
                    "name_cn": name_cn, "name_en": name_en, "via": "photo_manager"
                })
            finally:
                conn.close()

            new_slug = slugify(name_en)
            print(f"  [OK] 添加菜品 (SQLite): {name_cn} / {name_en} -> {category_id}")
            self._json_response(200, {"success": True, "slug": new_slug, "id": new_id})

        except Exception as e:
            print(f"  [ERROR] 添加失败: {e}")
            self._json_response(500, {"success": False, "error": str(e)})

    def _handle_delete_dish(self):
        try:
            data = self._read_body()
            dish_id = data.get("id", "")
            name_cn = data.get("name_cn", "")

            if not dish_id:
                self._json_response(400, {"success": False, "error": "缺少 id"})
                return

            # V7: Soft Delete — SQLite 是唯一真相源
            from db import get_db, log_event
            conn = get_db()
            try:
                dish = conn.execute("SELECT name_cn FROM dishes WHERE id = ?", (dish_id,)).fetchone()
                if not dish:
                    self._json_response(404, {"success": False, "error": f"菜品不存在: {dish_id}"})
                    return

                name_cn = dish["name_cn"]

                # Soft Delete: is_active = 0, deleted_at = timestamp
                conn.execute(
                    "UPDATE dishes SET is_active = 0, deleted_at = datetime('now') WHERE id = ?",
                    (dish_id,)
                )
                conn.commit()

                log_event("dish_soft_deleted", "dishes", dish_id, {
                    "name_cn": name_cn, "deleted_via": "photo_manager"
                })
            finally:
                conn.close()

            # 删除照片
            manifest = load_manifest()
            if name_cn in manifest:
                photo_file = manifest[name_cn].get("file", "")
                if photo_file:
                    photo_path = os.path.join(PHOTOS_DIR, photo_file)
                    if os.path.exists(photo_path):
                        os.remove(photo_path)
                del manifest[name_cn]
                save_manifest(manifest)

            print(f"  [OK] 菜品已下架(Soft Delete): {name_cn}")
            self._json_response(200, {"success": True, "soft_delete": True})

        except Exception as e:
            print(f"  [ERROR] 删除失败: {e}")
            self._json_response(500, {"success": False, "error": str(e)})

    def _handle_save_categories(self):
        try:
            data = self._read_body()
            categories = data.get("categories", [])

            # V7: 写入 SQLite categories 表
            from db import get_db
            conn = get_db()
            try:
                # 简单做法：全量替换
                conn.execute("DELETE FROM categories")
                for c in categories:
                    conn.execute("""
                        INSERT INTO categories (id, label_cn, label_en, sort_order, active)
                        VALUES (?, ?, ?, ?, ?)
                    """, (
                        c.get("id", ""),
                        c.get("label_cn", c.get("id", "")),
                        c.get("label_en", ""),
                        c.get("order", c.get("sort_order", 0)),
                        1 if c.get("active", True) else 0,
                    ))
                conn.commit()
            finally:
                conn.close()

            print(f"  [OK] 保存分类设置 (SQLite, {len(categories)} 个分类)")
            self._json_response(200, {"success": True})

        except Exception as e:
            print(f"  [ERROR] 保存分类失败: {e}")
            self._json_response(500, {"success": False, "error": str(e)})

    def _handle_save_custom_tags(self):
        try:
            data = self._read_body()
            custom_tags = data.get("custom_tags", [])

            # V7: 写入 SQLite custom_tags_def 表
            from db import get_db
            conn = get_db()
            try:
                conn.execute("DELETE FROM custom_tags_def")
                for t in custom_tags:
                    label = t.get("label", "") if isinstance(t, dict) else str(t)
                    if label:
                        conn.execute(
                            "INSERT INTO custom_tags_def (label) VALUES (?)",
                            (label,)
                        )
                conn.commit()
            finally:
                conn.close()

            print(f"  [OK] 保存自定义标签 (SQLite, {len(custom_tags)} 个)")
            self._json_response(200, {"success": True})

        except Exception as e:
            print(f"  [ERROR] 保存标签失败: {e}")
            self._json_response(500, {"success": False, "error": str(e)})

    def _json_response(self, code, data):
        body = json.dumps(data, ensure_ascii=False).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)


def main():
    ensure_dirs()

    if not os.path.exists(DISH_POOL_FILE):
        print(f"[ERROR] 找不到 {DISH_POOL_FILE}")
        print("  请确保在项目根目录下运行此脚本")
        return

    dishes = get_all_dishes()
    manifest = load_manifest()
    photo_count = len(manifest)

    print("=" * 55)
    print("  🍳 菜品管理器 v2.0（结构化字段 + 分类/标签管理）")
    print("=" * 55)
    print(f"  菜品总数: {len(dishes)} 道")
    print(f"  已传照片: {photo_count} 道")
    print(f"  待传照片: {len(dishes) - photo_count} 道")
    print(f"  照片目录: {PHOTOS_DIR}")
    print(f"  映射文件: {MANIFEST_FILE}")
    print(f"  菜谱文件: {DISH_POOL_FILE}")
    print("-" * 55)
    print(f"  浏览器打开: http://localhost:{PORT}")
    print("  操作完成后关闭此窗口即可")
    print("  之后执行 ./sync.sh 同步到 GitHub")
    print("=" * 55)
    print()

    webbrowser.open(f"http://localhost:{PORT}")

    server = ThreadingHTTPServer(("0.0.0.0", PORT), PhotoManagerHandler)
    server.daemon_threads = True
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\n\n服务器已关闭。别忘了运行 ./sync.sh 同步到 GitHub！")
        server.server_close()


if __name__ == "__main__":
    main()
