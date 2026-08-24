# Diff Against `NEW_VERSION_BACKEND_NEEDS.md`

基线：生产实际运行提交 `e9b64ec` 与正式 SQLite schema/data。  
判定：✅ 可保留；⚠️ 可通过兼容改造解决；❌ 结构冲突，需要新模型/重写该子系统，不表示必须丢弃整个后端。

## 总裁决

不建议整套后端和正式数据推倒重做。

- 数据资产可保留：`dishes`、`ingredients`、`dish_ingredients`、图片字段、`current_pantry`、历史菜单、事件和 Push 日志均有迁移价值。
- 库存 avail 推导核心可保留：现有实现已经由 pantry 动态计算，不是人工维护 `dishes.avail`。
- 必须新增/替换：每餐级 meal plan、双厨房菜单唯一键、饮品、day notes、宵夜、每餐独立确认、四日规划/去重/降级编排。
- `app.py` 的 Web/API/UI 边界建议重写或拆出，不应继续把新版功能堆入 4,483 行单文件。
- 推送 transport 与 revision/idempotency 日志可保留；“四日菜单生成 + 选人 + 每餐确认后如何推”的推送编排应重写。

建议路线是“保留数据层和可复用服务，建立 V2 meal-plan 模型与 API，重构规则编排，重写前端/路由边界”，不是“清空数据库重做”。

## A. 数据模型要求

### A1. dishes

| 新版要求 | 现状 | 判定 | 兼容改造与影响 |
|---|---|---|---|
| `zh / en` | `dishes.name_cn / name_en` | ✅ | API adapter 改字段名即可。 |
| `cat` 新枚举 | 现有 8 类：`protein_main/egg_tofu/vegetable_mushroom/soup/staple_carb/cold_dish/one_pot_meal/fruit_snack` | ❌ | 新文档写“8 分类”但实际列出 9 个值（meat/sea/veg/soup/staple/egg/cold/onepot/fruit）。现有 protein 合并 meat+sea，egg_tofu 也合并。需建立明确映射并人工/规则拆分数据；影响 Admin、搜索、推荐、slot filter、导入导出。无需丢弃 dishes。 |
| `emoji` | 无 | ⚠️ | 加 nullable/default 字段或 API 按新分类返回默认 emoji；仅 Admin/API/UI。 |
| `avail=ok/low/miss` 且由 pantry 推导 | `inventory.check_dish_availability` 动态返回 `available/almost_available/missing/incomplete`，不写入 dishes | ✅/⚠️ | 核心正确。API 映射 `available→ok`、`almost_available→low`、`missing→miss`；`incomplete` 建议保留独立 `data_complete=false`，不要伪装成库存缺货。 |
| drink + 子类 | 无 drink/drink subtype | ❌ | 建议新建 `drinks` 与 `meal_plan_drinks`，避免饮品混入菜品统计；若放 dishes 需新增 kind/subtype 且所有规则显式排除，风险更高。 |
| `manualOnly` | 仅 `manual_only_for_breakfast` | ⚠️ | 加通用 `manual_only`；保留旧字段兼容并一次性回填。影响 Admin、池过滤、推荐和四日生成。 |
| 家宴 | `banquet` | ✅ | 字段含义可映射。 |
| `staple_type` | `carb_type + breakfast_staple_type` | ⚠️ | 建议 API adapter 统一语义；若新版只接受一个字段需制定枚举映射。 |
| `soup_type` | `quick_soup`、`slow_soup` 两个布尔 | ⚠️ | 可映射为 `quick/slow/none`；清理同时为真和未分类数据。 |
| 必需食材 | `dish_ingredients.required` | ✅ | 直接复用；213 active dishes 中 7 道无 required ingredients，迁移前需补录或保留 incomplete。 |
| 包含蔬菜 | `dishes.vegetables` JSON | ✅ | 可复用，建议 V2 API 输出数组。 |
| 照片 | `dishes.image` + `/opt/family-menu/photos` | ✅ | 直接复用。 |

### A2. meal_plans（每餐级）

正式库没有 `meal_plans`。现有是：

```text
menus                 每天一行，status 整单级，date 全局 UNIQUE
menu_items            每道菜一行，带 meal_type/source
menu_meal_settings    每餐 diners/note/is_skipped
```

| 新版要求 | 现状 | 判定 | 兼容改造与影响 |
|---|---|---|---|
| 每餐 `status=pending/confirmed/cancelled` | `menus.status=draft/confirmed/pushed` 整单级；meal settings 只有 `is_skipped` | ❌ | 新建 `meal_plans` 最清晰；也可扩 `menu_meal_settings`，但会继续依赖整单 status。影响确认、编辑回待确认、revision、Push、历史和 UI。 |
| `mtype` 5 种 | 4 种，无 late-night | ⚠️ | 新枚举加 `late_night`；更新所有固定 tuple、renderer、slot、Push formatter、schema 校验。 |
| `drinks[]` | 无 | ❌ | 新建 `drinks`、`meal_plan_drinks`；规则和统计只 join meal_plan_dishes。 |
| 每餐 `note` | `menu_meal_settings.note` | ✅ | 可迁移/复用。 |
| 每餐 `source` | 只有 `menu_items.source`，无 meal source | ⚠️ | 给新 `meal_plans` 加 source；现有 item source 继续保留。 |

推荐的兼容模型：

```text
meal_plans(id, location, date, mtype, status, note, source, diners_json, ...)
UNIQUE(location, date, mtype)
meal_plan_dishes(meal_plan_id, dish_id, sort_order, source, locked, ...)
meal_plan_drinks(meal_plan_id, drink_id, sort_order, ...)
```

先双写/adapter，再迁移旧 `menus + menu_items + menu_meal_settings`，可以避免一次性破坏正式库。

### A3. day_notes

现状没有 day-level 用户备注。`menus.notes_zh/en` 被规则 review warning 占用，`menus.meal_notes` 也不是独立 day note，不能复用为同一语义。

判定：⚠️ 新增 `day_notes(location,date,note,updated_at)`，`UNIQUE(location,date)`；影响 V2 API、Worker 页面与备份，无需重写数据库。

### A4. pantry

| 要求 | 现状 | 判定 | 说明 |
|---|---|---|---|
| 充足/缺货两态 | `status=available/priority_use/expiring` + `is_active`；正式数据只出现 available/expiring | ⚠️ | `is_active=1/0` 已接近有货/无货。若仍需“将过期”，应改成正交 attention/expiry 字段，不再当库存状态。 |
| pantry 变化触发 avail 重算 | inventory_version++ + location cache invalidate；下次请求动态重算 | ✅ | 无需回写 dishes.avail。 |
| low/miss 与采购聚合 | 缺 1–2 个 required→almost_available，缺更多→missing；purchase_requests 按食材去重 | ✅/⚠️ | 核心可用；阈值需与新版“差少量”定义确认，API 改名即可。 |

## B. 业务规则要求

| # | 新版规则 | 现状 | 判定/改造 |
|---:|---|---|---|
| 1 | 默认只早/午/晚；不自动推下午茶/宵夜 | 生成后固定随机加 1–2 个下午茶；Push formatter 已排除下午茶 | ❌ | 删除生成器自动下午茶行为；meal plan 默认只建三餐。Push 排除逻辑可复用并扩展宵夜。 |
| 2 | 加一餐：今天全 5 种；其他天只下午茶/宵夜与恢复取消餐 | 当前总是渲染四餐，可 skip/restore，无 late-night、无日期规则 | ❌ | 新 meal-plan create/cancel/restore API 和日期授权规则。 |
| 3 | 每餐独立确认；编辑后该餐回 pending | 当前整单确认；首次编辑 confirmed tomorrow 会把整单 revert 为 draft | ❌ | 每餐 status + 每餐 revision；确认/编辑/进度/Push 全链路调整。 |
| 4 | 双厨房 pantry + meal_plans 隔离 | pantry 隔离；menus `date UNIQUE` 且读取多只按 date；生产 20 menus 全是深圳 | ❌ | 新表直接 `UNIQUE(location,date,mtype)`。若保留 menus，SQLite 需重建表改为 `UNIQUE(location,date)`，风险更高。 |
| 5 | 饮品不计道数/校验/自动推送 | 无饮品概念 | ❌ | 独立 drinks 关联表天然隔离统计；不要复用 menu_items。 |

## C. 推送/规划引擎

### 输入输出

新版需要“日期 + 人数 + 库存 → 4 天 × 各餐 dishId + missingIngredients”。当前 `generate_and_store_menu` 只生成一个 date 的一天菜单，且直接落库；没有纯函数式四日输出。

判定：❌ 重写四日 planner/orchestrator，但复用 `NutritionAnalyzer`、slot filter、availability 和部分评分。

### 去重

- 当前同一天跨餐通过 `day_history` 硬排除重复。
- 过去 3/7 日只对 status=`pushed` 的菜单取历史，并在 `ScoringEngine` 中扣分，不是硬排除。
- `get_dish_recommendations` 另有一套“近 3 日没吃过 +20”逻辑，也不是硬排除。
- 没有“一次生成 4 天时，前 3 天 + 今天锁定”的统一状态。

判定：❌ 四日 planner 必须集中实现 date-scoped used set；现有同日 `day_history` 代码可作为局部参考。

### 入池与降级

- 全量生成和 AI fill 已只选 `status=available`，这是可保留信号。
- 只排除 `manual_only_for_breakfast`，没有通用 manualOnly；无 drink 类型。
- 池不足时当前停止并返回 no candidate/unmet slot，不会“允许重复 + 补录提示”。

判定：⚠️ 保留 availability filter，重构 pool policy 与 degrade result。

### 餐次结构

| 规则 | 现状 | 判定 |
|---|---|---|
| 早餐粥 1 | 有 porridge slot | ✅ |
| 包点/搭配主食 1 | companion_staple slot，dim_sum 有兼容 | ✅ |
| 蛋 1 | egg slot | ✅ |
| 豆腐 1 | tofu slot | ✅ |
| 粗粮 ≥1 | coarse_grain slot | ✅ |
| 蛋白质 1–2 | 早餐 slot analyzer 没有独立 protein_main | ❌ |
| 蔬菜按人数 | 1–2 人目标 1，3+ 目标 2 | ⚠️：需确认新版精确人数表 |
| 午餐 quick_soup | 有 | ✅ |
| 晚餐 slow_soup | 有 | ✅ |
| onepot 不乱补 | complete one-pot lunch 会覆盖蛋白/蔬菜/主食，只补 quick soup | ✅/⚠️：只覆盖 lunch 和“完整 onepot” |
| 智能补充单餐缺失分类 | `ai_fill_menu(..., meal_type=)` 已支持 | ✅：但需迁移到新 meal_plan ID 与统一四日状态 |

### 规则集中度

正面：slot target、candidate filter、nutrition analyzer 大部分集中在 `rule_engine.py`。  
风险：推荐评分在 `app.py:get_dish_recommendations` 又实现一套；智能换菜也在 `app.py`；下午茶生成、历史查询、库存过滤分散在 `menu_service.py/rule_engine.py/app.py`。

裁决：不是全部重写信号，但应把 pool policy、历史去重、降级和评分统一到一个 planner service/config。

## D. 基础设施

| 项 | 现状 | 判定/改造 |
|---|---|---|
| 中英翻译 | `ingredient_service.py` 使用本地 `KNOWN_TRANSLATIONS + INGREDIENT_EN_NAMES`，未知项标记 `translation_pending`；无 LLM | ⚠️：词典兜底可保留，新增后端 LLM adapter、超时/失败兜底和审计字段。 |
| 拍照识别食材 | 未发现 Qwen-VL/视觉模型/API | ⚠️：新增独立服务与 env key，不是重做数据层理由。 |
| 鉴权 | app 用户名/密码 + 30 天签名 session；runtime 仅 owner/worker；无匿名 + 家庭口令 + employer/family/nanny 三角色 | ❌/⚠️：session 安全框架可保留，identity/role 模型和权限矩阵需扩展。不可把 `diners.role` 当登录角色直接复用。 |
| 数据迁移 | 已完成正式 SQLite，223 dishes、157 ingredients；图片/食材关系在库 | ✅：这是保留后端数据层的强信号。迁移新版前先做 ID、分类、required ingredients、双厨房条数对账。 |

## 五块逻辑复用评估

| 模块 | 自评 | 理由 |
|---|---|---|
| 菜单生成 | 建议重构 | RuleEngine/slot 核心可复用，但要变为 4 日纯 planner、默认三餐、硬去重、降级、早餐蛋白质和新 meal-plan persistence。 |
| 推荐 | 建议重构 | avail/评分思路可复用，但当前两套评分分散，分类、manualOnly、drink、四日去重未统一。 |
| 换菜 | 建议重构 | 智能换菜有 avail=available 和循环池，但仍在 app.py，依赖旧分类/旧 menu_id，且未共享四日 used set。 |
| 库存 | 可直接复用（加 adapter） | current pantry、required ingredient、动态 avail、location version/cache、采购聚合已完整；主要是两态 API 和字段命名调整。 |
| 推送 | 应该重写编排；保留 transport | PushPlusClient、revision、幂等日志和错误脱敏可保留；现有只推一个整单 confirmed 菜单，缺每餐 status、4 日输出、选人/角色策略和新餐次模型。 |

## `app.py` 耦合评估

主要耦合点：

1. HTTP route、session 权限、SQL 查询、业务判断、HTML、CSS、JavaScript 同文件。
2. 推荐/智能替换逻辑留在 app，而生成/AI fill 在 service/rule engine，策略重复。
3. renderer 会在 GET 时触发 `ensure_tomorrow_menu` 写业务数据，读页面与生成副作用绑定。
4. 固定餐次 tuple 和 location 默认散落多处。
5. 整单 status、Push revision、编辑回退紧耦合。
6. production/preview UI 和 preview-only API 共存于同一 handler。

如果继续在其上直接增加“每餐独立确认、饮品位、day notes、宵夜、四日菜单”，会造成更多 `if/elif`、重复枚举和状态分支，测试面指数上升。

建议边界：

```text
HTTP/API adapter
  → Auth/role policy
  → MealPlanService（CRUD/confirm/cancel/note/drink）
  → PlannerService（4 日/去重/降级）
  → AvailabilityService（复用 inventory.py 核心）
  → PushOrchestrator（每餐/目标/策略）
  → SQLite repositories
```

前端通过 JSON API 访问，不再在 `app.py` 内拼接新版 HTML/JS。

## E. 裁决打分卡结论

| 维度 | 生产事实 | 信号 |
|---|---|---|
| 每餐级 status | 不存在，整单 menus.status | 强重构/局部重写 |
| avail 推导 | pantry 动态推导，带版本/cache | 强保留 |
| 推送/生成引擎 | 规则部分集中，但只有一天、历史软扣分、无池不足重复降级 | 重构 planner；重写 push orchestration |
| 菜品分类 | 分类数量接近但枚举/拆分不一致 | 数据迁移 + adapter，不必丢库 |
| 双厨房 | pantry 隔离，menus 不隔离 | 新 meal-plan 表/结构迁移 |
| 前端 | 生产 Python 内嵌 UI 与新版目标不同 | 前端/API 边界重写，不影响数据保留裁决 |

最终裁决：**保留正式 SQLite 数据与库存/Push 基础能力；新建 V2 meal-plan 数据模型；重构规则引擎外围；重写四日 planner、每餐确认/推送编排和 Web/API 边界。**
