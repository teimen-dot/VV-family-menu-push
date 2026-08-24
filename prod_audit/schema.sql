CREATE TABLE categories (
            id          TEXT PRIMARY KEY,
            label_cn    TEXT NOT NULL,
            label_en    TEXT,
            sort_order  INTEGER DEFAULT 0,
            active      INTEGER DEFAULT 1
        );
CREATE TABLE dishes (
            id                      TEXT PRIMARY KEY,
            name_cn                 TEXT NOT NULL,
            name_en                 TEXT,
            category_id             TEXT,
            meal_tags               TEXT DEFAULT '[]',
            banquet                 INTEGER DEFAULT 0,
            protein_types           TEXT DEFAULT '[]',
            vegetables              TEXT DEFAULT '[]',
            vegetable_count         INTEGER DEFAULT 0,
            carb_type               TEXT,
            breakfast_staple_type   TEXT,
            meal_components         TEXT DEFAULT '[]',
            taste                   TEXT,
            cooking_methods         TEXT DEFAULT '[]',
            can_serve_warm          INTEGER DEFAULT 0,
            custom_tags             TEXT DEFAULT '[]',
            allergens               TEXT DEFAULT '[]',
            dietary_tags            TEXT DEFAULT '[]',
            image                   TEXT,
            image_uploaded          INTEGER DEFAULT 0,
            needs_review            INTEGER DEFAULT 0,
            old_category            TEXT,
            old_tags                TEXT DEFAULT '[]',
            created_at              TEXT DEFAULT (datetime('now')),
            updated_at              TEXT DEFAULT (datetime('now')), quick_soup INTEGER DEFAULT 0, slow_soup INTEGER DEFAULT 0, manual_only_for_breakfast INTEGER DEFAULT 0, is_active INTEGER DEFAULT 1, deleted_at TEXT, meal_roles TEXT DEFAULT '[]',
            FOREIGN KEY (category_id) REFERENCES categories(id)
        );
CREATE TABLE ingredients (
            ingredient_id   TEXT PRIMARY KEY,
            name_cn         TEXT NOT NULL,
            name_en         TEXT,
            aliases         TEXT DEFAULT '[]',
            category        TEXT,
            created_at      TEXT DEFAULT (datetime('now'))
        , ingredient_group TEXT DEFAULT "main", is_common INTEGER DEFAULT 0, translation_pending INTEGER DEFAULT 0, updated_at TEXT);
CREATE TABLE dish_ingredients (
            id              INTEGER PRIMARY KEY AUTOINCREMENT,
            dish_id         TEXT NOT NULL,
            ingredient_id   TEXT NOT NULL,
            required        INTEGER DEFAULT 1,
            FOREIGN KEY (dish_id) REFERENCES dishes(id),
            FOREIGN KEY (ingredient_id) REFERENCES ingredients(ingredient_id),
            UNIQUE(dish_id, ingredient_id)
        );
CREATE TABLE sqlite_sequence(name,seq);
CREATE TABLE custom_tags_def (
            id      INTEGER PRIMARY KEY AUTOINCREMENT,
            label   TEXT NOT NULL UNIQUE
        );
CREATE TABLE inventory (
            id              INTEGER PRIMARY KEY AUTOINCREMENT,
            location        TEXT NOT NULL,
            date            TEXT NOT NULL,
            submitted_by    TEXT,
            submitted_at    TEXT,
            status          TEXT DEFAULT 'pending',
            notes           TEXT,
            created_at      TEXT DEFAULT (datetime('now')),
            UNIQUE(location, date)
        );
CREATE TABLE inventory_items (
            id              INTEGER PRIMARY KEY AUTOINCREMENT,
            inventory_id    INTEGER NOT NULL,
            ingredient_id   TEXT NOT NULL,
            status          TEXT DEFAULT 'available',
            notes           TEXT, quantity_level TEXT DEFAULT 'enough',
            FOREIGN KEY (inventory_id) REFERENCES inventory(id),
            FOREIGN KEY (ingredient_id) REFERENCES ingredients(ingredient_id)
        );
CREATE TABLE menus (
            id              INTEGER PRIMARY KEY AUTOINCREMENT,
            date            TEXT NOT NULL UNIQUE,
            location        TEXT NOT NULL,
            status          TEXT DEFAULT 'draft',
            auto_confirmed  INTEGER DEFAULT 0,
            confirmed_at    TEXT,
            pushed_at       TEXT,
            diners_count    INTEGER DEFAULT 4,
            diners          TEXT DEFAULT '[]',
            notes_zh        TEXT,
            notes_en        TEXT,
            created_at      TEXT DEFAULT (datetime('now')),
            updated_at      TEXT DEFAULT (datetime('now'))
        , inventory_snapshot_id INTEGER, meal_mode TEXT DEFAULT 'daily', banquet_total_diners INTEGER, push_status TEXT DEFAULT 'not_sent', push_error TEXT, confirmed_revision TEXT, meal_notes TEXT DEFAULT '{}');
CREATE TABLE menu_items (
            id              INTEGER PRIMARY KEY AUTOINCREMENT,
            menu_id         INTEGER NOT NULL,
            dish_id         TEXT NOT NULL,
            meal_type       TEXT NOT NULL,
            is_locked       INTEGER DEFAULT 0,
            locked_by       TEXT,
            locked_at       TEXT,
            sort_order      INTEGER DEFAULT 0, custom_name TEXT, source TEXT DEFAULT 'ai',
            FOREIGN KEY (menu_id) REFERENCES menus(id)
        );
CREATE TABLE selections (
            id                  INTEGER PRIMARY KEY AUTOINCREMENT,
            menu_id             INTEGER NOT NULL,
            dish_id             TEXT NOT NULL,
            meal_type           TEXT,
            selected_by         TEXT,
            selected_at         TEXT DEFAULT (datetime('now')),
            shortage_handled    INTEGER DEFAULT 0,
            purchase_approved   INTEGER DEFAULT 0,
            FOREIGN KEY (menu_id) REFERENCES menus(id)
        );
CREATE TABLE purchase_requests (
            id              INTEGER PRIMARY KEY AUTOINCREMENT,
            menu_date       TEXT NOT NULL,
            location        TEXT NOT NULL,
            dish_id         TEXT,
            ingredient_id   TEXT NOT NULL,
            status          TEXT DEFAULT 'needed',
            created_at      TEXT DEFAULT (datetime('now')),
            notified_at     TEXT,
            resolved_at     TEXT,
            resolved_by     TEXT,
            notes           TEXT,
            FOREIGN KEY (ingredient_id) REFERENCES ingredients(ingredient_id)
        );
CREATE TABLE diners (
            id              TEXT PRIMARY KEY,
            name_cn         TEXT NOT NULL,
            name_en         TEXT,
            role            TEXT,
            default_attends INTEGER DEFAULT 1,
            sort_order      INTEGER DEFAULT 0
        );
CREATE TABLE dietary_alerts (
            id          INTEGER PRIMARY KEY AUTOINCREMENT,
            diner_id    TEXT NOT NULL,
            allergen    TEXT NOT NULL,
            severity    TEXT DEFAULT 'avoid',
            notes       TEXT,
            FOREIGN KEY (diner_id) REFERENCES diners(id)
        );
CREATE TABLE events (
            id          INTEGER PRIMARY KEY AUTOINCREMENT,
            event_type  TEXT NOT NULL,
            entity_type TEXT,
            entity_id   TEXT,
            details     TEXT,
            created_at  TEXT DEFAULT (datetime('now'))
        );
CREATE TABLE config (
            key     TEXT PRIMARY KEY,
            value   TEXT,
            notes   TEXT
        );
CREATE TABLE current_pantry (
            id              INTEGER PRIMARY KEY AUTOINCREMENT,
            location        TEXT NOT NULL,
            ingredient_id   TEXT NOT NULL,
            status          TEXT DEFAULT 'available',
            is_active       INTEGER DEFAULT 1,
            created_at      TEXT DEFAULT (datetime('now')),
            updated_at      TEXT DEFAULT (datetime('now')), quantity_level TEXT DEFAULT 'enough',
            UNIQUE(location, ingredient_id)
        );
CREATE TABLE inventory_snapshots (
            id              INTEGER PRIMARY KEY AUTOINCREMENT,
            location        TEXT NOT NULL,
            items_json      TEXT,
            created_at      TEXT DEFAULT (datetime('now'))
        );
CREATE TABLE dishes_legacy(
  id TEXT,
  name_cn TEXT,
  name_en TEXT,
  category_id TEXT,
  meal_tags TEXT,
  banquet INT,
  protein_types TEXT,
  vegetables TEXT,
  vegetable_count INT,
  carb_type TEXT,
  breakfast_staple_type TEXT,
  meal_components TEXT,
  taste TEXT,
  cooking_methods TEXT,
  can_serve_warm INT,
  custom_tags TEXT,
  allergens TEXT,
  dietary_tags TEXT,
  image TEXT,
  image_uploaded INT,
  needs_review INT,
  old_category TEXT,
  old_tags TEXT,
  created_at TEXT,
  updated_at TEXT,
  quick_soup INT,
  slow_soup INT,
  manual_only_for_breakfast INT,
  is_active INT,
  deleted_at TEXT
);
CREATE TABLE dish_preference_stats (
            dish_id                 TEXT PRIMARY KEY,
            vv_confirm_count        INTEGER DEFAULT 0,
            vv_confirm_count_30d    INTEGER DEFAULT 0,
            last_confirmed_at       TEXT,
            last_selected_at        TEXT,
            FOREIGN KEY (dish_id) REFERENCES dishes(id)
        );
CREATE TABLE push_logs (
            id              INTEGER PRIMARY KEY AUTOINCREMENT,
            menu_id         INTEGER NOT NULL,
            menu_revision   TEXT NOT NULL,
            date            TEXT NOT NULL,
            location        TEXT NOT NULL,
            channel         TEXT NOT NULL DEFAULT 'pushplus',
            status          TEXT NOT NULL,
            pushed_at       TEXT,
            error           TEXT,
            created_at      TEXT DEFAULT (datetime('now')),
            updated_at      TEXT DEFAULT (datetime('now')), confirmed_at TEXT, triggered_by TEXT, push_requested_at TEXT, message_id TEXT, response_code INTEGER, attempt_count INTEGER DEFAULT 0,
            FOREIGN KEY (menu_id) REFERENCES menus(id),
            UNIQUE(menu_id, menu_revision, channel)
        );
CREATE TABLE consumed_history (id INTEGER PRIMARY KEY AUTOINCREMENT, location TEXT NOT NULL, ingredient_id TEXT NOT NULL, consumed_at TEXT NOT NULL, consumed_by TEXT NOT NULL DEFAULT 'owner', source TEXT NOT NULL DEFAULT 'pantry_used_up', UNIQUE(location, ingredient_id, consumed_at));
CREATE INDEX idx_consumed_history_location_date ON consumed_history(location, consumed_at DESC);
CREATE TABLE pantry_usage_stats (
            location        TEXT NOT NULL,
            ingredient_id   TEXT NOT NULL,
            add_count       INTEGER NOT NULL DEFAULT 0,
            last_action_at  TEXT NOT NULL,
            PRIMARY KEY (location, ingredient_id),
            FOREIGN KEY (ingredient_id) REFERENCES ingredients(ingredient_id)
        );
CREATE TABLE menu_meal_settings (
            menu_id         INTEGER NOT NULL,
            meal_type       TEXT NOT NULL,
            diners          TEXT,
            note            TEXT DEFAULT '',
            is_skipped      INTEGER DEFAULT 0,
            updated_at      TEXT DEFAULT (datetime('now')),
            PRIMARY KEY (menu_id, meal_type),
            FOREIGN KEY (menu_id) REFERENCES menus(id)
        );
CREATE TABLE menu_item_replace_history (
            id              INTEGER PRIMARY KEY AUTOINCREMENT,
            menu_id         INTEGER NOT NULL,
            menu_item_id    INTEGER NOT NULL,
            dish_id         TEXT NOT NULL,
            replaced_at     TEXT DEFAULT (datetime('now')),
            FOREIGN KEY (menu_id) REFERENCES menus(id)
        );
CREATE INDEX idx_replace_history_item ON menu_item_replace_history(menu_id,menu_item_id,id DESC);
