# EXECUTION_GUIDE.md — 14 项浏览器验收操作手册（PM 签发，2026-08-20）

> 本手册把 TASK_CURRENT.md 的 14 项验收写成**照单执行**的操作步骤。
> 执行 AI（或人工）按步骤操作，每完成一项：截图 → 更新 ACCEPTANCE_TRACKER.md 置 ✅。
> 环境：预览 `http://127.0.0.1:18765`（Owner 登录），写库 `assets/test-family_menu.db`。
> 验收口径：**UI 操作 → 真实 API → test DB → 刷新页面后仍保持**。
> 安全红线：不碰正式库/golden；`PUSH_ENABLED=false`；不生成假图。

## 登录（已由 PM 验证）

- 预览认证用户：`vivian`（Owner）。htpasswd 位于 `assets/preview-18765.htpasswd`。
- 流程：访问 `/` → 303 到 `/login` → 表单 POST（username/password）→ 会话 cookie。
- 密码不在代码/文档中，**需向用户索取预览密码**；无密码时无法执行浏览器验收，需先请用户提供或确认。

## 验收前基线（已由 PM 确认）

- 今日菜单 menu 189 = 2026-08-20 draft 4 人：breakfast 6 / lunch 4 / dinner 3，全部 ai 源。
- 食材样例：silken_tofu(available)、corn(available)、chicken(available)、16谷米(expiring)、rice(available) 等。
- 菜品库 dishes 数据齐备；bootstrap `readonly: False`。
- 测试闸门：`python3 -m unittest <file> -v`（本机无 pytest）。

---

## 1. 人数 4→3

1. 打开预览 → 餐单 Tab（明日/今日菜单）。
2. 找到人数控件（dinerNum），把 4 改成 3，保存/确认。
3. **预期**：调 `/api/menu/diners-count`，后端 `update_menu_diners_count` + `reconcile_meal_for_diners` 自动重配该餐，返回 `reconciled: true`；页面显示人数 3。
4. **验证**：刷新页面 → 人数仍为 3，菜单按 3 人份。
5. 截图：修改前（4 人）、修改后（3 人）、刷新后。

## 2. 新增/修改备注

1. 餐单页 → 某餐（如 lunch）备注输入框。
2. 输入备注（如"少盐"），保存。
3. **预期**：调 `/api/tomorrow/meal-note`，menus.meal_notes JSON 写入，audit event `meal_note_updated`。
4. **验证**：刷新 → 备注仍在；改内容再刷新 → 新内容。
5. 截图：备注输入、保存后、刷新后。

## 3. 换菜

1. 餐单页某菜品 → 换菜按钮 → 弹出候选列表（真实菜品源，含真实 dish_id）。
2. 选一道（如把"葱烧鸡"换成"辣子鸡"）。
3. **预期**：调 `/api/tomorrow/replace`，`replace_dish_in_menu` 更新 menu_item 的 dish_id；event `dish_replaced`。
4. **验证**：刷新 → 新菜仍在；DB 中该 menu_item.dish_id 已变。
5. 截图：换菜弹窗、换后、刷新后。

## 4. 添加菜

1. 餐单页 → 添加菜品按钮 → 搜索/选择一道菜。
2. **预期**：调 `/api/tomorrow/add`，`add_dish_to_menu(menu_id, dish_id, meal_type)` 新增 menu_item；event `dish_added`。
3. **验证**：刷新 → 新菜仍在对应餐。
4. 截图：添加前、添加后、刷新后。

## 5. 删除菜

1. 餐单页某菜品 → 删除按钮。
2. **预期**：调 `/api/tomorrow/remove`，`remove_dish_from_menu` 移除 menu_item；event `dish_removed`。
3. **验证**：刷新 → 菜已不在。
4. 截图：删除前、删除后、刷新后。

## 6. AI Fill

1. 餐单页 → AI 智能补充按钮（某餐或整日）。
2. **预期**：调 `/api/tomorrow/ai-fill`，`ai_fill_menu` 补齐缺口，返回补齐 N 道/无菜可补/结构完整 toast；event `ai_fill_menu`。
3. **验证**：刷新 → 补齐的菜仍在；不重复、不覆盖已确认餐。
4. 截图：AI Fill 前、点击后 toast、刷新后。

## 7. 确认一餐

1. 餐单页某餐 → 确认按钮。
2. **预期**：调 `/api/tomorrow/confirm`，菜单状态 draft→confirmed（或该餐锁定）；event `menu_confirmed`。
3. **验证**：刷新 → 该餐保持已确认；再点确认/重新生成不应改动它。
4. 截图：确认前、确认后、刷新后。

## 8. 添加食材

1. 食材 Tab → 添加食材（搜索或常用食材快捷添加，如"上海青"）。
2. **预期**：调 `/api/pantry/add` 或 `/api/pantry/add-by-name`，`add_ingredient_to_pantry` 写入 current_pantry；event `pantry_item_added`。
3. **验证**：刷新 → 食材仍在，英文名正确显示（上海青 → Shanghai Bok Choy）。
4. 截图：添加前、添加后、刷新后。

## 9. 标记快过期

1. 食材 Tab → 某食材 → 状态按钮 → 快过期。
2. **预期**：调 `/api/pantry/update_status`，`update_ingredient_status` 更新 status；event。
3. **验证**：刷新 → 状态保持"快过期"。
4. 截图：标记前、标记后、刷新后。

## 10. 用完→最近消耗→补货恢复

1. 食材 Tab → 某食材 → 用完（consume）。
2. **预期**：调 `/api/pantry/consume`，写 consumed_history；食材从当前列表消失。
3. **验证**：最近消耗列表出现该食材；刷新后仍在最近消耗。
4. 再对该食材 → 补货/恢复。
5. **预期**：`update_ingredient_status`（无独立 restock 端点）恢复为 available；刷新后回到食材列表。
6. 截图：用完前、消耗后、补货恢复后、刷新后。

## 11. 新增/编辑菜品

1. 菜品 Tab → 新增菜品（名称、分类、餐别、食材等字段）。
2. **预期**：调 `/api/dishes/create`，写 dishes；event `dish_added`。
3. **验证**：刷新 → 新菜在菜品库；编辑它（如改英文名）→ `/api/dishes/update` → 刷新后保持。
4. 截图：新增表单、新增后、编辑后、刷新后。

## 12. 早餐8道菜尺寸与4道菜一致

1. 餐单页早餐（现 6 道，可先添加至 8 道）。
2. **预期**：8 道时图片/文字/按钮尺寸与 4 道时一致，行高统一；页面向下增长，不自动压缩。
3. **验证**：手机视口（390×844）与桌面视口截图对比，元素尺寸一致。
4. 截图：4 道早餐、8 道早餐（手机视口）。

## 13. 食材列表无图片

1. 食材 Tab 检查每个条目。
2. **预期**：条目只有中文名/英文名/状态按钮，无图片。
3. **验证**：滚动整个列表截图。
4. 截图：食材列表全览。

## 14. 四 Tab + 深港切换

1. 依次点击 Tomorrow / Pantry / Dishes / History 四个 Tab，各页正常加载。
2. 切换 深圳/香港（location）→ bootstrap 重新加载对应数据。
3. **预期**：四 Tab 均无 console error；深港切换后数据按 location 隔离。
4. **验证**：刷新后仍正常；浏览器控制台无新增 error。
5. 截图：四 Tab 各一屏 + 深港切换后。

---

## 收尾流程（14 项全部 ✅ 后）

1. `python3 -m unittest test_*.py -v` 全量跑，确认无新增失败（先记录基线）。
2. 确认正式库/golden 零修改（SHA 对比）、`PUSH_ENABLED=false` 保持。
3. 更新 ACCEPTANCE_TRACKER.md 全部 ✅ + 证据路径。
4. 本地 commit（不 push），保持预览服务运行。
5. 回复：完成项编号+证据、新增 API 清单（应为空）、未完成项。
