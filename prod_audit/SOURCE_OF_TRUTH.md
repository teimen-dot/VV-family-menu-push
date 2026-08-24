# Production Source of Truth

审计时间：2026-08-10 10:48 HKT（Asia/Shanghai）  
审计方式：仅 SSH/HTTP/SQLite 只读查询；未修改生产代码、数据库、进程或配置。

## 结论

本次审计对象是正式服务器 `43.129.246.80` 上 `/opt/family-menu/app/` 的实际运行代码，不是工作电脑副本，也不是 GitHub `main`。

生产代码逐文件对应提交：

```text
e9b64eccc4e65414a96c1654de0b724fd8c8c079
fix: use accepted meal plan UI in production
```

生产目录没有 `.git`，所以不能在服务器执行 `git status`。服务器内的 `RUNNING_COMMIT` 标记为上述 hash；将排除图片、缓存、日志、数据库和 credentials 后的 95 个生产文件与本地该提交逐文件比较，内容完全一致。唯一额外文件是 `RUNNING_COMMIT` 本身，未发现服务器私改。

## 正式运行进程

```text
主机                 VM-0-7-ubuntu
service              family-menu-app.service
状态                 active / running
PID                  2415808
用户                 family-menu
启动时间             2026-08-07 23:52:08 HKT
WorkingDirectory     /opt/family-menu/app
ExecStart             /usr/bin/python3 /opt/family-menu/app/app.py
Python               /usr/bin/python3.12
监听                 127.0.0.1:8090
数据库               /opt/family-menu/data/family_menu.db
```

Admin 为独立进程：

```text
service              family-menu-admin.service
状态                 active / running
PID                  2415809
ExecStart             /usr/bin/python3 /opt/family-menu/app/photo_manager.py
监听                 127.0.0.1:8080
```

`family-menu-app.service` 从 `/etc/family-menu.env` 与 `/etc/family-menu/pushplus.env` 载入环境。只确认了文件存在、用途和权限，没有导出内容。其他 credentials 也只核实存在，不进入交付物。

## 非敏感运行开关

从运行中进程严格筛选出的非敏感变量：

```text
APP_ENV=production
FAMILY_MENU_DB_PATH=/opt/family-menu/data/family_menu.db
H5_BASE_URL=https://menu.ourmenu.site
HOST=127.0.0.1
PUSH_ENABLED=false
PUSH_ON_CONFIRM=true
PUSH_SCHEDULE_ENABLED=false
LOCAL_PREVIEW_UI 未设置
```

因为 `PUSH_ENABLED=false`，即时推送总闸门关闭，即使 `PUSH_ON_CONFIRM=true` 也不会发送。19:00/20:00 reminder timer 均未安装；数据库和照片备份 timer 为 active/enabled。

## `/tomorrow` 的真实路由

生效 Nginx 配置没有单独的 `/tomorrow` location；Family 全部动态路径都转发到 8090：

```nginx
server {
    server_name menu.ourmenu.site;

    location ^~ /photos/ {
        auth_basic off;
        alias /opt/family-menu/photos/;
        autoindex off;
        disable_symlinks on;
        limit_except GET { deny all; }
    }

    location / {
        proxy_pass http://127.0.0.1:8090;
        proxy_set_header Host $host;
        proxy_set_header X-Real-IP $remote_addr;
        proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;
        proxy_set_header X-Forwarded-Proto $scheme;
        proxy_set_header X-Authenticated-User "";
    }
}
```

真实调用链：

```text
GET https://menu.ourmenu.site/tomorrow
→ Nginx 127.0.0.1:8090
→ /opt/family-menu/app/app.py
→ AppHandler.do_GET()
→ render_tomorrow()
→ APP_ENV=production
→ render_meal_plan_reference()
```

Family 身份验证现由 `app.py` 的登录页和签名 session cookie 完成；Nginx 主动清空浏览器传入的 `X-Authenticated-User`。Admin 仍由独立 Basic Auth 保护。

## Git/GitHub 对应关系

生产目录本身不是 Git checkout。可验证的对应关系如下：

```text
生产 RUNNING_COMMIT                              e9b64eccc4e65414a96c1654de0b724fd8c8c079
逐文件内容比较                                  与 e9b64ec 完全一致
本机 remote-tracking origin/codex/fix-smart-replace-recommendations
                                                  e9b64eccc4e65414a96c1654de0b724fd8c8c079
本机 remote-tracking origin/main                  6b0f84c45549cb1d4ee788f45c85c1a9314597c1
```

因此生产版本对应 GitHub 分支 `codex/fix-smart-replace-recommendations` 上的 `e9b64ec`，不对应 `origin/main`。审计时尝试实时 `git ls-remote`，GitHub 连接超时；上表的远端分支结论来自本机现有 remote-tracking ref，并由生产逐文件比对确认代码内容。

## 工作电脑与生产差异

工作电脑状态：

```text
branch               home-work-20260806
HEAD                 0e5304b16b99b47db832e7ab344cc0a9beb25235
relative to main     ahead 8
worktree             dirty
```

`e9b64ec..本地 HEAD` 的已提交差异为 72 个文件（2,421 insertions / 5,232 deletions）。主要差异清单：

- 修改：`.gitignore`、`app.py`、`backup_data.py`、`db.py`、`inventory.py`、`menu_service.py`、`photo_manager.py`、`photo_manifest.json`、`push_service.py`、`rule_engine.py`、部分部署模板、PWA 图标与测试。
- 新增：`HANDOFF.md`、DB backup systemd 模板、`family_ui/app.js`、`family_ui/index.html`、`family_ui/styles.css`、`test_inventory_menu_adjustments.py`、`test_task_c222.py`、`test_ui_browser.js`。
- 删除：生产提交中的 `AGENTS.md`、`DEPLOYMENT.md`、`HANDOFF_HOME.md`、`ingredient_service.py`、`local_preview.py`、`local-preview-index.html`、多个部署模板、public 图标、若干回归测试与少量图片。
- 二进制差异：部分菜品图片与 PWA 图标；不影响本次后端审计对象认定。

本地工作树相对本地 HEAD 另有未提交状态：

```text
D  HANDOFF.md
M  app.py
M  db.py
M  inventory.py
M  test_inventory_menu_adjustments.py
?? .local-preview/
?? AGENTS.md
?? CODEX_AUDIT_REQUEST.md
?? HANDOFF_HOME.md
?? NEW_VERSION_BACKEND_NEEDS.md
?? TASK_CURRENT.md
?? family_menu.db-shm
?? family_menu.db-wal
?? repair_required_ingredients.py
```

结论：本地副本与生产差异显著，不能作为本次生产审计的代码基线。

## 脱敏代码快照

交付文件：`production_code_e9b64ec_sanitized.zip`  
SHA-256：`ee3ba17ad422b7ce1efc15b2ca3ec45f7c7f1b6fb9d1c79b92a27522195d8494`

快照来自上述已逐文件验证的生产目录。已排除数据库及 WAL/SHM、运行数据 JSON、照片和二进制图标、日志、缓存、`.env`、私钥、Cookie、credentials 与其他敏感配置；保留 Python、HTML、部署示例、测试和文档。归档前后均核对文件名，并对保留的文本内容扫描私钥标记、常见高熵 Token 格式和含认证信息的 URL，未发现匹配。
