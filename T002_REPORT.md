# T-002 执行报告（2026-08-19）

## 1. 修改文件

- `rule_engine.py`：按冻结规则重做逐槽生成、硬上限、轮换与受控降级。
- `menu_service.py`：AI Fill 复用规则候选、已确认/已推送保护、未满足原因兼容。
- `app.py`：食材英文名解析与既有空英文展示覆盖，不执行历史回填。
- `public/family-menu/index.html`：真实菜品源、真实 `dish_id` 换菜、三处共享图片渲染与中性 fallback。
- `test_t002_blackbox.py`：冻结文档 §11 十五项黑盒与 UI/食材补充测试。
- `missing-photos-20260819.json`：17 项照片资产核验记录。
- `ingredient-name-en-backfill-20260819.json`：既有空英文名回填清单（42 项）。
- `draft_backfill_ingredient_name_en.py`：只读默认、显式 `--apply` 才写库的脚本草稿；本轮未执行写入模式。
- `screenshots/T002/*.jpg`：390×844 手机端三页面和换菜弹窗截图。
- `T002_REPORT.md`：本报告。
- 未修改 `deploy/index.html`。

## 2. T2.1 缺图核验

- 对 17 个目标文件逐项只读请求 `https://menu.ourmenu.site/photos/<filename>`：17/17 返回 HTTP 200。
- 预览 photos 原有 7 个，缺少 10 个；仅下载生产端真实存在的 10 个文件到预览 photos。
- 同步后 17/17 文件存在，均验证为 400×400 JPEG；未生成假图，未对生产/管理端执行上传、删除、改名或修改。
- `missing-photos-20260819.json` 保留完整 17 项审计字段：文件、菜名、`name_en`、类目、fallback emoji、生产可达性及同步结果；同步后 `missing_after_sync=0`。

## 3. T2.2 换菜与图片渲染

- 换菜弹窗数据源改为 bootstrap/API 返回的真实菜品集合；条目携带真实 `dish_id`，replace 请求使用 `new_dish_id:d.id`。
- 弹窗、菜单行、菜品库网格统一调用 `makeDishTile()`；图片统一 `object-fit:cover`、`loading=lazy`、`decoding=async`，加载失败时原位 emoji fallback，不改变行高。
- `categoryOf()` 未匹配时返回 `neutral/🍽️`，不再默认 `staple/🍚`；长英文名 small 行单行省略。
- 真实持久化：`menu_item.id=3220` 从 `dish_0231/红烧肉` 替换为 `dish_0198/辣子鸡`；数据库记录变为 owner lock，页面刷新后仍为 `dish_0198/辣子鸡`；preview DB `quick_check=ok`。
- 手机弹窗同类可做分组优先、图片 lazy；390×844 下打开、滚动与选择交互正常。

## 4. T2.3 改前样例与规则对照

### 改前生成样例（代码修改前，预览库临时副本，seed=42）

- 2026-08-19 早餐：8 道——鸡肉淮山粥、红薯/番薯、汤饺、原味蒸鸡蛋、炒冬瓜片、手撕茄子、蒜蓉上海青、榨菜肉丝。问题：缺豆腐，额外出现 protein_main，超过冻结的 7 道结构。
- 2026-08-20 早餐：8 道——藜麦淮山粥、淮山/山药、蛋卷、丝瓜豆腐鸡蛋汤、山药疙瘩汤、黑鱼子酱配豆腐、水煮蛋、煎蛋。问题：3 道 egg_dish、普通汤进入早餐自动菜单、缺主食伴侣。

### 冻结规则 → 代码改动点

| 冻结规则 | 代码改动点 |
|---|---|
| 早餐粥1/伴侣1/豆腐1/蛋1/蔬菜2/粗粮1，无额外蛋白槽 | `rule_engine.py`: `MEAL_SLOT_ORDER`、`analyze_meal_slots()`、`GapFiller.generate_meal()` |
| 午晚人数矩阵，午快汤/晚慢汤 | `RuleEngine._meal_target()` 与显式 quick/slow soup 槽 |
| egg_dish 同餐≤1、自动全天≤2，仅认 role | `MealState.auto_egg_dish_count`、候选硬上限、`final_review()` |
| 豆腐不抵肉类 | `meat_main` 独立槽与 `MEAT_PROTEINS` 判定 |
| 同餐 dish_id 不重复 | 每餐 state id 硬过滤 |
| 4 天硬锁、5–7 天软排序 | `get_rotation_context()`、`choose_rotation_candidate()` |
| 仅低于 §4.6 最小池规模才降级 | `AUTO_POOL_MINIMUMS`、`get_slot_candidates()` 与降级警告 |
| 换菜/删除/取消释放 reservation | rotation 查询实时读取 menu_items，并排除 skipped meal |
| 手动 onepot 整餐覆盖；早餐自动汤饺不覆盖 | `has_manual_one_pot_meal` source 语义 |
| 生成≠推送；已确认餐不回改 | `generate_and_store_menu()` confirmed/pushed 保护；未新增推送调用 |

## 5. T2.3 十五项黑盒验收

以下 15 项均由 `test_t002_blackbox.py` 自动验收通过：

1. PASS — 早餐有且仅 1 道 `egg_dish`（蛋豆合一可一顶二）。
2. PASS — 早餐无额外蛋白槽仍完整为 7 道。
3. PASS — 同一天自动菜单 `egg_dish ≤ 2`。
4. PASS — 午餐 3 人为 2 蛋白 + 1 蔬菜 + 1 主食 + 1 快汤。
5. PASS — 晚餐 3 人为 2 蛋白 + 1 蔬菜 + 1 主食 + 1 慢汤。
6. PASS — 晚餐 4 人为 2 蛋白 + 2 蔬菜 + 1 主食 + 1 慢汤。
7. PASS — 豆腐不抵肉类，仍有非 tofu 肉类主菜。
8. PASS — 两道蛋白质优先不同主蛋白来源。
9. PASS — 汤内肉/菜/蛋不占主菜槽。
10. PASS — 4 天内跨餐次零重复，第 5 天可回。
11. PASS — 换菜/删除/取消后 reservation 释放。
12. PASS — 仅池规模低于门槛时允许窗口重复并给提示，不出空白餐。
13. PASS — 手动 onepot 后 AI Fill 零新增。
14. PASS — 早餐自动汤饺不触发 onepot 整餐覆盖。
15. PASS — 深圳/香港轮换历史与库存隔离。

附加保护同样通过：已确认菜单不回改；UI 真实 ID/共享渲染/中性 fallback 契约；上海青英文名契约。

## 6. T2.4 食材英文名

- add-by-name 精确解析 `上海青 → Shanghai Bok Choy`，不走“小白菜”近似映射。
- 在预览库执行允许的上海青 pantry 写入后，页面立即显示 `Shanghai Bok Choy`，刷新后仍保持。
- 既有 `ingredients.name_en=''` 仍为空，证明未执行回填；展示与响应通过解析覆盖英文名。
- 输出 42 项空英文名清单及脚本草稿；脚本仅以默认只读模式生成清单，未使用 `--apply`。

## 7. 新增 API

无。

## 8. 浏览器持久化与截图

- 390×844 手机视口完成餐单、食材、菜品三页面及换菜弹窗打开态验收。
- 浏览器 console：0 error，0 warning。
- 本任务相关成功操作均为 2xx；4 天锁拒绝也保持业务 JSON 200，未观察到新增 4xx/5xx。
- 截图：`01-plan-mobile.jpg`、`02-swap-modal-mobile.jpg`、`03-pantry-mobile.jpg`、`04-dishes-mobile.jpg`。

## 9. 全量测试

- `python3 -m unittest`：129 tests passed，1 skipped，0 failures。
- `python3 -m unittest test_t002_blackbox`：18 tests passed。
- 既有 phase2/裁决定向回归：24 tests passed，1 skipped。
- 三个 HTML script 块经 Node 语法检查通过；相关 Python 文件编译检查通过。

## 10. 测试库 / 正式库隔离

- 写入仅发生在允许的预览库 `test-family_menu.db`：T2.2 的 `menu_item` 替换与 T2.4 的上海青 pantry 写入。
- 正式库、golden 库均未写；主工作区未修改；`deploy/index.html` 未修改。

## 11. before-T002 快照

`/Users/heymen/workbuddy/claw-family-ui-phase2-assets-20260819/test-family_menu.before-T002.db`

## 12. 本地 commit

本报告与 T-002 代码、测试、审计 JSON、脚本草稿、截图一并提交到本地分支；未 push。

## 13. 部署结果

未部署；不 push。

## 14. 尚未完成问题

无本轮阻塞项。17 个生产照片文件均已找到并同步到预览，因此不需要等待用户补图。
