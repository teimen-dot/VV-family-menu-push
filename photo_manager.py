#!/usr/bin/env python3
"""
家庭菜单管家 - 本地菜品管理器
启动后在浏览器中可视化上传菜品照片、编辑/添加/删除菜品。
浏览器自动裁剪统一尺寸，零手动操作。

用法: python photo_manager.py
然后浏览器自动打开 http://localhost:8080
"""

import json
import os
import re
import base64
import webbrowser
from http.server import HTTPServer, BaseHTTPRequestHandler
from urllib.parse import urlparse

# ========== 路径配置 ==========
BASE_DIR = os.path.dirname(os.path.abspath(__file__))
DISH_POOL_FILE = os.path.join(BASE_DIR, "dish_pool.json")
MANIFEST_FILE = os.path.join(BASE_DIR, "photo_manifest.json")
PHOTOS_DIR = os.path.join(BASE_DIR, "photos")
PORT = 8080


def slugify(en_name):
    """英文名转文件名 slug: 'Pan-fried Wagyu Beef' -> 'pan_fried_wagyu_beef'"""
    slug = en_name.lower().strip()
    slug = re.sub(r"[^a-z0-9\s]", "", slug)
    slug = re.sub(r"[\s]+", "_", slug)
    slug = re.sub(r"_+", "_", slug).strip("_")
    return slug if slug else "unnamed"


def load_dish_pool():
    """加载 dish_pool.json"""
    with open(DISH_POOL_FILE, "r", encoding="utf-8") as f:
        return json.load(f)


def save_dish_pool(pool):
    """保存 dish_pool.json"""
    with open(DISH_POOL_FILE, "w", encoding="utf-8") as f:
        json.dump(pool, f, ensure_ascii=False, indent=2)


def get_all_dishes():
    """从 dish_pool.json 提取所有菜品，返回 [{zh, en, category, category_label}]"""
    pool = load_dish_pool()
    dishes = []

    # 分类菜品
    for cat_key, cat_data in pool.get("categories", {}).items():
        label = cat_data.get("label", cat_key)
        for dish in cat_data.get("dishes", []):
            dishes.append({
                "zh": dish.get("zh", ""),
                "en": dish.get("en", ""),
                "category": cat_key,
                "category_label": label,
            })

    # 轮换池菜品
    for pool_key, pool_data in pool.get("rotation_pools", {}).items():
        label = pool_data.get("description", pool_key)
        for item in pool_data.get("items", []):
            dishes.append({
                "zh": item.get("zh", ""),
                "en": item.get("en", ""),
                "category": pool_key,
                "category_label": label,
            })

    return dishes


def get_categories():
    """获取所有分类列表（用于添加菜品下拉框）"""
    pool = load_dish_pool()
    cats = []
    for cat_key, cat_data in pool.get("categories", {}).items():
        cats.append({"key": cat_key, "label": cat_data.get("label", cat_key), "type": "category"})
    for pool_key, pool_data in pool.get("rotation_pools", {}).items():
        cats.append({"key": pool_key, "label": pool_data.get("description", pool_key), "type": "rotation_pool"})
    return cats


def load_manifest():
    """加载照片映射"""
    if os.path.exists(MANIFEST_FILE):
        with open(MANIFEST_FILE, "r", encoding="utf-8") as f:
            return json.load(f)
    return {}


def save_manifest(manifest):
    """保存照片映射"""
    with open(MANIFEST_FILE, "w", encoding="utf-8") as f:
        json.dump(manifest, f, ensure_ascii=False, indent=2)


def ensure_dirs():
    """确保目录和文件存在"""
    os.makedirs(PHOTOS_DIR, exist_ok=True)
    if not os.path.exists(MANIFEST_FILE):
        save_manifest({})


# ========== HTML 界面 ==========
HTML_PAGE = r"""<!DOCTYPE html>
<html lang="zh">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>菜品管理器</title>
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
.header-actions { display: flex; align-items: center; gap: 12px; }
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
  margin: 12px auto 0;
  padding: 0 30px;
  display: flex;
  gap: 8px;
  flex-wrap: wrap;
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
  height: 150px;
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
.card-actions button:first-child { border-right: 1px solid #e8e8ed; }
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
  box-shadow: 0 8px 32px rgba(0,0,0,0.15);
}
.modal h3 {
  font-size: 18px;
  margin-bottom: 18px;
  font-weight: 700;
}
.modal label {
  display: block;
  font-size: 13px;
  font-weight: 600;
  color: #6e6e73;
  margin-bottom: 4px;
  margin-top: 14px;
}
.modal label:first-of-type { margin-top: 0; }
.modal input, .modal select {
  width: 100%;
  padding: 10px 14px;
  font-size: 15px;
  border: 2px solid #e0e0e0;
  border-radius: 10px;
  outline: none;
  transition: border-color 0.2s;
}
.modal input:focus, .modal select:focus { border-color: #007aff; }
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
      <button class="btn-add" onclick="openAddModal()">+ 添加菜品</button>
    </div>
  </div>
</div>

<div class="search-box">
  <input type="text" id="searchInput" placeholder="搜索菜名（中英文均可）..." oninput="filterDishes()">
</div>

<div class="filter-bar" id="filterBar"></div>

<div class="container" id="container">
  <div class="empty-msg">正在加载菜品数据...</div>
</div>

<!-- Edit Modal -->
<div class="modal-overlay" id="editModal">
  <div class="modal">
    <h3>编辑菜品</h3>
    <label>中文名</label>
    <input type="text" id="editZh" placeholder="中文名">
    <label>英文名</label>
    <input type="text" id="editEn" placeholder="英文名">
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
    <label>分类</label>
    <select id="addCategory"></select>
    <label>中文名</label>
    <input type="text" id="addZh" placeholder="中文名">
    <label>英文名</label>
    <input type="text" id="addEn" placeholder="英文名">
    <div class="modal-actions">
      <button class="btn-cancel" onclick="closeModal('addModal')">取消</button>
      <button class="btn-save" onclick="saveAdd()">添加</button>
    </div>
  </div>
</div>

<div class="toast" id="toast"></div>

<script>
let allDishes = [];
let allCategories = [];
let currentFilter = 'all';
let editingDish = null; // {category, old_zh}

async function loadDishes() {
  const resp = await fetch('/api/dishes');
  allDishes = await resp.json();

  const catResp = await fetch('/api/categories');
  allCategories = await catResp.json();

  renderFilters();
  renderAddCategories();
  render();
}

function renderAddCategories() {
  const sel = document.getElementById('addCategory');
  sel.innerHTML = allCategories.map(c =>
    `<option value="${c.key}">${c.label}</option>`
  ).join('');
}

function renderFilters() {
  const cats = {};
  allDishes.forEach(d => {
    if (!cats[d.category_label]) cats[d.category_label] = d.category;
  });
  const bar = document.getElementById('filterBar');
  let html = `<button class="filter-btn active" onclick="setFilter('all', this)">全部</button>`;
  Object.entries(cats).forEach(([label, key]) => {
    html += `<button class="filter-btn" onclick="setFilter('${key}', this)">${label}</button>`;
  });
  bar.innerHTML = html;
}

function setFilter(cat, btn) {
  currentFilter = cat;
  document.querySelectorAll('.filter-btn').forEach(b => b.classList.remove('active'));
  btn.classList.add('active');
  render();
}

function filterDishes() { render(); }

function render() {
  const search = document.getElementById('searchInput').value.toLowerCase().trim();
  const container = document.getElementById('container');

  const grouped = {};
  let totalCount = 0, photoCount = 0;

  allDishes.forEach(d => {
    if (currentFilter !== 'all' && d.category !== currentFilter) return;
    if (search) {
      const text = (d.zh + ' ' + d.en).toLowerCase();
      if (!text.includes(search)) return;
    }
    if (!grouped[d.category_label]) grouped[d.category_label] = [];
    grouped[d.category_label].push(d);
    totalCount++;
    if (d.has_photo) photoCount++;
  });

  // Update stats (all dishes, not filtered)
  const totalAll = allDishes.length;
  const photoAll = allDishes.filter(d => d.has_photo).length;
  document.getElementById('stats').innerHTML =
    `<span class="done">${photoAll}</span> / ${totalAll} 道菜已上传`;

  if (totalCount === 0) {
    container.innerHTML = '<div class="empty-msg">没有匹配的菜品</div>';
    return;
  }

  let html = '';
  Object.entries(grouped).forEach(([label, dishes]) => {
    html += `<div class="category-section">`;
    html += `<div class="category-title">${label}（${dishes.length}）</div>`;
    html += `<div class="dish-grid">`;
    dishes.forEach(d => {
      const photoHtml = d.has_photo
        ? `<img src="/photos/${d.photo_file}?t=${d._t || Date.now()}" alt="${escAttr(d.zh)}">
           <div class="has-photo-badge">已上传</div>`
        : `<div class="photo-placeholder">🍽️</div>`;
      html += `
        <div class="dish-card ${d.has_photo ? 'has-photo' : ''}" data-zh="${escAttr(d.zh)}" data-slug="${d.slug}" data-category="${escAttr(d.category)}" data-en="${escAttr(d.en)}">
          <div class="dish-photo-area" onclick="triggerUpload(this)" ondragover="onDragOver(event,this)" ondragleave="onDragLeave(event,this)" ondrop="onDrop(event,this)">
            ${photoHtml}
          </div>
          <div class="dish-info">
            <div class="dish-zh">${escHtml(d.zh)}</div>
            <div class="dish-en">${escHtml(d.en)}</div>
          </div>
          <div class="card-actions">
            <button class="btn-upload" onclick="event.stopPropagation();triggerUpload(this.closest('.dish-card').querySelector('.dish-photo-area'))">
              ${d.has_photo ? '更换' : '上传'}
            </button>
            <button class="btn-edit" onclick="event.stopPropagation();openEditModal(this.closest('.dish-card'))">编辑</button>
            <button class="btn-delete" onclick="event.stopPropagation();deleteDish(this.closest('.dish-card'))">删除</button>
          </div>
        </div>`;
    });
    html += `</div></div>`;
  });

  container.innerHTML = html;
}

function escAttr(s) { return String(s).replace(/"/g, '&quot;').replace(/'/g, '&#39;'); }
function escHtml(s) { return String(s).replace(/</g,'&lt;').replace(/>/g,'&gt;'); }

// ===== Photo Upload =====
function triggerUpload(photoArea) {
  const card = photoArea.closest('.dish-card');
  const input = document.createElement('input');
  input.type = 'file';
  input.accept = 'image/*';
  input.onchange = (e) => {
    if (e.target.files[0]) handleFile(e.target.files[0], card, photoArea);
  };
  input.click();
}

function onDragOver(e, area) {
  e.preventDefault();
  area.closest('.dish-card').classList.add('dragover');
}
function onDragLeave(e, area) {
  e.preventDefault();
  area.closest('.dish-card').classList.remove('dragover');
}
function onDrop(e, area) {
  e.preventDefault();
  area.closest('.dish-card').classList.remove('dragover');
  const card = area.closest('.dish-card');
  if (e.dataTransfer.files[0]) handleFile(e.dataTransfer.files[0], card, area);
}

function handleFile(file, card, photoArea) {
  if (!file.type.startsWith('image/')) {
    showToast('请选择图片文件', 'error');
    return;
  }

  const btn = card.querySelector('.btn-upload');
  btn.classList.add('uploading');
  btn.textContent = '处理中...';

  const reader = new FileReader();
  reader.onload = (e) => {
    const img = new Image();
    img.onload = () => {
      const canvas = document.createElement('canvas');
      canvas.width = 400;
      canvas.height = 300;
      const ctx = canvas.getContext('2d');

      const scale = Math.max(400 / img.width, 300 / img.height);
      const sw = img.width * scale;
      const sh = img.height * scale;
      const sx = (sw - 400) / 2;
      const sy = (sh - 300) / 2;

      ctx.drawImage(img, -sx, -sy, sw, sh);
      const dataUrl = canvas.toDataURL('image/jpeg', 0.85);
      const base64 = dataUrl.split(',')[1];

      uploadPhoto(card.dataset.slug, card.dataset.zh, base64, card, photoArea, btn);
    };
    img.src = e.target.result;
  };
  reader.readAsDataURL(file);
}

async function uploadPhoto(slug, zhName, base64, card, photoArea, btn) {
  try {
    const resp = await fetch('/api/upload', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ slug, zh_name: zhName, image_base64: base64 })
    });
    const result = await resp.json();

    if (result.success) {
      const dish = allDishes.find(d => d.zh === zhName);
      if (dish) {
        dish.has_photo = true;
        dish.photo_file = result.file;
        dish._t = Date.now();
      }
      card.classList.add('has-photo');
      photoArea.innerHTML =
        `<img src="/photos/${result.file}?t=${Date.now()}" alt="${escAttr(zhName)}">
         <div class="has-photo-badge">已上传</div>`;
      btn.classList.remove('uploading');
      btn.textContent = '更换';
      const photoAll = allDishes.filter(d => d.has_photo).length;
      document.getElementById('stats').innerHTML =
        `<span class="done">${photoAll}</span> / ${allDishes.length} 道菜已上传`;
      showToast(`「${zhName}」照片上传成功`, 'success');
    } else {
      throw new Error(result.error || '上传失败');
    }
  } catch (err) {
    btn.classList.remove('uploading');
    btn.textContent = '重试';
    showToast('上传失败: ' + err.message, 'error');
  }
}

// ===== Edit Dish =====
function openEditModal(card) {
  editingDish = {
    category: card.dataset.category,
    old_zh: card.dataset.zh,
  };
  // Find current dish data
  const dish = allDishes.find(d => d.zh === card.dataset.zh && d.category === card.dataset.category);
  document.getElementById('editZh').value = dish ? dish.zh : card.dataset.zh;
  document.getElementById('editEn').value = dish ? dish.en : card.dataset.en;
  document.getElementById('editModal').classList.add('show');
  document.getElementById('editZh').focus();
}

async function saveEdit() {
  const newZh = document.getElementById('editZh').value.trim();
  const newEn = document.getElementById('editEn').value.trim();

  if (!newZh || !newEn) {
    showToast('中英文名都不能为空', 'error');
    return;
  }

  try {
    const resp = await fetch('/api/edit_dish', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({
        category: editingDish.category,
        old_zh: editingDish.old_zh,
        new_zh: newZh,
        new_en: newEn,
      })
    });
    const result = await resp.json();

    if (result.success) {
      // Update local data
      const dish = allDishes.find(d => d.zh === editingDish.old_zh && d.category === editingDish.category);
      if (dish) {
        dish.zh = newZh;
        dish.en = newEn;
        dish.slug = result.new_slug || slugify(newEn);
        if (result.has_photo) {
          dish.has_photo = true;
          dish.photo_file = result.photo_file;
          dish._t = Date.now();
        }
      }
      closeModal('editModal');
      render();
      renderFilters();
      showToast(`已更新：「${newZh}」`, 'success');
    } else {
      throw new Error(result.error || '更新失败');
    }
  } catch (err) {
    showToast('更新失败: ' + err.message, 'error');
  }
}

// ===== Add Dish =====
function openAddModal() {
  document.getElementById('addZh').value = '';
  document.getElementById('addEn').value = '';
  document.getElementById('addModal').classList.add('show');
  document.getElementById('addZh').focus();
}

async function saveAdd() {
  const category = document.getElementById('addCategory').value;
  const zh = document.getElementById('addZh').value.trim();
  const en = document.getElementById('addEn').value.trim();

  if (!zh || !en) {
    showToast('中英文名都不能为空', 'error');
    return;
  }

  try {
    const resp = await fetch('/api/add_dish', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ category, zh, en })
    });
    const result = await resp.json();

    if (result.success) {
      // Add to local data
      const catInfo = allCategories.find(c => c.key === category);
      allDishes.push({
        zh, en,
        category,
        category_label: catInfo ? catInfo.label : category,
        slug: result.slug,
        has_photo: false,
        photo_file: '',
      });
      closeModal('addModal');
      render();
      renderFilters();
      showToast(`已添加：「${zh}」`, 'success');
    } else {
      throw new Error(result.error || '添加失败');
    }
  } catch (err) {
    showToast('添加失败: ' + err.message, 'error');
  }
}

// ===== Delete Dish =====
async function deleteDish(card) {
  const zh = card.dataset.zh;
  const category = card.dataset.category;

  if (!confirm(`确定删除「${zh}」吗？\n如果有照片，照片也会一起删除。`)) return;

  try {
    const resp = await fetch('/api/delete_dish', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ category, zh })
    });
    const result = await resp.json();

    if (result.success) {
      allDishes = allDishes.filter(d => !(d.zh === zh && d.category === category));
      render();
      renderFilters();
      showToast(`已删除：「${zh}」`, 'success');
    } else {
      throw new Error(result.error || '删除失败');
    }
  } catch (err) {
    showToast('删除失败: ' + err.message, 'error');
  }
}

// ===== Utils =====
function slugify(en) {
  let s = en.toLowerCase().trim();
  s = s.replace(/[^a-z0-9\s]/g, '');
  s = s.replace(/[\s]+/g, '_');
  s = s.replace(/_+/g, '_').replace(/^_|_$/g, '');
  return s || 'unnamed';
}

function closeModal(id) {
  document.getElementById(id).classList.remove('show');
}

document.addEventListener('keydown', (e) => {
  if (e.key === 'Escape') {
    closeModal('editModal');
    closeModal('addModal');
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
    def log_message(self, format, *args):
        pass  # 静默日志

    def do_GET(self):
        parsed = urlparse(self.path)
        path = parsed.path

        if path == "/" or path == "/index.html":
            self._serve_html()
        elif path == "/api/dishes":
            self._serve_dishes()
        elif path == "/api/categories":
            self._serve_categories()
        elif path.startswith("/photos/"):
            self._serve_photo(path)
        else:
            self._json_response(404, {"error": "Not found"})

    def do_POST(self):
        parsed = urlparse(self.path)
        if parsed.path == "/api/upload":
            self._handle_upload()
        elif parsed.path == "/api/edit_dish":
            self._handle_edit_dish()
        elif parsed.path == "/api/add_dish":
            self._handle_add_dish()
        elif parsed.path == "/api/delete_dish":
            self._handle_delete_dish()
        else:
            self._json_response(404, {"error": "Not found"})

    def _serve_html(self):
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.end_headers()
        self.wfile.write(HTML_PAGE.encode("utf-8"))

    def _serve_dishes(self):
        dishes = get_all_dishes()
        manifest = load_manifest()

        result = []
        for d in dishes:
            slug = slugify(d["en"])
            zh = d["zh"]
            has_photo = zh in manifest
            result.append({
                "zh": zh,
                "en": d["en"],
                "category": d["category"],
                "category_label": d["category_label"],
                "slug": slug,
                "has_photo": has_photo,
                "photo_file": manifest.get(zh, {}).get("file", "") if has_photo else "",
            })

        self._json_response(200, result)

    def _serve_categories(self):
        self._json_response(200, get_categories())

    def _serve_photo(self, path):
        filename = os.path.basename(path)
        filepath = os.path.join(PHOTOS_DIR, filename)

        if not os.path.exists(filepath):
            self.send_response(404)
            self.end_headers()
            return

        self.send_response(200)
        self.send_header("Content-Type", "image/jpeg")
        self.send_header("Cache-Control", "no-cache")
        self.end_headers()
        with open(filepath, "rb") as f:
            self.wfile.write(f.read())

    def _read_body(self):
        """读取 POST body 并解析 JSON"""
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
            category = data.get("category", "")
            old_zh = data.get("old_zh", "")
            new_zh = data.get("new_zh", "").strip()
            new_en = data.get("new_en", "").strip()

            if not category or not old_zh or not new_zh or not new_en:
                self._json_response(400, {"success": False, "error": "缺少参数"})
                return

            pool = load_dish_pool()

            # 找到菜品所在的数组
            dish_array, is_rotation = self._find_dish_array(pool, category)
            if dish_array is None:
                self._json_response(400, {"success": False, "error": f"分类不存在: {category}"})
                return

            # 找到菜品并更新
            found = False
            for item in dish_array:
                if item.get("zh") == old_zh:
                    item["zh"] = new_zh
                    item["en"] = new_en
                    found = True
                    break

            if not found:
                self._json_response(404, {"success": False, "error": f"菜品不存在: {old_zh}"})
                return

            save_dish_pool(pool)

            # 同步 photo_manifest：如果菜名变了，更新 manifest key
            manifest = load_manifest()
            has_photo = False
            photo_file = ""
            if old_zh != new_zh and old_zh in manifest:
                manifest[new_zh] = manifest.pop(old_zh)
                save_manifest(manifest)
                has_photo = True
                photo_file = manifest[new_zh].get("file", "")
            elif new_zh in manifest:
                has_photo = True
                photo_file = manifest[new_zh].get("file", "")

            new_slug = slugify(new_en)
            print(f"  [OK] 编辑菜品: {old_zh} -> {new_zh} / {new_en}")
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
            category = data.get("category", "")
            zh = data.get("zh", "").strip()
            en = data.get("en", "").strip()

            if not category or not zh or not en:
                self._json_response(400, {"success": False, "error": "缺少参数"})
                return

            pool = load_dish_pool()

            dish_array, is_rotation = self._find_dish_array(pool, category)
            if dish_array is None:
                self._json_response(400, {"success": False, "error": f"分类不存在: {category}"})
                return

            # 检查重名
            for item in dish_array:
                if item.get("zh") == zh:
                    self._json_response(400, {"success": False, "error": f"菜品已存在: {zh}"})
                    return

            # 创建新菜品
            new_dish = {"zh": zh, "en": en, "ingredients": [], "tags": []}
            if is_rotation and category == "porridge":
                new_dish["has_yam"] = False
            dish_array.append(new_dish)

            save_dish_pool(pool)

            new_slug = slugify(en)
            print(f"  [OK] 添加菜品: {zh} / {en} -> {category}")
            self._json_response(200, {"success": True, "slug": new_slug})

        except Exception as e:
            print(f"  [ERROR] 添加失败: {e}")
            self._json_response(500, {"success": False, "error": str(e)})

    def _handle_delete_dish(self):
        try:
            data = self._read_body()
            category = data.get("category", "")
            zh = data.get("zh", "")

            if not category or not zh:
                self._json_response(400, {"success": False, "error": "缺少参数"})
                return

            pool = load_dish_pool()

            dish_array, is_rotation = self._find_dish_array(pool, category)
            if dish_array is None:
                self._json_response(400, {"success": False, "error": f"分类不存在: {category}"})
                return

            # 找到并删除菜品
            found = False
            for i, item in enumerate(dish_array):
                if item.get("zh") == zh:
                    dish_array.pop(i)
                    found = True
                    break

            if not found:
                self._json_response(404, {"success": False, "error": f"菜品不存在: {zh}"})
                return

            save_dish_pool(pool)

            # 删除照片（如果有）
            manifest = load_manifest()
            if zh in manifest:
                photo_file = manifest[zh].get("file", "")
                if photo_file:
                    photo_path = os.path.join(PHOTOS_DIR, photo_file)
                    if os.path.exists(photo_path):
                        os.remove(photo_path)
                del manifest[zh]
                save_manifest(manifest)

            print(f"  [OK] 删除菜品: {zh} <- {category}")
            self._json_response(200, {"success": True})

        except Exception as e:
            print(f"  [ERROR] 删除失败: {e}")
            self._json_response(500, {"success": False, "error": str(e)})

    def _find_dish_array(self, pool, category):
        """
        根据分类 key 找到菜品数组。
        返回 (array, is_rotation) 或 (None, False)。
        """
        # 先在 categories 中找
        if category in pool.get("categories", {}):
            return pool["categories"][category].get("dishes", []), False
        # 再在 rotation_pools 中找
        if category in pool.get("rotation_pools", {}):
            return pool["rotation_pools"][category].get("items", []), True
        return None, False

    def _json_response(self, code, data):
        self.send_response(code)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.end_headers()
        self.wfile.write(json.dumps(data, ensure_ascii=False).encode("utf-8"))


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
    print("  🍳 菜品管理器（照片 + 编辑 + 添加 + 删除）")
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

    server = HTTPServer(("0.0.0.0", PORT), PhotoManagerHandler)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\n\n服务器已关闭。别忘了运行 ./sync.sh 同步到 GitHub！")
        server.server_close()


if __name__ == "__main__":
    main()
