# API_MAP.md — 14 项验收 → 现有后端端点映射（PM 盘点，2026-08-20）

> PM 已完成后端盘点：**14 项验收所需的写端点全部已存在**（app.py，标准库 http.server 手写路由）。
> 后续 AI 不需要新增端点、不需要探索代码——直接按此表接通 UI 即可。
> 若发现某端点实际行为与下表不符，**先记录到 ACCEPTANCE_TRACKER.md 再决定是否修**，禁止另造端点。

## 验收项 → 端点映射

| # | 验收项 | 后端端点（app.py POST） | 后端函数（menu_service / inventory） |
|---|--------|------------------------|--------------------------------------|
| 1 | 人数 4→3 | `/api/menu/diners-count` | `update_menu_diners_count` + `reconcile_meal_for_diners`（改人数即触发重配） |
| 2 | 新增/修改备注 | `/api/tomorrow/meal-note` | app.py 内联实现（menus.meal_notes JSON，≤500 字，写 audit event） |
| 3 | 换菜 | `/api/tomorrow/replace`（真实 dish_id） | `replace_dish_in_menu`；另有 `/api/tomorrow/cycle-replace` |
| 4 | 添加菜 | `/api/tomorrow/add` | `add_dish_to_menu(menu_id, dish_id, meal_type)` |
| 5 | 删除菜 | `/api/tomorrow/remove` | `remove_dish_from_menu(menu_id, menu_item_id)` |
| 6 | AI Fill | `/api/tomorrow/ai-fill` | `ai_fill_menu`（已确认/已推送保护） |
| 7 | 确认一餐 | `/api/tomorrow/confirm` | `confirm_menu`；撤销 `/api/tomorrow/revert` |
| 8 | 添加食材 | `/api/pantry/add`、`/api/pantry/add-by-name` | `add_ingredient_to_pantry`（精确中文解析，含英文名） |
| 9 | 标记快过期 | `/api/pantry/update_status` | `update_ingredient_status` |
| 10 | 用完→最近消耗→补货恢复 | `/api/pantry/consume`、`/api/pantry/update_status` | 消耗写 `consumed_history`；补货恢复=`update_ingredient_status`（无独立 restock 端点） |
| 11 | 新增/编辑菜品 | `/api/dishes/create`、`/api/dishes/update` | dish CRUD（软删除 is_active=0 语义保留） |
| 12 | 早餐8道菜尺寸统一 | 纯前端 CSS（public/family-menu/index.html） | 无后端改动；验收=视觉一致 + 页面向下增长不压缩 |
| 13 | 食材列表无图片 | 纯前端（public/family-menu/index.html 渲染） | 无后端改动；验收=条目只含中文名/英文名/状态按钮 |
| 14 | 四 Tab + 深港切换 | `/api/family-menu/bootstrap`、`/api/tomorrow`、`/api/pantry`、`/api/dishes`、`/api/history` | bootstrap 读 location；切换=重新 bootstrap |

## 关键约束（TASK_CURRENT + 门禁重申）

- 所有写操作走真实 API 落 `assets/test-family_menu.db`，刷新后保持。
- `generate / AI Fill / reconcile` 只认 `diners_count`；不恢复成员名单人数链、不恢复餐单家宴模式。
- 菜品侧 `banquet` 家宴菜语义保留（可搜索、可手动加入）。
- 历史页业务只读。
- **不新增端点、不另造业务逻辑、不改 rule_engine 规则语义。**

## PM 实测结论（2026-08-20）

- **预览服务 18765 连的是 `assets/test-family_menu.db`**（已验证：健康检查触发该库 -shm 时间戳更新，writable 空库不动）。
- `writable/family_menu.db` 是 0 表空库（4096B），**不是预览库，任何 AI 不得误写**。
- **events.created_at 存的是 UTC**，核对验收时间需 +8 换算 HKT。
- T003E 的 preview regeneration 重建过菜单 items（当前库已无 T002 的 item 3220），**T002 验收结果已失效，必须按 14 项口径重新走查**。
- **全局只读模式已解除（PM 预检）**：bootstrap 返回 `"readonly": False`；前端无「只读预览/Read only/暂未开放」残留（唯一命中是业务"查看态"说明文字）；app.js 显示 "WRITES ENABLED"。
  - `build_family_ui_readonly_tabs` / `render_family_menu_readonly` 等命名带 readonly 的符号是**历史命名残留**，实际返回写模式数据，勿误判为只读分支。
- 14 项验收所需数据齐备：24 menus / 266 items / 2095 events，test DB `quick_check: ok`。

## 测试闸门（PM 预检，2026-08-20）

- **本机无 pytest，用 `python3 -m unittest <file> -v` 跑测试**。
- 全量测试集（T002 等 18 项）实测可跑通，且**全部隔离**：`patch.object(db, "DB_PATH", tmp/t002.db)` 指向临时目录，不碰正式库与 test DB。
- T003 系列测试对真实库只读（`mode=ro` + `skipUnless` 保护），不会写坏 test DB。
- v10/v11 legacy 黑盒测试不访问真实库。
- 「全量测试无新增失败」验收闸门真实可用，不是纸面承诺。

## 前端控件（PM 预检，2026-08-20）

- `public/family-menu/index.html` 为 UTF-8，四 Tab 写操作所需控件**全部齐备**：
  diners(28)、换菜(47)、确认(24)、收藏(12)、添加(14)、ai-fill、饮品、备注等交互元素均在。
- 前端无「只读预览/Read only/暂未开放」残留；bootstrap `readonly: False`。
- test DB 今日菜单已就绪：menu 189 = 2026-08-20 draft 4 人，breakfast 6 / lunch 4 / dinner 3，
  可直接用于 14 项验收操作。
- **结论：UI 控件与后端端点两侧都已接通，14 项验收 = 纯走查执行 + 填证据。**

## 写链路实测（PM 隔离副本验证，2026-08-20）

- 在 test DB 的**隔离副本**上真实调用（不污染 assets 库，事后已核对原库零变化）：
  - `update_menu_diners_count(189, 3)` → OK，menus.diners_count 4→3 落库读回，新增 audit event `diners_count_updated`。
  - 备注写（app.py meal-note 同款逻辑）→ menus.meal_notes JSON 落库读回 `{"lunch":"少盐"}`。
  - 换菜 `replace_dish_in_menu` → OK，menu_item.dish_id 更新读回。
  - 添加菜 `add_dish_to_menu` → OK（未锁定菜；被 4 天锁定池内的菜返回 False 是**规则正确生效**，非 bug）。
  - 删除菜 `remove_dish_from_menu` → OK。
  - AI Fill `ai_fill_menu` → OK（餐满时返回"补充 0 道"，逻辑正确）。
  - 确认餐 `confirm_menu` → OK，menu draft→confirmed，重复确认幂等安全。
  - 添加食材 `add_ingredient_to_pantry` → OK，current_pantry 落库读回。
  - 标记快过期 `update_ingredient_status` → OK，status 更新读回。
  - 新增菜品 `save_family_dish` → OK，dishes 落库读回（dish_0234）。
- **结论：14 项验收的全部写链路代码路径均实测可通，audit event 正常记录。**
- 验证全程零污染：assets 测试库核对（menu 189 仍 4 人 draft、events 2095、max item 3399、无测试菜品）全部保持。

## 测试基线（PM 全量跑测，2026-08-20）

- 全量 `unittest discover`：**191 项，190 通过，1 错误**（见下）。
- ❌ `test_adjudication_20260817.MigrationProtectionTests.test_production_shape_strict_dry_run_split_and_idempotency`
  - **根因**：该测试默认 backup 源是 `family_menu.db`（工作区本地），但 writable 的 `family_menu.db` 是 **0 表空库**（T003 系列 20:40 破坏痕迹）→ `ensure_schema` 报 no such table。
  - **对照**：Claw 主工作区同测试 ✅（其 family_menu.db 是 08-16 冻结生产副本，211 dishes/20 menus/789 events）。
  - **修复指引**：设 `ADJUDICATION_PRODUCTION_BACKUP=<Claw/family_menu.db 或 assets 测试库>` 即可跑通；或把 Claw 冻结库复制为 writable 的 backup 源。
  - **不影响 14 项验收**：该测试是历史迁移保护，不在 EXECUTION_GUIDE 门禁内；全程临时库隔离，不碰真实库。
- 其余 190 项全过，T002 黑盒 18 项全过，验收闸门基线健康。

## 四 Tab 路由与 SHA 基线（PM 验证，2026-08-20）

- 四 Tab 路由：`/`（=tomorrow）、`/tomorrow`、`/pantry`、`/dishes`、`/history`（app.py 3762 行）。
- 前端导航：data-page = menu / pantry / dishes / history（index.html）。
- **SHA 基线（完成定义对比用）**：
  - golden：`d84d5bba8fb461ffb6a4f4710ef5264aeaa5dc8844f3d2dbd4323ab9576ab12a`
  - test DB（当前）：`ba8730f13bfe25577d7d7e9eb45c109b000a9f2e7936e778a1a7dbabfa159b22`
  - 验收结束后对比 test DB SHA 变化（应只含验收写入，golden 必须不变）。

## HTTP 层接线（PM 验证，2026-08-20）

- POST 流程：登录（`/login` 表单 + 限流）→ 会话 cookie → `do_POST` 内 `session_from_cookie` 认证（401 拦截）→ `read_json()` 按 Content-Length 读 body → 分发到对应处理。
- 写保护：已确认菜单返回 409（"已确认菜单不可直接修改，请先回退到草稿"）；menu location 不匹配返回 403。
- **结论：HTTP 请求层 → 认证 → body 解析 → 业务函数 → 落库 全链路接线完整，无缺口。**

## 手机 UI 修复（#12 #13）注意事项

- 早餐列表统一：图片尺寸、中英文文字、收藏/换菜/搜索/删除按钮、行高；菜多只向下增长。
- 食材条目去图片：只留中文名/英文名/状态按钮。
- 缺图复核（榨菜肉丝/煎鸡翅/红烧肉）：服务器有真实文件→修正映射；没有→保持 fallback 并列入缺图名单；**不得生成假图**。

*PM 盘点完成。AI 开工前先读 PM_GATE.md → TASK_CURRENT.md → 本表。*
