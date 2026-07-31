# V7 修复报告：彻底统一真相源

**日期**：2026-07-31 11:10
**触发问题**：用户报告 H5 前端菜品库出现"薄切牛肉片"，但菜品管理器里没有
**根本原因**：菜品管理器 (photo_manager.py 8080) 还在用 V3 老数据源 `dish_pool.json`，与 SQLite 完全分裂
**用户核心诉求**："底层逻辑是要根据我目前的菜品管理器来"

---

## 一、问题诊断

### 1.1 数据源分裂现状（修复前）

| 端点 | 数据源 | 菜品数 | "薄切牛肉片"数量 |
|------|--------|--------|------------------|
| 菜品管理器 8080 | `dish_pool.json` | 203 | **1 条** (dish_0010) |
| H5 前端 8090 | SQLite | 203 | **2 条** (dish_0004 + dish_0010) |

### 1.2 4+4 道菜的"幽灵数据"

**SQLite 多出 4 道幽灵菜**（在菜品管理器里没有）：
- dish_0004 薄切牛肉片 / Thinly Sliced Beef
- dish_0009 香煎澳洲和牛片 / Pan-fried Australian Wagyu Beef
- dish_0022 红椒鸡丁西葫芦 / Diced Chicken with Zucchini & Red Pepper
- dish_0143 茶碗蒸 / Chawanmushi

**dish_pool.json 多出 4 道菜**（在 SQLite 里没有）：
- dish_0209 南瓜饼 / Pan-Fried Pumpkin Cakes
- dish_0210 牛油果虾仁滑蛋酸种面包早餐盘+水果 / Western-Style Breakfast & fruit
- dish_0211 芋头 / Taro
- dish_0212 肉饼汤 / Pork Patty Soup

### 1.3 V6 修复为什么没解决问题

V6 我做了：
- ✅ `inventory.check_dish_availability` 改用 SQLite
- ✅ `menu_service._load_pool` 改读 SQLite
- ✅ `app.py` H5 端改读 SQLite
- ✅ `photo_manager._handle_delete_dish` 改用 SQLite 软删

**V6 漏掉的**：
- ❌ `photo_manager.get_all_dishes` 仍然读 `dish_pool.json`
- ❌ `photo_manager.get_categories` 仍然读 `dish_pool.json`
- ❌ `photo_manager._handle_add_dish` 仍然写 `dish_pool.json`
- ❌ `photo_manager._handle_edit_dish` 仍然写 `dish_pool.json`
- ❌ `photo_manager._handle_save_categories` 仍然写 `dish_pool.json`
- ❌ `photo_manager._handle_save_custom_tags` 仍然写 `dish_pool.json`

**结果**：菜品管理器的**所有 CRUD 操作都不影响 SQLite**，只有删除时同步到 SQLite。这就导致：
- 用户在菜品管理器加的菜 → 只在 `dish_pool.json`，H5 看不到
- 用户在菜品管理器改的菜 → 只在 `dish_pool.json`，H5 看不到
- 数据源分裂是必然的

---

## 二、V7 修复方案

### 2.1 核心原则

> **以菜品管理器为唯一真相源**（用户原话）
> - 菜品管理器的真相源 = SQLite（重构后）
> - H5 前端 = 菜品管理器 = 同一个真相
> - `dish_pool.json` 废弃为只读快照

### 2.2 Step 1: 从 `dish_pool.json` 重建 SQLite

新建 `rebuild_dishes_from_pool.py`：
- 备份当前 dishes 表到 `dishes_legacy` (V7 临时表)
- 删掉 4 道幽灵菜 (dish_0004/0009/0022/0143) 及其 dish_ingredients 引用
- 从 dish_pool.json 重写所有 203 道菜到 SQLite (含缺失的 dish_0209-0212)
- 验证：dishes 表与 dish_pool.json 完全一致

### 2.3 Step 2: photo_manager.py 全部切到 SQLite

| 函数 | 修复前 | 修复后 |
|------|--------|--------|
| `get_all_dishes()` | 读 `dish_pool.json` | 读 SQLite `dishes` (is_active=1) + LEFT JOIN categories |
| `get_categories()` | 读 `dish_pool.json` | 读 SQLite `categories` |
| `_handle_add_dish()` | 写 `dish_pool.json` | SQLite INSERT (自动生成 dish_xxxx ID) |
| `_handle_edit_dish()` | 写 `dish_pool.json` | SQLite UPDATE (含 log_event) |
| `_handle_delete_dish()` | SQLite 软删 + 同步 JSON | **仅** SQLite 软删（不再写 JSON） |
| `_handle_save_categories()` | 写 `dish_pool.json` | SQLite categories 表全量替换 |
| `_handle_save_custom_tags()` | 写 `dish_pool.json` | SQLite custom_tags_def 表全量替换 |

### 2.4 Step 3: 兼容历史"组合菜名"数据

V3 时代 20 天推送（menu_id 1-20）每条 menu_items 的 dish_id 是"组合菜名"（如"银鳕鱼淮山红萝卜松茸粥 ＋ 黑鱼子酱配嫩豆腐 ＋ 香港红薯 ＋ 清炒豆苗"），不是 dish_xxxx。

`menu_service.get_menu_with_dishes` 增强：
- 检测 `dish_id` 不是 `dish_xxxx` 开头 → 视为 `custom_name`（历史组合菜单）
- 把它填到 `name_cn` 字段让前端正常显示
- 添加 `is_historical_combo: True` 标记

这样 History 页面**完整保留**所有 72 条 V3 历史菜单，**不会因为 JOIN 不到 dish 而消失**。

---

## 三、验证结果

### 3.1 端到端一致性测试（5/5 通过）

| Test | 验证内容 | 结果 |
|------|----------|------|
| 1a | 菜品管理器(8080) vs H5前端(8090) 菜品数 203=203 | ✅ |
| 1b | "薄切牛肉片"：8080=1条，8090=1条 (都是 dish_0010) | ✅ |
| 2 | 菜品管理器加菜 (dish_0213) → H5 立即看到 | ✅ |
| 3 | 菜品管理器删菜 → H5 立即看不到 | ✅ |
| 4 | 数据库软删 (is_active=0, deleted_at=timestamp) | ✅ |
| 5 | 菜品管理器编辑 → H5 看到新菜名 | ✅ |

### 3.2 V6 黑盒测试 11/11 全部回归通过

| Test | V6 内容 | V7 结果 |
|------|---------|---------|
| A | 搜索已存在食材不删除 | ✅ PASS |
| B | Same as Last Update | ✅ PASS |
| C | Clear All 已移除 | ✅ PASS |
| D | AI None 清理 | ✅ PASS |
| E | Draft 不 Push | ✅ PASS |
| F | Almost Available 定义 | ✅ PASS |
| G | 单食材菜不进入 Almost | ✅ PASS |
| H | Pantry → Tomorrow 同步 | ✅ PASS |
| I | Pantry → Dishes 同步 | ✅ PASS |
| J | 菜品管理器删除 → H5 + AI 同步 | ✅ PASS |
| K | History 保留已删除菜 | ✅ PASS |

**总成绩：16/16 全部 PASS**

---

## 四、文件变更

### 新建
- `rebuild_dishes_from_pool.py` (133 行) — 从 dish_pool.json 重建 SQLite
- `backups/family_menu.db.pre_v7_20260731_110629` (290KB) — 数据库备份
- `backups/dish_pool.json.pre_v7_20260731_110629` (154KB) — JSON 备份
- `dishes_legacy` 表 (V7 临时备份) — 旧 dishes 表

### 修改
- `photo_manager.py` (大改) — 6 个函数从 dish_pool.json 切到 SQLite
- `menu_service.py` (改) — get_menu_with_dishes 兼容历史组合菜名
- `family_menu.db` (重建) — 删 4 道幽灵菜，加 4 道缺失菜
- `dish_pool.json` (未变) — 现在仅作只读快照

---

## 五、长期数据健康度

V7 之后：
- ✅ **菜品管理器 (8080) = H5 前端 (8090) = AI 引擎 (menu_service) = SQLite 真相**
- ✅ **不再有"幽灵菜"**：所有展示路径都从 SQLite `is_active=1` 读
- ✅ **增删改一致性**：用户在菜品管理器操作 → SQLite → H5 + AI 立即看到
- ✅ **可恢复**：dish_pool.json + dishes_legacy 双重备份，必要时可回滚

未来如果菜品管理器再次分裂（比如加菜只在 8080 生效），都会从 SQLite 这个唯一真相源被发现。
