# 家庭菜单管家 — 项目进度总览

> 本文档汇总所有对话中的需求规格、技术决策、当前进度和待办事项。
> 新对话可直接读取此文件获取完整上下文。

---

## 一、项目概述

- **目标**：AI 生成 20 天中英双语家庭菜谱，通过 PushPlus 每天 10:30 推送到家庭微信群
- **推送方案**：GitHub Actions + PushPlus Route B（群推），电脑关机也能执行
- **项目路径**：`/Users/vv/WorkBuddy/Claw`
- **GitHub 仓库**：`https://github.com/teimen-dot/VV-family-menu-push`
- **起步日期**：2026-07-28，每天 10:30 推送，持续 20 天
- **PushPlus 群组编码**：`home-menu`

---

## 二、家庭成员与饮食约束

- 深圳一家四口：父母、22 岁兄长、18 岁弟弟
- 22 岁兄长海鲜/甲壳类过敏，且不在家吃
- 全家常温热食，忌内脏
- 忌冷食（酸奶等生冷）
- 中文保姆需要能跟做新菜

---

## 三、文件结构

| 文件 | 说明 |
|------|------|
| `dish_pool.json` | 菜品数据库 v2.0（115 道普通菜 + 27 项轮换池） |
| `photo_manager.py` | 本地菜品管理器 v2.0（端口 8080，纯 Python stdlib） |
| `push_menu.py` | 推送脚本（PushPlus + GitHub Actions） |
| `photo_manifest.json` | 照片映射（中文菜名 → 文件名） |
| `menu_data.json` | 20 天推送计划 |
| `config.json` | 推送配置 |
| `seasonal_tips.json` | 时令建议（12 个月，中英双语） |
| `migrate_v2.py` | v1→v2 数据迁移脚本 |
| `sync.sh` | Git 一键同步脚本 |
| `photos/` | 菜品照片（正方形 400×400 JPEG） |
| `.github/workflows/daily-push.yml` | GitHub Actions 定时任务 |
| `dish_pool.json.bak.v1` | v1 备份（迁移前） |
| `photo_manifest.json.bak.v1` | 照片映射 v1 备份 |

---

## 四、菜品数据结构 v2.0

### 数据结构

```json
{
  "meta": { "version": "2.0", "created": "...", "total_dishes": 142 },
  "categories": [
    { "id": "protein_main", "label_cn": "蛋白质 / 主菜", "label_en": "Protein / Main", "order": 1, "active": true }
  ],
  "dishes": [
    {
      "id": "dish_0001",
      "name_cn": "黑鱼子酱配嫩豆腐",
      "name_en": "Black Caviar with Silken Tofu",
      "category_id": "protein_main",
      "meal_tags": ["breakfast", "lunch", "dinner"],
      "banquet": true,
      "protein_types": ["fish", "tofu"],
      "vegetables": [],
      "vegetable_count": 0,
      "carb_type": null,
      "meal_components": [],
      "taste": "light",
      "cooking_methods": ["steam"],
      "can_serve_warm": false,
      "custom_tags": ["高端"],
      "needs_review": false,
      "old_category": "protein_breakfast"
    }
  ],
  "custom_tags_def": [
    { "label": "老板喜欢" }, { "label": "快速菜" }, { "label": "Toby喜欢" }
  ],
  "rotation_pools": {
    "porridge_base": { "description": "粥底轮换池", "items": [...] },
    "egg_styles": { "description": "鸡蛋做法轮换池", "items": [...] }
  }
}
```

### 8 个一级分类

| ID | 中文名 | 英文名 |
|----|--------|--------|
| protein_main | 蛋白质 / 主菜 | Protein / Main |
| egg_tofu | 蛋类 / 豆制品 | Egg / Tofu |
| vegetable_mushroom | 蔬菜 / 菌菇 | Vegetable / Mushroom |
| soup | 汤 / 羹 | Soup |
| staple_carb | 主食 / 碳水 | Staple / Carb |
| cold_dish | 冷菜 / 凉拌 | Cold Dish |
| one_pot_meal | 一餐型料理 | One-Pot Meal |
| fruit_snack | 水果 / 加餐 / 下午茶 | Fruit / Snack |

### 结构化字段说明

- `meal_tags`：breakfast / lunch / dinner（多选，决定一道菜适合什么时候吃，不再用旧分类判断）
- `banquet`：true/false（家宴推荐，不是独立分类）
- `protein_types`：fish / shrimp / other_seafood / beef / pork / chicken / egg / tofu / other / none（多选）
- `vegetables`：字符串数组，如 `["芹菜"]`；`vegetable_count` 自动计算
- `carb_type`：rice / porridge / noodle / dim_sum / coarse_grain / tuber / other（仅主食/碳水分类适用）
- `meal_components`：protein / vegetable / carb（仅一餐型料理适用，标记这道菜已包含什么）
- `taste`：light / normal / rich / spicy（单选）
- `cooking_methods`：steam / boil / stir_fry / braise / simmer / pan_fry / roast / cold_mix / other（多选）
- `can_serve_warm`：true/false（仅冷菜/凉拌适用）
- `custom_tags`：用户自定义标签数组
- `needs_review`：true/false（迁移时无法自动判断分类的菜标记为待审核）

---

## 五、菜品管理器 v2.0 功能

### 编辑窗口（多层折叠）

1. **基础信息**：中文名、英文名、分类（下拉选择）
2. **适合餐别**：早餐 / 午餐 / 晚餐（多选）
3. **特殊标签**：家宴推荐
4. **自定义标签**：Tag Input（回车添加）
5. **更多信息 / AI 配餐信息**（默认折叠）：
   - 主要蛋白质（多选）
   - 包含蔬菜（Tag Input，vegetable_count 自动计算）
   - 主食类型（选"主食/碳水"时显示）
   - 一餐型料理组成（选"一餐型料理"时显示）
   - 口味（单选）
   - 烹饪方式（多选）
   - 可改温热版（选"冷菜/凉拌"时显示）

### 菜品卡片

显示：图片 + 中文名 + 英文名 + [分类标签] + 餐别标签 + 家宴标签 + 待审核标签

### 双层筛选

- 第一层：全部 / 早餐 / 午餐 / 晚餐 / 家宴
- 第二层：8 个分类 + 2 个轮换池
- 两个条件可组合

### 搜索

支持：中文名、英文名、分类、主要食材、蔬菜、自定义标签

### 分类管理

- 修改分类中英文名称
- 调整排序（↑↓）
- 启用 / 停用
- 删除保护：分类下有菜品时不可删除，提示"该分类下还有 XX 道菜，请先移动菜品后再删除"

### 标签管理

- **系统标签**（早餐/午餐/晚餐/家宴推荐）：只读，不可删除
- **自定义标签**：新增、修改、删除

---

## 六、三餐规则

### 早餐
- 蛋白质 1-2 份 + 蔬菜 2-3 种 + 主食/碳水 1-2 种（优先至少一种粗粮或薯类）
- 可以吃鲜牛肉炒芹菜、清蒸鱼、白灼虾、鸡蛋、豆腐等符合 breakfast 标签的菜
- 不限定早餐只能吃传统早餐食品

### 午餐
- 蛋白质/主菜 ×1 + 蔬菜 ×1 + 主食/碳水 ×1 + 汤/羹 0-1 + 蛋类/豆制品 0-1
- 简单、均衡，不为数量强行增加菜

### 晚餐
- 蛋白质/主菜 1-2 + 蛋类/豆制品 0-1 + 蔬菜约 2 + 汤/羹 0-1 + 主食/碳水 1
- 家人共同用餐，比午餐丰富

### 家宴
- 优先 `banquet=true` 的菜
- 综合考虑：人数、菜式丰富度、蛋白质种类、蔬菜、汤、主食、冷菜、烹饪方式、口味搭配
- 避免：全部牛肉、全部炒菜、全部重口、全部清蒸

---

## 七、已完成进度

### 2026-07-28（第一天）
- [x] 创建 dish_pool.json v1 菜谱数据库（112 道菜 + 10 款粥底 + 18 款鸡蛋做法）
- [x] 创建 menu_data.json 推送计划（20 天周期）
- [x] 创建 config.json 推送配置
- [x] 创建 seasonal_tips.json 时令建议
- [x] 创建 push_menu.py 推送脚本
- [x] 创建 .github/workflows/daily-push.yml GitHub Actions 定时任务
- [x] 上传到 GitHub 仓库
- [x] PushPlus 实名认证通过，GitHub Actions 推送成功
- [x] 创建 photo_manager.py 本地照片管理器
- [x] menu_data.json 去重（修复 11 处同天菜品重复）
- [x] seasonal_tips.json 清理饮料类建议
- [x] push_menu.py 排版优化 + 照片嵌入 + HTTPS + 905 提示
- [x] photo_manager.py 增加菜品编辑/添加/删除功能
- [x] Git 同步：sync.sh 脚本 + .gitignore

### 2026-07-29（第二天）
- [x] 启动 photo_manager.py 服务器（用户反馈无法访问 8080）
- [x] 照片裁剪改为 400×400 正方形
- [x] push_menu.py 一餐多图并排显示在文字上方
- [x] **数据结构 v2.0 迁移**：
  - dish_pool.json 从 v1 升级到 v2（扁平 dishes 数组 + categories 列表）
  - 8 个新分类 + 2 个轮换池
  - 115 道普通菜 + 27 项轮换池 = 142 项全部保留
  - 51 张照片全部保留
  - 新增结构化字段（meal_tags, banquet, protein_types, vegetables, carb_type 等）
- [x] **photo_manager.py v2.0 完全重写**：
  - 多层折叠编辑窗口
  - 条件显示（主食类型/一餐型组成/可改温热版）
  - 卡片标签显示
  - 双层筛选
  - 增强搜索
  - 分类管理 + 标签管理
- [x] 11 项数据完整性验证全部通过

---

## 八、待完成（按优先级）

### 第一优先级 — 数据填充
- [ ] 人工审核 `needs_review=true` 的菜品，修正分类
- [ ] 逐道菜补充 AI 配餐信息（蛋白质类型、蔬菜、口味、烹饪方式）
- [ ] 上传更多菜品照片（目前 51/142）

### 第二优先级 — AI 自动配餐
- [ ] 基于 v2.0 结构化字段编写 AI 配餐逻辑
- [ ] AI 读取 protein_types/vegetables/taste/cooking_methods 等字段，不再只猜菜名
- [ ] 实现三餐规则 + 家宴规则
- [ ] 避免：同餐口味重复、烹饪方式重复、蛋白质种类单一

### 第三优先级 — 推送优化
- [ ] 测试照片在 PushPlus 中的显示效果
- [ ] 验证正方形照片在微信群中的展示
- [ ] 一餐多图并排显示效果验证

### 第四优先级 — 其他
- [ ] GitHub Actions 手动触发验证新排版
- [ ] 20 天菜单基于新数据结构重新生成
- [ ] 考虑用餐人数动态调整菜量

---

## 九、技术要点

- **photo_manager.py**：纯 Python stdlib（http.server + json + os），零依赖
- **push_menu.py**：纯 Python stdlib（urllib + json），零依赖
- **照片裁剪**：浏览器 Canvas 客户端裁剪，400×400 JPEG 85%
- **推送时间**：UTC 02:30 = 北京时间 10:30
- **Git 代理**：http://127.0.0.1:50930, HTTP/1.1

---

## 十、v1 → v2 分类迁移对照

| 旧分类 | 新分类 | 餐别标签 |
|--------|--------|----------|
| 早餐蛋白质 (protein_breakfast) | 蛋白质 / 主菜 (protein_main) | breakfast |
| 午晚餐主荤 (protein_lunch_dinner) | 蛋白质 / 主菜 (protein_main) | lunch, dinner |
| 蔬菜 / 蛋类 (vegetable) | 蔬菜 / 菌菇 或 蛋类 / 豆制品 | 按菜名判断，无法判断的 needs_review=true |
| 汤类 (soup) | 汤 / 羹 (soup) | lunch, dinner |
| 主食 / 碳水 (staple) | 主食 / 碳水 (staple_carb) | 按原有 tags |
| 水果 / 加餐 (fruit_snack) | 水果 / 加餐 / 下午茶 (fruit_snack) | 保持 |
| 冷菜类 (cold_dish_reform) | 冷菜 / 凉拌 (cold_dish) | lunch, dinner |
| 粥底轮换池 | 不迁入普通分类，保留为轮换组件 | — |
| 鸡蛋做法轮换池 | 不迁入普通分类，保留为轮换组件 | — |
