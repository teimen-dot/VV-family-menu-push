# HANDOFF.md — 家庭菜单系统交接笔记

> 最后更新：2026-08-03
> 适用状态：Task A/B/C1/C2.1/C2.2/C2.2.1/C2.2.2/C2.3 后的当前真实架构

## 当前阶段闸门

- **C2.2 双域名 HTTPS：通过。**
- **C2.2 浏览器产品验收：通过。** Family、Worker、Admin、移动端布局和 PWA 均已人工验收。
- **C2.2.1 Owner/Worker 权限与账号收尾：通过。**
- **C2.2.2 新 Family UI：通过。** 新 UI 已接入真实 API/SQLite，不使用 mock 或 localStorage 业务数据。
- **C2.3 正式数据切换与备份：通过。** 腾讯云 SQLite 已成为唯一正式可写真相源。
- 当前必须保持：`APP_ENV=production`、`PUSH_ENABLED=false`、PushPlus token/topic 为空。
- Mac 数据源已于 2026-08-03 HKT 冻结；冻结快照保留在 `/private/tmp/family-menu-c2.3-final-20260803-170236/`，本地 8080/8090 不恢复。
- 正式数据库 SHA-256：`f8446e0bb42ed74eef97621d4092dd757520bbd8b45bac20e0579bcd70b318a4`。
- 正式数据数量：211 dishes、202 active、20 menus、99 menu_items、34 pantry、789 events、0 push_logs。

## 当前真实生产架构

```text
Internet
  |
HTTPS 443
  |
Nginx
  ├─ menu.ourmenu.site
  │    ├─ Family Basic Auth
  │    ├─ /、/api/、/health → 127.0.0.1:8090 app.py
  │    └─ /photos/ → 匿名只读 Nginx alias → /opt/family-menu/photos/
  │
  └─ admin.ourmenu.site
       ├─ 独立 Admin Basic Auth
       └─ 全部路径 → 127.0.0.1:8080 photo_manager.py
```

- 8080/8090 只监听 `127.0.0.1`，公网不开放。
- HTTP 分别重定向到各自 HTTPS；直接公网 IP 不提供业务页面。
- SAN 证书覆盖 `menu.ourmenu.site` 与 `admin.ourmenu.site`。
- Certbot timer 已启用，续期 dry-run 已通过；暂未启用 HSTS。
- Nginx 不使用 `/admin/` 或 `sub_filter`。

## 认证与角色

Family Nginx 使用：

```text
/etc/nginx/auth/family-menu-family.htpasswd
```

当前只保留两个用户名：

- `vivian` → Owner
- `kitchen` → Worker

角色映射位于 root-only `/etc/family-menu.env`：

```text
OWNER_AUTH_USERNAME=vivian
WORKER_AUTH_USERNAME=kitchen
```

Nginx 强制设置：

```nginx
proxy_set_header X-Authenticated-User $remote_user;
```

浏览器伪造同名 header 会被覆盖，不能提权。

权限规则：

- Owner 保留完整 Family 功能。
- Worker 登录 `/` 自动进入 `/pantry`。
- Worker 可浏览 Tomorrow、Pantry、Dishes、History，并可执行允许的 Pantry 操作。
- Worker 不显示 Confirm、Push、AI Fill、Add、Replace、Delete 等菜单写入控件。
- Worker 菜单写 API 返回 403。
- 旧 `family` 账号已在 root-only 备份后删除。
- Admin 使用独立 `/etc/nginx/auth/family-menu-admin.htpasswd`，与 Family 两个账号互不通用。
- 密码和 htpasswd 内容不得写入仓库、文档、env、脚本或日志。

## 数据与运行目录

```text
代码       /opt/family-menu/app/
SQLite     /opt/family-menu/data/family_menu.db
图片       /opt/family-menu/photos/
备份       /opt/family-menu/backups/
日志       /opt/family-menu/logs/ 和 systemd journal
环境文件   /etc/family-menu.env（root:root 0600）
```

- SQLite `family_menu.db` 是唯一业务数据源。
- 图片元数据来自 `dishes.image`，文件来自 `photos/`。
- `menu_data.json`、`dish_pool.json`、`photo_manifest.json` 不参与正式 H5、Manager 或 Push runtime。
- 普通菜品删除是 Soft Delete：`is_active=0`，保留 `dishes.image` 和图片文件。
- 腾讯云 `/opt/family-menu/data/family_menu.db` 是唯一正式可写真相源；Mac 数据库只作冻结归档。

## PushPlus 与调度状态

正式设计路径：

```text
VV Confirm
→ SQLite confirmed revision
→ PushService
→ PushPlus
→ push_logs
```

当前安全状态：

```text
APP_ENV=production
PUSH_ENABLED=false
PUSHPLUS_TOKEN=
PUSHPLUS_TOPIC=
H5_BASE_URL=https://menu.ourmenu.site
```

- 不得发送真实或测试 PushPlus。
- GitHub `main` 中四个 legacy workflow 的 `schedule` 已移除，正式 schedule 总数为 0；仅保留手动入口用于历史/诊断。
- 2026-08-02 至 2026-08-03 的五条错误通知已逐条确认来自旧 GitHub Actions schedule，不是 SQLite PushService。
- Mac launchd/cron 未发现推送任务；Lighthouse 未启用 reminder，DB/photos backup timers 已启用。
- 19:00、20:00 reminder 尚未启用；只允许在后续首次真实 Push 验证且用户明确指定启用日期后启用。
- 不再使用 10:30 daily push、21:00 reminder 或 GitHub Actions production scheduler。

## PWA 与移动端

- Family 和 Admin 均有 manifest、Apple Touch Icon、favicon、192/512 图标和 standalone PWA 配置。
- 不注册 Service Worker，不缓存 API 或业务数据；动态 HTML/JSON 返回 `Cache-Control: no-store`。
- Family Pantry 手机端无横向溢出，三个状态按钮完整显示。
- Admin 手机端菜品卡片每行三列，电脑端布局保持正常。

## 当前服务状态

```text
family-menu-app.service    active/enabled
family-menu-admin.service  active/enabled
nginx.service              active/enabled
certbot.timer              active/enabled
family-menu-db-backup.timer active/enabled
family-menu-photos-backup.timer active/enabled
```

未安装或未启用：

```text
family-menu-reminder-19.timer
family-menu-reminder-20.timer
```

## 已完成验收

- DNS 两个 hostname 均指向 `43.129.246.80`。
- HTTPS、SAN、HTTP→HTTPS、Certbot timer 和 renew dry-run 通过。
- Family/Admin 未认证均为 401；认证与交叉隔离通过。
- Family 匿名真实图片 200；缺失 404；写请求拒绝；目录浏览关闭。
- Admin 页面、API、图片均受 Admin Auth 保护。
- Owner 页面、API 和完整控件通过。
- Worker 默认 Pantry、控件隐藏、菜单 API 403、Pantry 权限通过。
- 客户端伪造 `X-Authenticated-User` 无法提升权限。
- 8080/8090 仅监听 localhost。
- `nginx -t`、SQLite `quick_check`、应用日志检查通过。
- 浏览器人工验收已确认 Family、Worker、Admin、手机布局和 PWA 正常。

## 上线后仍需单独授权

当前网站和备份已正式上线，PushPlus 与 reminder 仍保持关闭。后续必须另开 Gate：

1. 用户检查并接受新 UI。
2. 由 root 安全写入真实 PushPlus token/topic，仍保持 `PUSH_ENABLED=false`。
3. 使用正式 PushService 执行一次受控测试消息。
4. 用户确认只收到一条且内容正确后，才允许设置 `PUSH_ENABLED=true`。
5. 最后询问 19:00/20:00 reminder 从今天还是明天启用；未得到回答不得启用。

任何一步失败，应立即关闭 Push、停止 reminder，并按回滚文档处理；不得回退到旧 JSON 双数据源或旧 GitHub schedule 架构。
