# 家庭菜单系统部署手册 V2.1

> **平台**：Tencent Cloud Lighthouse（中国香港）  
> **系统**：Ubuntu 24.04 LTS，2 vCPU / 2 GB RAM / 40 GB SSD / 20 Mbps  
> **适用代码**：Task A / A.1 / A.2 / Task B 后的当前 SQLite 单数据源版本  
> **本文性质**：最终部署操作方案；生成本文不代表已经购买、连接或修改服务器。

## 1. 当前真实架构

```text
Internet
  ↓ 80/443
Nginx
  ├─ menu.ourmenu.site  → Family Basic Auth → 127.0.0.1:8090 app.py
  │    └─ /photos/ → anonymous read-only alias → /opt/family-menu/photos/
  └─ admin.ourmenu.site → Admin Basic Auth  → 127.0.0.1:8080 photo_manager.py

app.py / photo_manager.py / AI / Search / Push
  ↓
/opt/family-menu/data/family_menu.db

photos files        /opt/family-menu/photos/
photos metadata     SQLite dishes.image
confirmed delivery  Confirm → revision → PushService → PushPlus → push_logs
```

没有第二条定时菜单推送链。`menu_data.json`、`dish_pool.json` 和
`photo_manifest.json` 仅是 legacy/manual/migration 文件，不是正式 runtime 数据源。
图片 URL 来自 `H5_BASE_URL/photos/<dishes.image>`，不使用 GitHub Raw。

运行约束：

- 两个 Python 服务均使用标准库 `ThreadingHTTPServer`，单实例、单进程，不使用 Flask、Gunicorn、Uvicorn 或多 worker。
- SQLite 启用 `foreign_keys=ON`、WAL 和 `busy_timeout=5000`。
- 8080/8090 只监听 `127.0.0.1`；公网只到 Nginx。
- 本地默认 `APP_ENV=development`、`PUSH_ENABLED=false`。
- 真实 Push 必须同时满足 `APP_ENV=production` 和 `PUSH_ENABLED=true`。

## 2. 上线资料与 Gate

部署者需要准备：

- Lighthouse 公网 IPv4 地址。
- SSH key；临时密码只用于首次登录且不得写入本文或仓库。
- Family Basic Auth 用户名和密码。
- 不同于 Family 的 Admin Basic Auth 用户名和密码。
- 已配置 Family 域名 `menu.ourmenu.site` 与 Admin 域名 `admin.ourmenu.site`。
- PushPlus token 与 topic。

任何 secret 都不得进入 Git、源码、systemd unit 或 Nginx 配置。生产 secret 只放在
`/etc/family-menu.env` 或 root 管理的 htpasswd 文件中。

## 3. 创建 Lighthouse 与防火墙

实例规格：香港、Ubuntu 24.04 LTS、2 vCPU、2 GB、40 GB、20 Mbps、单实例。

Lighthouse 防火墙只开放：

| 端口 | 用途 |
|---:|---|
| 22/tcp | SSH，完成后尽量限制来源 IP |
| 80/tcp | HTTP、ACME challenge、HTTPS 跳转 |
| 443/tcp | HTTPS |

不要开放 8080 或 8090。Lighthouse 防火墙和应用绑定 `127.0.0.1` 构成双重保护。

## 4. SSH Key

在可信本机生成专用 key（已有合适 key 可跳过）：

```bash
ssh-keygen -t ed25519 -a 64 -f ~/.ssh/family_menu_lighthouse
ssh-copy-id -i ~/.ssh/family_menu_lighthouse_ed25519.pub ubuntu@43.129.246.80
ssh -i ~/.ssh/family_menu_lighthouse_ed25519 ubuntu@43.129.246.80
```

确认 key 登录成功后再编辑 `/etc/ssh/sshd_config.d/99-family-menu.conf`：

```text
PasswordAuthentication no
PermitRootLogin no
PubkeyAuthentication yes
```

验证配置后平滑重载，保持原 SSH 会话不关闭，并另开终端再次验证：

```bash
sudo sshd -t
sudo systemctl reload ssh
```

## 5. 安装系统包

```bash
sudo apt update
sudo apt upgrade -y
sudo apt install -y \
  nginx python3 sqlite3 apache2-utils \
  certbot python3-certbot-nginx \
  rsync curl ca-certificates
python3 --version
sqlite3 --version
```

Admin 使用独立 hostname，直接保留 Manager 原生 `/api/` 和 `/photos/`，不依赖 `sub_filter`。

设置服务器时区便于人工读日志；timer 本身也显式使用香港时区：

```bash
sudo timedatectl set-timezone Asia/Hong_Kong
timedatectl
```

## 6. 本地迁移前保护与上传

当前本地项目是 `/Users/vv/WorkBuddy/Claw`。先在本地创建一致性备份；不要直接复制正在写入的 SQLite 主文件：

```bash
cd /Users/vv/WorkBuddy/Claw
export PREDEPLOY_DIR="$HOME/family-menu-predeploy-$(date +%Y%m%d_%H%M%S)"
mkdir -p "$PREDEPLOY_DIR"
BACKUP_DIR="$PREDEPLOY_DIR" BACKUP_REMOTE_ENABLED=false \
  python3 backup_data.py all
python3 audit_images.py > "$PREDEPLOY_DIR/image-audit.json"
```

确认生成一个 `family_menu_*.db` 和一个 `photos_*.tar.gz`，并离线保存。

服务器创建最小权限账号与目录：

```bash
sudo useradd --system --home /opt/family-menu --shell /usr/sbin/nologin family-menu
sudo install -d -o root -g family-menu -m 0750 /opt/family-menu/app
sudo install -d -o family-menu -g family-menu -m 0750 \
  /opt/family-menu/data /opt/family-menu/photos \
  /opt/family-menu/backups /opt/family-menu/logs
```

从本地上传代码时使用明确白名单，不上传 `.git`、cache、日志、开发备份、临时 DB 或 secret：

```bash
cd /Users/vv/WorkBuddy/Claw
cat > /tmp/family-menu-runtime-files.txt <<'EOF'
app.py
db.py
inventory.py
menu_service.py
rule_engine.py
preference_service.py
push_service.py
push_menu.py
nanny_reminder.py
photo_manager.py
runtime_config.py
photo_security.py
backup_data.py
audit_images.py
config.json
EOF
rsync -av --files-from=/tmp/family-menu-runtime-files.txt \
  ./ ubuntu@43.129.246.80:/tmp/family-menu-app/
scp "$PREDEPLOY_DIR"/family_menu_*.db ubuntu@43.129.246.80:/tmp/family_menu.db
rsync -av --exclude='.gitkeep' ./photos/ ubuntu@43.129.246.80:/tmp/family-menu-photos/
rsync -av ./deploy/ ubuntu@43.129.246.80:/tmp/family-menu-deploy/
scp family-menu.env.example ubuntu@43.129.246.80:/tmp/
```

服务器落盘并设置权限：

```bash
sudo rsync -a --delete /tmp/family-menu-app/ /opt/family-menu/app/
sudo install -o family-menu -g family-menu -m 0640 /tmp/family_menu.db \
  /opt/family-menu/data/family_menu.db
sudo rsync -a --delete /tmp/family-menu-photos/ /opt/family-menu/photos/
sudo chown -R root:family-menu /opt/family-menu/app
sudo find /opt/family-menu/app -type d -exec chmod 0750 {} \;
sudo find /opt/family-menu/app -type f -exec chmod 0640 {} \;
sudo chown -R family-menu:family-menu \
  /opt/family-menu/data /opt/family-menu/photos \
  /opt/family-menu/backups /opt/family-menu/logs
sudo find /opt/family-menu/photos -type d -exec chmod 0750 {} \;
sudo find /opt/family-menu/photos -type f -exec chmod 0640 {} \;
sudo usermod -aG family-menu www-data
sudo systemctl restart nginx
```

不要上传本地 `server.log`、`photo_manager.log`、`backups/`、测试缓存或任何真实 env 文件。

## 7. EnvironmentFile：先安全启动，再切生产

创建 `/etc/family-menu.env`：

```bash
sudo install -o root -g root -m 0600 /tmp/family-menu.env.example /etc/family-menu.env
sudoedit /etc/family-menu.env
```

阶段 A（只有 IP、尚未 HTTPS）必须使用：

```text
APP_ENV=development
PUSH_ENABLED=false
PUSHPLUS_TOKEN=CHANGE_ME
PUSHPLUS_TOPIC=CHANGE_ME
H5_BASE_URL=http://43.129.246.80
FAMILY_MENU_DB_PATH=/opt/family-menu/data/family_menu.db
PHOTO_DIR=/opt/family-menu/photos
HOST=127.0.0.1
MAX_UPLOAD_BYTES=8388608
BACKUP_DIR=/opt/family-menu/backups
BACKUP_RETENTION=14
BACKUP_REMOTE_ENABLED=false
BACKUP_REMOTE_COMMAND=CHANGE_ME {artifact}
```

阶段 A 的 `H5_BASE_URL` 只用于 Preview/页面测试；`APP_ENV=development` 时允许 HTTP，
但绝不启用真实 Push。不要通过放宽代码校验来让 IP 冒充 production。

域名和 HTTPS 全部通过后，才切换为：

```text
APP_ENV=production
PUSH_ENABLED=false
PUSHPLUS_TOKEN=<由 root 写入，不回显>
PUSHPLUS_TOPIC=<由 root 写入>
H5_BASE_URL=https://menu.ourmenu.site
```

完成最终 Push Gate 后最后一步才改 `PUSH_ENABLED=true`。

## 8. 安装 systemd 服务

从模板安装：

```bash
sudo install -m 0644 /tmp/family-menu-deploy/family-menu-app.service.example \
  /etc/systemd/system/family-menu-app.service
sudo install -m 0644 /tmp/family-menu-deploy/family-menu-admin.service.example \
  /etc/systemd/system/family-menu-admin.service
sudo systemd-analyze verify \
  /etc/systemd/system/family-menu-app.service \
  /etc/systemd/system/family-menu-admin.service
sudo systemctl daemon-reload
sudo systemctl enable --now family-menu-app.service family-menu-admin.service
```

验证只监听 localhost：

```bash
sudo ss -lntp | grep -E ':8080|:8090'
curl -fsS http://127.0.0.1:8090/health
curl -fsS http://127.0.0.1:8080/health
sudo systemctl status family-menu-app family-menu-admin --no-pager
sudo journalctl -u family-menu-app -u family-menu-admin -n 100 --no-pager
```

预期地址只能是 `127.0.0.1:8080` 和 `127.0.0.1:8090`。

## 9. Basic Auth

创建两个不同账号、不同密码；命令会交互式询问密码：

```bash
sudo install -d -o root -g www-data -m 0750 /etc/nginx/auth
sudo htpasswd -c /etc/nginx/auth/family-menu-family.htpasswd FAMILY_USERNAME
sudo htpasswd -c /etc/nginx/auth/family-menu-admin.htpasswd ADMIN_USERNAME
sudo chown root:www-data /etc/nginx/auth/*.htpasswd
sudo chmod 0640 /etc/nginx/auth/*.htpasswd
```

不得复用两个密码，不要把命令历史改成带 `-b` 明文密码形式。

### Family 图片的认证边界

`menu.ourmenu.site` 上只有 `/photos/<filename>` 是匿名只读资源，供 PushPlus/微信客户端加载。
Nginx 直接使用 `alias /opt/family-menu/photos/` 提供文件，不经过 Python；`autoindex off`，只允许
GET/HEAD，其他方法拒绝，缺失文件正常 404。`www-data` 仅通过 `family-menu` 组获得图片读取权限。

Family 的 `/`、`/api/`、`/health` 仍要求 Family Basic Auth。`admin.ourmenu.site` 的所有路径，包括
`/`、`/api/`、`/photos/`、`/health` 和 upload，全部要求 Admin Basic Auth。

PushService 的唯一公开图片基地址必须是：

```text
H5_BASE_URL=https://menu.ourmenu.site
https://menu.ourmenu.site/photos/<dishes.image>
```

不得使用 `admin.ourmenu.site`、localhost 或 GitHub Raw。

## 10. 阶段 A：公网 IP 的 HTTP 基础测试

安装 bootstrap 配置：

```bash
sudo install -m 0644 \
  /tmp/family-menu-deploy/nginx-family-menu-bootstrap.conf.example \
  /etc/nginx/sites-available/family-menu.conf
sudo ln -sfn /etc/nginx/sites-available/family-menu.conf /etc/nginx/sites-enabled/family-menu.conf
sudo rm -f /etc/nginx/sites-enabled/default
sudo nginx -t
sudo systemctl reload nginx
```

通过 `http://43.129.246.80/` 做 Family Basic Auth、H5、API 和匿名只读图片的基础测试。
IP 无法稳定区分两个 hostname，因此阶段 A 不把 Admin 暴露在额外路径或端口；Admin 只在服务器本机
通过 `curl http://127.0.0.1:8080/health` 验证。完整 Admin 浏览器测试使用 `admin.ourmenu.site`。
全阶段保持 `APP_ENV=development`、`PUSH_ENABLED=false`，不要启用 reminder timer。

```bash
curl -I http://43.129.246.80/                         # 未认证应为 401
curl -u FAMILY_USERNAME -I http://43.129.246.80/      # 应为 200
curl -I http://43.129.246.80/photos/REAL_IMAGE.jpg    # 匿名应为 200
curl -I http://43.129.246.80/api/dishes               # 未认证应为 401
curl -fsS http://127.0.0.1:8080/health
```

## 11. 阶段 B：域名、DNS 与 HTTPS

1. 购买或准备基础域名。
2. 建立 `menu.ourmenu.site` A 记录指向 Lighthouse 香港公网 IP。
3. 建立 `admin.ourmenu.site` A 记录指向同一个公网 IP；不需要第二台服务器。
4. 等待 DNS 生效：`dig +short menu.ourmenu.site` 和 `dig +short admin.ourmenu.site`。
5. 临时把 bootstrap 配置的 `server_name _;` 改成两个 hostname 并 reload，确保 ACME challenge 可达。
6. 确认 80 端口和 `/.well-known/acme-challenge/` 可访问。
7. 申请一张覆盖两个 hostname 的 SAN 证书：

```bash
sudo certbot certonly --webroot -w /var/www/html \
  -d menu.ourmenu.site -d admin.ourmenu.site
sudo certbot renew --dry-run
```

也可以分别申请两张证书，但随后必须分别修改两个 HTTPS server block 的证书路径；默认模板采用
一张 SAN 证书，证书目录名为第一个 hostname `menu.ourmenu.site`。

将 [nginx-family-menu.conf.example](deploy/nginx-family-menu.conf.example) 中的 `menu.ourmenu.site` 和
`admin.ourmenu.site` 两个真实 hostname，安装为 `/etc/nginx/sites-available/family-menu.conf`：

```bash
sudo nginx -t
sudo systemctl reload nginx
curl -I https://menu.ourmenu.site/
curl -I https://admin.ourmenu.site/
curl -I https://menu.ourmenu.site/photos/REAL_IMAGE.jpg
```

确认 HTTP 自动跳转 HTTPS、SAN 证书链正常、Family/Admin hostname 分层认证正常后，再把
`/etc/family-menu.env` 改为 production + HTTPS URL，但仍保持 `PUSH_ENABLED=false`：

```bash
sudo systemctl restart family-menu-app family-menu-admin
sudo journalctl -u family-menu-app -u family-menu-admin -n 50 --no-pager
```

HSTS 在首次部署不启用。稳定运行并确认不会回退 HTTP 后，再取消 Nginx 模板中 HSTS 的注释。

## 12. Reminder timers：只保留 19:00 与 20:00

GitHub Actions 的 daily push、19:00、20:00 和 21:00 正式 schedule 均已停用；workflow 文件仅保留
手动历史入口。生产 reminder 改为 systemd timer。

```bash
for name in family-menu-reminder-19 family-menu-reminder-20; do
  sudo install -m 0644 /tmp/family-menu-deploy/$name.service.example /etc/systemd/system/$name.service
  sudo install -m 0644 /tmp/family-menu-deploy/$name.timer.example /etc/systemd/system/$name.timer
done
sudo systemd-analyze verify /etc/systemd/system/family-menu-reminder-*.service \
  /etc/systemd/system/family-menu-reminder-*.timer
sudo systemctl daemon-reload
systemd-analyze calendar '*-*-* 19:00:00 Asia/Hong_Kong'
systemd-analyze calendar '*-*-* 20:00:00 Asia/Hong_Kong'
```

在正式 production env、数据库和 PushPlus 凭证都完成前，不要 enable timer。先在
`PUSH_ENABLED=false` 下验证脚本读取正确 SQLite 状态且没有真实发送，再启用：

```bash
sudo systemctl enable --now family-menu-reminder-19.timer family-menu-reminder-20.timer
systemctl list-timers 'family-menu-reminder-*'
```

不要创建 10:30 菜单 push 或 21:00 VV reminder timer。

## 13. Confirm Push 的唯一链路

```text
VV Confirm
→ menus.confirmed_at + confirmed_revision
→ PushService
→ PushPlus
→ push_logs success/failed/disabled
```

不存在第二个定时菜单 push。相同 revision 的 success 会防重复；修改后重新 Confirm 产生新 revision。

第一次真实发送前：

1. 保持 `PUSH_ENABLED=false`。
2. 使用 `push_menu.py --menu-id ID --preview /tmp/menu-preview.html`。
3. 检查日期、location、Breakfast、Lunch、Dinner、中英文名和所有 HTTPS 图片。
4. Confirm 测试应保存 confirmed，但产生 `push_status=disabled`，不能 success。
5. 确认 `push_logs` 没有意外 success。
6. 将 `APP_ENV=production`、`H5_BASE_URL=https://menu.ourmenu.site` 保持不变，仅把 `PUSH_ENABLED=true`。
7. 重启 app/reminder 相关服务。
8. 只对一份可控菜单执行一次 Push，确认 PushPlus 实际收到且 `push_logs=success`。

## 14. Backup timers

数据库每日 03:15 使用 SQLite backup API；photos 每周日 03:45 生成 tar.gz：

```bash
for name in family-menu-backup family-menu-photos-backup; do
  sudo install -m 0644 /tmp/family-menu-deploy/$name.service.example /etc/systemd/system/$name.service
  sudo install -m 0644 /tmp/family-menu-deploy/$name.timer.example /etc/systemd/system/$name.timer
done
sudo systemd-analyze verify /etc/systemd/system/family-menu-*backup*.service \
  /etc/systemd/system/family-menu-*backup*.timer
sudo systemctl daemon-reload

# 手动验证，不覆盖生产 DB
sudo systemctl start family-menu-backup.service
sudo systemctl start family-menu-photos-backup.service
ls -lh /opt/family-menu/backups
sqlite3 /opt/family-menu/backups/family_menu_YYYYMMDD_HHMMSS.db 'PRAGMA quick_check;'

sudo systemctl enable --now family-menu-backup.timer family-menu-photos-backup.timer
systemctl list-timers 'family-menu-*backup*'
```

`BACKUP_RETENTION=14` 控制同类本地备份保留数量。第一阶段
`BACKUP_REMOTE_ENABLED=false` 不阻塞上线。后续启用腾讯云 COS 时，使用实例角色或外部工具读取的
环境凭证，并把上传命令放在 `BACKUP_REMOTE_COMMAND`；不得把 SecretId/SecretKey 写入仓库。

### Restore 演练

恢复测试只能到新路径：

```bash
sudo -u family-menu python3 - <<'PY'
from backup_data import restore_database, quick_check
src = '/opt/family-menu/backups/family_menu_YYYYMMDD_HHMMSS.db'
dst = '/opt/family-menu/backups/restore-test.db'
restore_database(src, dst)
print(quick_check(dst))
PY
```

确认 `ok` 和核心表 count 后删除 restore-test；不要直接覆盖生产 DB。

## 15. 日志与 logrotate

- app/admin：systemd journal。
- reminder：`/opt/family-menu/logs/reminder.log`。
- backup：`/opt/family-menu/logs/backup.log`。
- Push 结果：SQLite `push_logs`，错误已做 token 脱敏。

安装 logrotate 模板：

```bash
sudo install -m 0644 /tmp/family-menu-deploy/family-menu-logrotate.example \
  /etc/logrotate.d/family-menu
sudo logrotate -d /etc/logrotate.d/family-menu
sudo journalctl --vacuum-time=30d
```

## 16. 图片现状与策略

上线前只读审计基线：202 active，196 active 有图片，6 active 无图；7 个缺文件引用全部属于
inactive 菜；6 个 orphan 图片。不要自动清理。

- Active 无图继续使用现有 placeholder/graceful fallback。
- Inactive 历史图片缺失时允许 placeholder，不得导致 History 整页失败。
- Orphan 图片先保留，待人工核对后处理。

## 17. 上线前功能清单

每项记录时间、账号、浏览器、结果和截图：

- [ ] 未认证 `https://menu.ourmenu.site/` 返回 401；Family 凭证返回 200。
- [ ] 未认证 `https://menu.ourmenu.site/api/dishes` 返回 401。
- [ ] 匿名 `https://menu.ourmenu.site/photos/<real>` 返回 200；目录浏览关闭。
- [ ] `/photos/` 对 POST/PUT/DELETE 拒绝，缺失文件和 traversal 返回 404。
- [ ] 未认证 `https://admin.ourmenu.site/` 返回 401；Admin 凭证返回 200。
- [ ] Admin `/api/`、`/photos/`、upload、CRUD 均正常且始终要求 Admin Auth。
- [ ] Family 凭证不能进入 Admin；Admin 密码不作为 Family 公共账号使用。
- [ ] H5 首页、Tomorrow、Pantry、Dishes、History。
- [ ] Diners 变化及人数重算。
- [ ] Banquet mode 与人数。
- [ ] AI Fill、Replace、Refresh AI。
- [ ] 历史高频回归：3 人 Dinner Missing Protein 1 → AI Fill 后必须为 2/2。
- [ ] Admin list/search/add/edit/upload/replace photo/Soft Delete。
- [ ] Soft Delete 保留 image 与文件，History 仍可显示。
- [ ] `/photos/<real>` 200；不存在和 traversal 路径 404。
- [ ] localhost App/Admin `/health` 200。
- [ ] `menu.ourmenu.site/health` 受 Family Auth；`admin.ourmenu.site/health` 受 Admin Auth。
- [ ] SQLite backup、photos archive、restore-test quick_check。
- [ ] 19:00/20:00 reminder 读取真实 SQLite；无 10:30/21:00 timer。
- [ ] `PUSH_ENABLED=false` 时 Confirm 保存但不真实发送。
- [ ] Preview 中所有 HTTPS 图片可加载。
- [ ] Preview 中所有 `https://menu.ourmenu.site/photos/...` 均可匿名 GET 200。
- [ ] 首次可控 PushPlus success 与 push_logs success 对齐。

## 18. 深圳与香港访问验收

上线后分别使用：

- 深圳家庭 Wi-Fi。
- 深圳手机 5G。
- 香港网络。

测试首页、图片、AI Fill、Confirm、Admin，并记录首屏、图片和操作的主观延迟。先收集真实体验，
不要在数据不足时购买跨境加速产品。

## 19. 更新发布流程

每次更新前：

```bash
sudo systemctl start family-menu-backup.service
sudo cp -a /opt/family-menu/app /opt/family-menu/backups/app-release-$(date +%Y%m%d_%H%M%S)
```

上传代码到临时目录，核对 diff，再原子替换/rsync 到 app；不要覆盖 data/photos/env：

```bash
sudo rsync -a --delete /tmp/family-menu-app/ /opt/family-menu/app/
sudo chown -R root:family-menu /opt/family-menu/app
sudo systemctl restart family-menu-app family-menu-admin
curl -fsS http://127.0.0.1:8090/health
curl -fsS http://127.0.0.1:8080/health
```

## 20. 完整回滚

### 立即止损

```bash
sudo sed -i 's/^PUSH_ENABLED=.*/PUSH_ENABLED=false/' /etc/family-menu.env
sudo systemctl disable --now family-menu-reminder-19.timer family-menu-reminder-20.timer
sudo systemctl restart family-menu-app family-menu-admin
```

### 代码回滚

停止两个服务，把最近一次 `app-release-*` 恢复到 `/opt/family-menu/app`，重新设置
`root:family-menu` 权限，启动后先检查 localhost health。

### DB 回滚

1. 停止 app/admin/reminder timers。
2. 再备份当前故障 DB，保留取证。
3. 选择已通过 quick_check 的目标备份。
4. 使用 `restore_database()` 恢复到一个新文件。
5. 核对核心表 count。
6. 原子重命名新文件为 `/opt/family-menu/data/family_menu.db`。
7. 设置 `family-menu:family-menu 0640`，启动服务并检查 health。

不要在服务运行时用 `cp` 覆盖 SQLite。

### Photos 恢复

先把现有 photos 移到带时间戳的隔离目录，再将选定 `photos_*.tar.gz` 解压到新目录；核对权限和
`audit_images.py` 后再启动 Admin。不要先删除现有目录。

### Nginx 回滚

恢复上一份已通过 `nginx -t` 的站点配置，reload；若 TLS 异常，保持 Push/timer 关闭，不能通过
降低 production HTTPS 校验绕过。

### 回到当前 Mac localhost

腾讯云异常时保持云端 `PUSH_ENABLED=false` 且 reminder timers 停止；在 Mac 项目目录以
`APP_ENV=development PUSH_ENABLED=false HOST=127.0.0.1` 启动当前 app/admin。Mac 仅作为临时本地回退，
不会自动成为公网生产服务。恢复前明确哪一份 SQLite 是最新真相源，避免双写。

## 21. 上线完成判定

只有以下全部满足才进入家庭试用：

- 双 hostname HTTPS、Basic Auth 隔离、Family 公开只读图片与 localhost 端口隔离通过。
- 全部功能清单通过，尤其 3 人 Dinner 2/2 回归。
- DB/photos backup 和 restore 演练通过。
- Preview 与 HTTPS 图片通过。
- 一次可控真实 PushPlus 成功且日志一致。
- 深圳 Wi-Fi、深圳 5G、香港网络完成体验记录。

在此之前保持 `PUSH_ENABLED=false`，不启用 reminder timers。
