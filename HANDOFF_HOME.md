# 家庭菜单项目：家里电脑交接

更新时间：2026-08-06（Asia/Shanghai）

## 1. 当前项目状态

- 正式项目目录：`/Users/vv/WorkBuddy/Claw`
- 家里电脑应打开：Syncthing 的 `WorkBuddy/Claw`；若目录结构相同即 `/Users/vv/WorkBuddy/Claw`。
- Git 分支：`main`
- 最新 commit：`7fdf53f10217623cc185fb084e44ea57d6d629d0` — `hotfix: restore production Family interactions and sessions`
- 本机 Family Preview、Admin、Vite、Flask、Node 开发服务器均未运行；8080、8090、8091、3000、4173、5173 无监听，`family_menu.db` 无进程占用。
- 本地 `family_menu.db` 只读完整性检查：`ok`；它是 2026-07-31 的本地副本，不等于当前生产业务数据，禁止直接覆盖生产数据库。
- Push 定时任务必须保持关闭；本次仅做交接，没有部署。

## 2. 当前线上部署版本

生产目录：`/opt/family-menu/app`；Family/Admin 服务均为 `active`，`PUSH_ENABLED=false`。

| 文件 | SHA-256 | 生产修改时间 |
|---|---|---|
| `app.py` | `e03a426d9df5522138879d1781e999cea2caec5519b24db79ac2422e8487b058` | 2026-08-06 16:57:19 +0800 |
| `db.py` | `36c543e80f11c6ce55cb34a484c77c46ed47ffa628a43083972691e4ece7cb51` | 2026-08-06 16:57:19 +0800 |
| `push_service.py` | `200594d661f338592389b4fd8d1aa5f8f5fba9174fef05e72550b64f894dcabe` | 2026-08-06 16:27:38 +0800 |
| `photo_manager.py` | `5e50f00441f2fbd59c93632b5d802388cd0613d8a86feb4ce94fedc0ed370227` | 2026-08-05 18:22:34 +0800 |

上述四个生产哈希与当前本地文件一致。

## 3. 未提交修改（必须保留）

不要执行 `git reset --hard`、`git checkout -- .` 或覆盖工作区。

已修改的跟踪文件：

```text
.github/workflows/daily-push.yml
.github/workflows/nanny-reminder-19.yml
.github/workflows/nanny-reminder-20.yml
.github/workflows/vv-reminder-21.yml
.gitignore
app.py
db.py
inventory.py
menu_service.py
nanny_reminder.py
photo_manager.py
photo_manifest.json
photos/avocado_with_onion_dressing_and_salmon_roe.jpg
photos/fourflavour_tofu_salad.jpg
push_menu.py
rule_engine.py
```

未跟踪内容：

```text
.local-preview/
AGENTS.md
DEPLOYMENT.md
HANDOFF_HOME.md
audit_images.py
backfill_ingredient_translations.py
backup_data.py
deploy/
ingredient_service.py
local-preview-index.html
local_preview.py
photo_manager.log
photo_security.py
photos/dumpling_soup.jpg
photos/stirfried_choy_sum.jpg
photos/stirfried_choy_sum_with_garlic.jpg
photos/stirfried_green_vegetables_with_garlic.jpg
preview_push_sqlite.html
public/
push_service.py
pwa/
test_meal_plan_extension.py
test_requested_five_fixes.py
test_requested_three_fixes.py
test_requested_two_fixes.py
test_task_a.py
test_task_b.py
test_task_c221.py
test_tomorrow_search_mobile.py
```

## 4. 本地启动

先进入项目：

```bash
cd /Users/vv/WorkBuddy/Claw
```

Family 隔离预览（复制正式本地 SQLite 到 `.local-preview`，不会写源数据库）：

```bash
python3 local_preview.py
```

地址：`http://127.0.0.1:8091/tomorrow`

菜品管理器（会写本地 `family_menu.db` 和 `photos`）：

```bash
APP_ENV=development PUSH_ENABLED=false HOST=127.0.0.1 \
FAMILY_MENU_DB_PATH="$PWD/family_menu.db" PHOTO_DIR="$PWD/photos" \
python3 photo_manager.py
```

地址：`http://127.0.0.1:8080`

停止服务：在对应终端按 `Ctrl-C`，然后确认：

```bash
lsof -nP -iTCP -sTCP:LISTEN | grep -E ':(8080|8090|8091|3000|4173|5173)\b'
lsof family_menu.db
```

## 5. GrapesJS / design-lab

- 当前 WorkBuddy 同步根内未找到 `design-lab`，也未发现 GrapesJS 的 `package.json` 或启动配置。
- 因此 GrapesJS 启动命令和地址目前为：**不可用，不能猜测启动**。
- 发现的旧静态稿位于：`WorkBuddy/2026-07-30-15-39-12/family-menu-redesign`；它不是 GrapesJS 编辑器。
- 家里电脑若已有 `design-lab`，应先确认它被放入 Syncthing 的 `WorkBuddy/design-lab`，再依据其 `package.json` 启动；不要覆盖 `Claw`。

## 6. 测试命令

本轮相关回归：

```bash
python3 -m unittest -q \
  test_meal_plan_extension.py \
  test_tomorrow_search_mobile.py \
  test_hotfix_production_regression.py
```

语法检查：

```bash
PYTHONPYCACHEPREFIX=/private/tmp/family-menu-pyc \
python3 -m py_compile app.py db.py push_service.py photo_manager.py
```

全量测试：

```bash
python3 -m unittest -q
```

全量测试目前有两个既有无关失败：`test_v10_blackbox` 导入已移除的 `normalize_ingredient_id`；`test_task_c221` 仍断言旧 Nginx 身份头行为。不要为绕过它们修改业务功能。

## 7. 最近完成的内容

- Tomorrow 已扩展为“餐单 / Meal Plan”：今天只显示晚餐，明天显示四餐，按真实日期读取不同菜单。
- 每餐支持独立成员、备注新增/修改、× 跳过、恢复原菜；隐藏“已单独设置 / Custom”。
- 智能换菜记录最近 8 道，连续轮换时优先避免重复；相关历史持久化在 SQLite。
- 搜索换菜支持 300ms 防抖、最后请求生效、手机端处理中状态及失败保留弹窗。
- Pantry 常用食材按厨房使用频率动态生成；中英文食材翻译和新菜 Family API/图片同步已有修复。
- Kitchen 保持只读，菜单写接口返回 403；PushPlus 当前关闭。

## 8. 尚未完成的问题与下一步

1. Admin 菜品的“待审核”是旧迁移标记 `needs_review`；当前编辑保存不会清除，且没有“审核通过”按钮。下一步应增加明确的审核通过操作，或在完整保存后可靠地清除标记。
2. 修复上述两个全量测试基线问题，再跑全量回归。
3. 找回或确认 `WorkBuddy/design-lab` 的真实来源；当前电脑上不存在，Syncthing 无法同步不存在的目录。
4. 在家里电脑确认 Syncthing 完成后，先核对 `git status --short` 和本文件，不要立即部署。
5. 本地数据库较旧；如需生产数据副本，必须先做生产在线备份并下载为新的测试副本，不能替换或上传生产数据库。

## 9. Syncthing 与 Secret 安全

- `/Users/vv/WorkBuddy/.stfolder` 存在，Syncthing 正在运行；`Claw` 整个项目、`AGENTS.md`、本文件、`photos`、`family_menu.db` 均位于 WorkBuddy 同步范围。
- `design-lab` 当前不存在，无法确认同步。
- 已创建 `/Users/vv/WorkBuddy/.stignore`，忽略 `.env`、私钥文件、`*.db-wal` 和 `*.db-shm`。
- `Claw` 内未发现 SSH 私钥、真实 PushPlus Token 或服务器 Secret；文档/示例中的空值和 `CHANGE_ME` 不是 Secret。
- WorkBuddy 根目录存在真实 `.env`，现在已被忽略；若家里电脑以前已同步过该文件，应在家里电脑手动删除其副本，并重新轮换其中的第三方凭据。
