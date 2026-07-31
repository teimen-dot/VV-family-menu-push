# V6 测试报告 — Single Source of Truth + Nanny UX 修复

**测试日期**: 2026-07-31  
**测试版本**: V6  
**测试环境**: localhost:8090 (H5) + localhost:8080 (菜品管理器)

---

## 黑盒验收测试结果（11/11 PASS）

| 测试 | 描述 | 结果 | 详情 |
|------|------|------|------|
| A | 搜索已存在食材不会删除 | ✅ PASS | Before: 16 items → After: 16 items, '16谷米' 仍在库存 |
| B | 和上次一样 Same as Last Update | ✅ PASS | 库存 16→16 项不变, confirmed_at 已记录 |
| C | Clear All 主页面已移除 | ✅ PASS | `clearAll()` 不再出现在 Pantry 页面 |
| D | AI None 清理 | ✅ PASS | Null dish_id count: 0, Tomorrow 无 None 菜品 |
| E | Draft 不 Push | ✅ PASS | Push 被拒绝: "菜单状态为 draft, 只有 VV confirmed 才能推送" |
| F | Almost Available = required>=2 && missing==1 | ✅ PASS | 家常水煮牛肉: required=2, missing=1, status=almost_available |
| G | 单食材菜不进入 Almost Available | ✅ PASS | 捞汁秋葵: required=1, missing=1, status=missing (非 almost) |
| H | Pantry → Tomorrow 同步 | ✅ PASS | 添加车厘子 → Tomorrow 缺货消失 (synced!) |
| I | Pantry → Dishes 同步 | ✅ PASS | 删除沙拉菜 → status=missing; 恢复 → status=available |
| J | 菜品管理器删除 → H5 + AI 同步 | ✅ PASS | H5 dishes: 203 = Active in DB: 203 |
| K | History 保留已删除菜历史 | ✅ PASS | History entries: 3 (LEFT JOIN 保留历史) |

---

## 本轮修复清单

### P1: Pantry UX 重构
- [x] 搜索已存在食材显示"已在库存 ✓"，不 toggle 删除
- [x] 删除只用"用完 Used Up"按钮 + 确认对话框
- [x] 移除主页面 Clear All 大按钮
- [x] 新增"和上次一样 Same as Last Update"按钮（只记录确认时间，不改库存）
- [x] 添加/删除/状态变化即时自动保存（增量操作，不覆盖整份库存）
- [x] Common 食材稳定显示（is_common 字段，独立于当前库存）
- [x] Common 点击：不在库存→加入；已在库存→显示 ✓ 不执行删除
- [x] 状态简化为 3 选 1（可用 Available / 优先 Priority / 快过期 Expiring）

### P2: 消灭 AI Suggestion None
- [x] `_store_menu_items` 跳过 dish_id 为 None/空的候选
- [x] `ai_fill_menu` 跳过 None dish_id
- [x] `get_menu_with_dishes` 过滤 null/orphan menu items + 记录 error log
- [x] 已有 menu_items 无 null 数据（0 条）
- [x] menu_items 表新增 custom_name + source 列（DB 约束支持）

### P3: Availability 重定义
- [x] Available: required > 0 && missing == 0
- [x] Almost Available: required >= 2 && missing == 1
- [x] Missing: missing >= 2 或 (required == 1 && missing == 1)
- [x] Incomplete: required == 0
- [x] Almost Available 卡片显示具体缺什么（缺：红椒 Missing: Red Pepper）
- [x] Pantry → Dishes 实时同步（inventory_version + 缓存失效）
- [x] Pantry → Tomorrow 实时同步

### P4: Single Source of Truth
- [x] dishes 表新增 is_active + deleted_at 列（Soft Delete）
- [x] 菜品管理器删除 → Soft Delete（is_active=0），不再物理删除
- [x] H5 get_all_dishes() → WHERE is_active = 1
- [x] AI _load_pool() → 从 SQLite 读取（不再读 dish_pool.json）
- [x] Confirmed 菜单中已删除菜显示"已下架 Archived"标记
- [x] History 仍可通过 LEFT JOIN 显示已删除菜
- [x] 所有新选择入口（Dishes/搜索/Add Dish/AI/Available Now）只读 active dishes

### P5: 推送规则加固
- [x] 未 VV Confirm 绝对不 Push（push_menu 检查 status == "confirmed"）
- [x] 无自动确认/自动推送兜底逻辑
- [x] 19:00/20:00 提醒保留但不自动确认
- [x] GitHub Actions push_menu.py 检查 confirmed 字段

---

## 修改文件

| 文件 | 修改内容 |
|------|----------|
| `db.py` | dishes 表新增 is_active + deleted_at；menu_items 新增 custom_name + source |
| `inventory.py` | Availability 4态重定义；新增 add_ingredient_to_pantry / remove_ingredient_from_pantry / update_ingredient_status / confirm_pantry_unchanged / is_ingredient_in_pantry |
| `menu_service.py` | _load_pool() 改读 SQLite；_store_menu_items 跳过 None；get_menu_with_dishes 过滤 null + 标记已下架；ai_fill_menu 跳过 None |
| `app.py` | Pantry 页面全面重构（搜索不删除/无Clear All/Same as Last/即时保存/Common稳定）；get_all_dishes 过滤 is_active；Dishes 卡片显示具体缺失食材；新增 /api/pantry/add + /api/pantry/same-as-last；/pantry/submit 重定向 |
| `photo_manager.py` | 删除菜品改为 Soft Delete（is_active=0 + deleted_at） |

---

## 架构改进

```
PantryService (inventory.py)
    ↓
Current Pantry (current_pantry 表, 增量维护)

InventoryService (inventory.py)
    ↓
Availability / Shortage (4态判定, inventory_version 缓存)

DishService (SQLite dishes 表, is_active 过滤)
    ↓
Active Dishes (唯一真相源)

MenuService (menu_service.py)
    ↓
Tomorrow / Confirm / Draft (过滤 null, 标记 archived)

AI Planner (rule_engine.py + menu_service.py)
    ↓
只调用以上 Service, 只读 active dishes
```
