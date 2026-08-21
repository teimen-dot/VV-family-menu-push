# Family Menu Production Recovery

## Stable release

- Repository: `https://github.com/teimen-dot/VV-family-menu-push.git`
- Branch: `codex/family-ui-phase2-writable`
- Production code commit: `df5f6de`
- Stable tag: `production-2026-08-21-diners-matrix`
- Production host: `43.129.246.80` (Ubuntu)

The Git repository contains source code, migrations, deployment examples, and
versioned dish photos. It intentionally does not contain the production SQLite
database, real environment file, passwords, API tokens, htpasswd files, or SSH
private keys.

## Production layout

| Item | Location |
| --- | --- |
| Application | `/opt/family-menu/app/` |
| SQLite database | `/opt/family-menu/data/family_menu.db` |
| Uploaded photos | `/opt/family-menu/photos/` |
| Server backups | `/opt/family-menu/backups/` |
| Environment file | `/etc/family-menu.env` (`root:root`, mode `0600`) |
| Family service | `family-menu-app.service` |
| Admin service | `family-menu-admin.service` |
| Reverse proxy | `nginx.service` |

The independent workstation copy of the production snapshot is stored outside
Git at `/Users/heymen/Documents/family-menu-backups/production-20260820-stable/`.
Use its `SHA256SUMS` file to verify every restored archive before extraction.

The pre-deployment server snapshot for this release is stored at
`/opt/family-menu/backups/predeploy-20260821-diners-matrix/`. It contains the
SQLite database plus a runtime archive of application code, uploaded photos,
systemd units, and Nginx configuration. Both files are covered by the adjacent
`SHA256SUMS` file and were verified before deployment.

## Last verified recovery drill

PASS on 2026-08-20 HKT using a new empty directory:

- GitHub tag resolved to production commit `ab170a2`.
- All 304 Git-tracked files matched the deployed application snapshot; no
  required production code was missing from GitHub.
- SQLite `PRAGMA integrity_check` returned `ok`; restored data contained 229
  dish rows (218 active), 90 pantry rows, 25 menus, and 290 menu items.
- All 218 uploaded photo files were restored.
- The isolated application started with push disabled; `/health` returned
  application/database OK, `/tomorrow` returned HTTP 200 after simulated trusted
  proxy authentication, and the real bootstrap payload loaded successfully.
- Independent snapshot SHA-256:
  `7b6e9552a0add52dbae464e067ad247bbceb919a51de00c7bf7036d5b3a7ab3e`.

## Required secrets and configuration

Recreate these values from the owner's password manager or protected server
backup; never commit their real values:

- `APP_ENV`, `FAMILY_MENU_DB_PATH`, `PHOTO_DIR`, `HOST`, `PORT`
- `H5_BASE_URL`, `SESSION_SECRET`
- `OWNER_AUTH_USERNAME`, `WORKER_AUTH_USERNAME`
- `PUSH_ENABLED`, `PUSH_SCHEDULE_ENABLED`, `PUSH_ON_CONFIRM`
- Push provider token/topic variables, if push is enabled later
- Family/Admin htpasswd files and TLS certificate material
- SSH deployment private key

The known safe production defaults at this release are `APP_ENV=production`,
`PUSH_ENABLED=false`, and `PUSH_SCHEDULE_ENABLED=false`.

## Restore to a new machine

```bash
git clone https://github.com/teimen-dot/VV-family-menu-push.git family-menu
cd family-menu
git checkout production-2026-08-21-diners-matrix
git rev-parse HEAD
```

The stable tag contains this recovery document and has `df5f6de` as its
production application parent commit.

Verify and unpack the independent snapshot, then place its contents using the
production layout above. Before starting the application, ensure the database
and photo directory are readable by the service account and the environment
file remains root-only. Do not run a destructive migration against the only
copy of the database.

For a local recovery smoke test, use an extracted database and photo directory:

```bash
export APP_ENV=development
export FAMILY_MENU_DB_PATH=/absolute/path/to/restored/family_menu.db
export PHOTO_DIR=/absolute/path/to/restored/photos
export HOST=127.0.0.1
export PORT=18766
python3 app.py
```

In another terminal verify:

```bash
curl -fsS http://127.0.0.1:18766/health
curl -fsS -o /dev/null -w '%{http_code}\n' http://127.0.0.1:18766/tomorrow
```

For production recovery, install the backed-up systemd and Nginx configuration,
then run:

```bash
sudo systemctl daemon-reload
sudo systemctl restart family-menu-app family-menu-admin nginx
sudo systemctl is-active family-menu-app family-menu-admin nginx
curl -fsS https://menu.ourmenu.site/health
```

Acceptance requires: health reports both application and database OK;
`/tomorrow` opens after authentication; current menus, diners counts, pantry,
dishes, and uploaded photos are present; and service logs contain no new startup
errors.

## Backup rules

- Keep code history in GitHub and runtime snapshots outside GitHub.
- A runtime snapshot must include code, SQLite (including a consistent WAL
  checkpoint/copy), uploaded photos, migrations, environment/configuration,
  systemd units, and Nginx configuration.
- Record SHA-256 for every archive and verify after copying off the server.
- Never put passwords, tokens, htpasswd contents, environment secrets, TLS
  private keys, or SSH private keys in GitHub.
- A backup is accepted only after the empty-directory clone and restored-data
  startup drill passes.
