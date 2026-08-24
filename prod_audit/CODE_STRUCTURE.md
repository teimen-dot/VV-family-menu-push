# Production Code Structure and Call Graph

基线：生产实际运行代码 `e9b64eccc4e65414a96c1654de0b724fd8c8c079`。

## 两层目录树

```text
/opt/family-menu/app/
├── app.py
├── db.py
├── menu_service.py
├── rule_engine.py
├── inventory.py
├── push_service.py
├── preference_service.py
├── ingredient_service.py
├── runtime_config.py
├── photo_manager.py
├── photo_security.py
├── nanny_reminder.py
├── push_menu.py
├── backup_data.py
├── local_preview.py
├── local-preview-index.html
├── gap_filler.py
├── migrate_*.py / rebuild_dishes_from_pool.py
├── import_excel.py / export_excel.py / merge_rotation_pools.py
├── audit_images.py / backfill_ingredient_translations.py
├── config.json / dish_pool.json / menu_data.json / photo_manifest.json
├── seasonal_tips.json
├── deploy/
│   ├── family-menu-*.service.example
│   ├── family-menu-*.timer.example
│   └── nginx-family-menu*.conf.example
├── pwa/
│   ├── family/
│   └── admin/
├── public/                 # 图标等静态资源；快照中排除二进制资源
├── photos/                 # 部署包内样例图片；正式图片运行目录实际为 /opt/family-menu/photos
├── .github/workflows/      # legacy/manual workflow；生产 schedule 总开关关闭
├── test_*.py
├── v5_blackbox_test.py / v6_blackbox_test.py
└── README.md / DEPLOYMENT.md / HANDOFF_HOME.md / PROGRESS.md / V*_TEST_REPORT.md
```

## 主要文件职责

| 文件/目录 | 职责 |
|---|---|
| `app.py` | 4,483 行单文件 H5/API 入口；session 鉴权、路由分发、SQL 查询辅助、页面 HTML/CSS/JS 渲染、推荐与智能换菜部分逻辑均在此。 |
| `db.py` | SQLite 连接、建表/兼容加列、事件日志和 config 读写。 |
| `menu_service.py` | 菜单生成、菜单项增删改、AI 补齐、人数 reconcile、整单确认/回退。 |
| `rule_engine.py` | 营养分析、餐次槽位、候选过滤、评分、GapFiller；现有规则最集中的模块。 |
| `inventory.py` | 双厨房 current pantry、库存版本、菜品 avail 推导、缺料和采购请求。 |
| `push_service.py` | PushPlus transport、确认版本冻结、幂等日志、重试与消息格式。 |
| `preference_service.py` | 从确认菜单累计 VV 菜品偏好分。 |
| `ingredient_service.py` | 中英词典兜底、食材精确匹配创建和待翻译标记。 |
| `runtime_config.py` | 生产环境、Push 开关、监听地址和 H5 URL 安全校验。 |
| `photo_manager.py` | 独立 Admin 管理站、菜品/食材/分类/照片管理 API 与页面。 |
| `nanny_reminder.py` | 19:00/20:00/21:00 legacy reminder CLI；当前 timer 未安装。 |
| `backup_data.py` | SQLite/照片备份。 |
| `migrate_*.py` 等 | 历史一次性迁移、重建、导入导出工具，不在 Family runtime 调用链。 |
| `gap_filler.py` | 旧 JSON 配餐 CLI；正式 API 使用的是 `rule_engine.py + menu_service.py`。 |
| `config.json`、`dish_pool.json`、`menu_data.json`、`photo_manifest.json` | 遗留配置/迁移数据。正式 Family 菜单和库存 runtime 的 Source of Truth 是 SQLite；这些文件不应作为新版数据源。 |
| `deploy/` | systemd/Nginx 示例，不等于服务器当前生效配置。 |
| `test_*.py` | 多轮功能、回归和结构测试；部分依赖未部署 fixture 或硬编码旧路径。 |

## 最近 10 条相关提交

```text
e9b64ec 2026-08-07 fix: use accepted meal plan UI in production
92bcb5b 2026-08-07 fix: derive carb replacement pools from catalog fields
4daaf8c 2026-08-07 fix: align real menu replacement and fill rules
1f2302f 2026-08-07 Record three-rule preview acceptance
0269d4c 2026-08-07 Handle default rice staple requirements
a7ecd45 2026-08-07 Align tomorrow replacement inventory rules
b3515de 2026-08-07 Fix tomorrow meal acceptance issues
f574308 2026-08-07 fix: unify meal planning rules
08ee5bd 2026-08-07 fix: smart replace and dish recommendations
64528f4 2026-08-07 fix: restore owner controls for confirmed tomorrow menu
```

## `app.py` 结构

| 区域 | 主要函数/类 | 说明 |
|---|---|---|
| 鉴权 | `authenticated_role`、`verify_family_password`、`create_session`、`session_from_cookie`、`post_path_allowed` | 用户名/密码 + HMAC 签名 cookie；runtime 角色只有 owner/worker。 |
| 菜品查询 | `get_all_dishes`、`get_dish_detail`、`get_categories` | 直接查询 SQLite，并拼装 required ingredients、图片和 avail。 |
| 推荐/换菜 | `get_dish_recommendations`、`smart_replace_menu_item` | 仍在 `app.py`，未完全下沉 service。 |
| 菜单设置 | `get/update_menu_diners`、`get/update_meal_setting`、`clear_menu_meal` | 整单 diners 与每餐 diners/note/is_skipped。 |
| 校验 | `validate_menu_meals`、`validate_menu_after_mutation` | 调用 `rule_engine` 槽位分析。 |
| 页面 | `render_meal_plan_reference`、`render_tomorrow`、`render_dishes`、`render_pantry`、`render_history` | Python 字符串内嵌 HTML、CSS 和 JavaScript。 |
| 路由 | `AppHandler.do_GET`、`AppHandler.do_POST` | 大型 `if/elif` 路由分发器。 |
| 启动 | `main` | 校验生产配置，固定先确保深圳明日菜单，再启动 `ThreadingHTTPServer`。 |

## 主要端点调用链

### 页面与读 API

```text
GET /health
→ AppHandler.do_GET → health_result → PRAGMA quick_check → SQLite

GET /tomorrow（或 /）
→ render_tomorrow
→ APP_ENV=production → render_meal_plan_reference
→ ensure_tomorrow_menu（owner）
→ menu_service.generate_and_store_menu（若不存在）
→ GapFiller / RuleEngine / ScoringEngine
→ inventory.check_dishes_availability_batch
→ SQLite menus + menu_items + dishes + current_pantry

GET /pantry
→ render_pantry → get_current_pantry / get_common_ingredients_static
→ SQLite current_pantry + ingredients + pantry_usage_stats

GET /dishes
→ render_dishes → 浏览器 GET /api/dishes

GET /history
→ render_history → get_history_menus
→ SQLite menus + menu_items + dishes

GET /api/dishes[?category=&search=]
→ get_all_dishes
→ inventory.check_dishes_availability_batch（传入 location 时）
→ SQLite dishes + categories + dish_ingredients + current_pantry

GET /api/dishes/{id}
→ get_dish_detail → SQLite dishes + dish_ingredients + ingredients

GET /api/dishes/{id}/availability-debug
→ inventory.check_dish_availability_debug
→ check_dish_availability → SQLite

GET /api/ingredients
→ get_all_ingredients → SQLite ingredients

GET /api/tomorrow
→ ensure_tomorrow_menu（owner）→ get_menu_with_dishes
→ SQLite menus + menu_items + dishes + avail

GET /api/history
→ get_history_menus → SQLite

GET /api/pantry 和 /api/pantry/last
→ get_current_pantry → SQLite current_pantry + ingredients

GET /api/purchase-requests
→ get_purchase_requests → SQLite purchase_requests + ingredients + dishes

GET /api/categories /api/diners
→ get_categories / get_all_diners → SQLite
```

### 写 API

```text
POST /login
→ verify_family_password(htpasswd) → create_session → signed cookie

POST /api/pantry/submit
→ inventory.save_pantry_changes
→ current_pantry UPSERT/soft-remove
→ inventory snapshot + legacy inventory sync
→ inventory_version++ + avail cache invalidate → SQLite

POST /api/pantry/add | update_status | remove | same-as-last
→ inventory 对应单项函数
→ inventory_version++（内容变化时）+ cache invalidate → SQLite

POST /api/pantry/add-by-name | consume
→ 仅 LOCAL_PREVIEW_UI=true；生产返回 404

POST /api/ingredients/add | update
→ ingredient_service.add_or_get_ingredient / update_ingredient_names
→ SQLite ingredients

POST /api/dishes/availability
→ app.get_dish_availability
→ inventory.check_dishes_availability_batch
→ check_dish_availability → SQLite dish_ingredients + current_pantry

POST /api/dishes/recommend
→ app.get_dish_recommendations
→ 餐次/category/近 3 日/烹饪方式评分
→ inventory.check_dishes_availability_batch → SQLite

POST /api/tomorrow/add | remove | replace
→ menu_service.add/remove/replace_dish_* → SQLite menu_items
→ replace 后 validate_menu_after_mutation → RuleEngine

POST /api/tomorrow/smart-replace
→ app.smart_replace_menu_item
→ 同餐次/同类或 carb/tofu 池
→ inventory avail=available 过滤
→ menu_service.replace_dish_in_menu
→ SQLite menu_items + menu_item_replace_history

POST /api/tomorrow/ai-fill
→ menu_service.ai_fill_menu
→ analyze_meal_slots / filter_candidates_for_slot / ScoringEngine
→ inventory avail=available 过滤
→ SQLite menu_items

POST /api/tomorrow/repair
→ menu_service.repair_menu
→ 删除非锁定 AI items
→ generate_and_store_menu → RuleEngine → SQLite

POST /api/tomorrow/confirm
→ menu_service.confirm_menu
→ 整个 menus.status=draft→confirmed
→ RuleEngine final review（warning 不阻断）
→ freeze confirmed_revision
→ preference_service.record_vv_confirm
→ 若 Push 总闸门允许则 push_service.push_confirmed_menu

POST /api/tomorrow/revert
→ menu_service.revert_to_draft → 整单回到 draft

POST /api/tomorrow/push
→ push_service.push_confirmed_menu(allow_retry=true)
→ revision/idempotency guard → PushPlus → push_logs + menus

POST /api/tomorrow/diners | meal-mode
→ 更新 menus
→ menu_service.reconcile_meal_for_diners
→ 保留 owner items、删多余 AI items、AI fill 缺口

POST /api/meal-plan/meal-diners | note | meal-state | clear-meal
→ app.update_meal_setting / clear_menu_meal
→ SQLite menu_meal_settings / menu_items

POST /api/purchase/update
→ inventory.update_purchase_status → SQLite purchase_requests
```

## 五个关键逻辑位置

| 逻辑 | 文件/函数 | 当前行为 |
|---|---|---|
| 菜单生成 | `menu_service.py:generate_and_store_menu` → `rule_engine.py:GapFiller.generate_day/generate_meal` | 生成早/午/晚，随后固定自动加下午茶；默认 seed 42。 |
| 推荐 | `app.py:get_dish_recommendations`、`rule_engine.py:ScoringEngine` | 推荐接口与整餐生成各有一套评分路径；历史只加减分，不是统一硬约束。 |
| 换菜 | `menu_service.py:replace_dish_in_menu`、`app.py:smart_replace_menu_item` | 手动替换直接写入；智能替换从同类 avail=available 池循环。 |
| 库存判断 | `inventory.py:check_dish_availability` | 每道菜由 required ingredients 与当前厨房 active pantry 精确 ID/alias/name 匹配，动态推导 available/almost_available/missing/incomplete；不存 dishes.avail。 |
| 推送 | `push_service.py:push_confirmed_menu`、`format_menu` | 仅整单 confirmed 菜单；按 revision 幂等、最多 3 次；发送至单一 PushPlus token/topic；正式推送排除下午茶。 |

库存变化由 `save_pantry_changes`、`add_ingredient_to_pantry`、`remove_ingredient_from_pantry`、`update_ingredient_status` 递增 location-specific `inventory_version` 并清除内存 avail cache。下次 API/生成调用时按新版本重算，不批量回写菜品字段。

## 双厨房现状

- Pantry：`current_pantry UNIQUE(location, ingredient_id)`，深圳/香港库存真实隔离。
- Pantry usage、snapshot、legacy inventory：均带 `location`，可隔离。
- 菜单：`menus.date UNIQUE`，不是 `UNIQUE(location,date)`；多数读取只按 `date` 查询。
- `ensure_tomorrow_menu(location)` 若该日期已有任一厨房菜单就直接复用；`main()` 启动时还固定执行 `ensure_tomorrow_menu("shenzhen")`。
- 正式库 20 条 menus 全部是 Shenzhen，Hong Kong 为 0。

结论：当前双厨房只有 pantry 隔离；meal plan 只是 UI location 切换，数据层未实现两份独立菜单。

## 写死、占位与遗留逻辑

- 未发现真正的 `TODO/FIXME` 待办标记。
- `main()` 写死先生成深圳明日菜单；多处函数默认 location 为 `shenzhen`。
- 自动生成/AI fill/repair 默认 seed 42，正常用户无参数时结果高度确定。
- 默认人数兜底为 4；晚餐 5 人以上仍沿用 4 人目标。
- 确认/推送权限除 env owner 映射外还写死用户名 `vivian`。
- 餐次枚举固定为 4 个：breakfast/lunch/afternoon_snack/dinner；无 late-night。
- 生成器固定自动创建下午茶，和新版“默认三餐”冲突。
- `LOCAL_PREVIEW_UI` 分支和 preview-only API 仍混在生产 `app.py`；当前生产变量未设置。
- `config.json` 含遗留家庭/推送配置，`dish_pool.json`、`menu_data.json`、`photo_manifest.json` 仍留在部署目录；正式 Family runtime 不以它们为业务数据源。
- `gap_filler.py` 是另一套旧 JSON 规则实现，容易与正式 `rule_engine.py` 混淆。
- 49 条历史 `menu_items.dish_id` 是整餐文本快照，不能关联 `dishes`；显示层有兼容拆分逻辑。
- `dietary_alerts` 被明确标为 deprecated，runtime/API/UI/AI 不读取。
- 中英翻译只有本地词典和 `translation_pending`，没有 LLM 调用。
- 没有 Qwen-VL/拍照识别实现。
