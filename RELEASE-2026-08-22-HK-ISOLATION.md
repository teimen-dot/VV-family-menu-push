# Hong Kong Menu Isolation Release

## Go / No-Go

- Decision: **GO**
- Production application commit: `3fe120b`
- Stable tag: `production-2026-08-22-hongkong-isolation`
- Rollback snapshot: `/opt/family-menu/backups/predeploy-20260822-hk-isolation-3fe120b/`

## Evidence

- 78 targeted unit/integration/regression tests passed.
- Formal production-copy rehearsal passed forward migration and exact snapshot rollback.
- Migration changed `UNIQUE(date)` to `UNIQUE(date, location)` only.
- Protected Shenzhen/shared-data fingerprints matched before and after migration.
- Existing production Shenzhen menus diff: 0 rows.
- Existing production Shenzhen menu items diff: 0 rows.
- Production Shenzhen pantry diff: 0 rows.
- Hong Kong menus created for 2026-08-22 through 2026-08-25, all with 3 diners.
- Re-running the window initializer creates 0 duplicate menus.
- SQLite `quick_check` and `foreign_key_check` passed.
- `family-menu-app`, `family-menu-admin`, and `nginx` are active.
- `/health` reports application and database OK.

## Rollback

Stop both write services, restore `family_menu.db` and `app.tar.gz` from the
rollback snapshot, restart services, then re-run health, SQLite integrity, and
Shenzhen menu checks. Any protected Shenzhen data difference is an immediate
rollback trigger.
