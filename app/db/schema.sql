-- Prices are in Kina; timestamps are UTC ISO strings (millisecond precision, 'Z').
-- The API's 9-digit item_key is the canonical item id.

CREATE TABLE IF NOT EXISTS servers (
  id INTEGER PRIMARY KEY,                -- same value as api_server_id
  api_server_id INTEGER UNIQUE,          -- /api/servers server_id
  name TEXT, region TEXT, faction TEXT,
  is_mine INTEGER DEFAULT 0,
  is_cross_server INTEGER DEFAULT 0      -- 1 once the cross-server market opens
);

CREATE TABLE IF NOT EXISTS items (
  item_key INTEGER PRIMARY KEY,          -- API id (9 digits)
  name TEXT, section TEXT, category_id INTEGER, category_name TEXT,
  watch INTEGER DEFAULT 0, hold_qty INTEGER DEFAULT 0, hold_avg_cost INTEGER,
  notes TEXT
);

CREATE TABLE IF NOT EXISTS item_snapshots (  -- one row per /api/items refresh
  item_key INTEGER, server_id INTEGER, captured_at TEXT,
  price INTEGER, listings INTEGER, change_pct REAL,
  units_sold INTEGER, sold_avg REAL, sold_min REAL, sold_max REAL,
  fetched_at TEXT,
  PRIMARY KEY (item_key, server_id, captured_at)
);

CREATE TABLE IF NOT EXISTS price_points (    -- /prices buckets
  item_key INTEGER, server_id INTEGER, bucket TEXT, ts TEXT,
  low REAL, high REAL, avg REAL, close REAL,
  listings INTEGER, samples INTEGER,
  PRIMARY KEY (item_key, server_id, bucket, ts)
);

CREATE TABLE IF NOT EXISTS sales_days (      -- /volume batch days
  item_key INTEGER, server_id INTEGER, registered TEXT,
  units_sold INTEGER, units_delta INTEGER, days_since_previous INTEGER,
  avg_price REAL, min_price REAL, max_price REAL,
  window_from TEXT, window_to TEXT,
  PRIMARY KEY (item_key, server_id, registered)
);

CREATE TABLE IF NOT EXISTS recipes (
  id INTEGER PRIMARY KEY,
  output_key INTEGER REFERENCES items(item_key), output_qty INTEGER DEFAULT 1,
  success_rate REAL, combo_probability REAL,
  combo_output_key INTEGER, combo_output_qty INTEGER,
  kina_cost INTEGER DEFAULT 0, craft_seconds INTEGER,
  profession TEXT, mastery_level INTEGER,
  rate_source TEXT,                      -- 'manual' | 'api'
  rate_updated_at TEXT, api_recipe_id INTEGER, notes TEXT
);

CREATE TABLE IF NOT EXISTS recipe_inputs (
  recipe_id INTEGER REFERENCES recipes(id),
  item_key INTEGER REFERENCES items(item_key),
  qty INTEGER NOT NULL, npc_price INTEGER,
  PRIMARY KEY (recipe_id, item_key)
);

CREATE TABLE IF NOT EXISTS craft_outcomes (  -- Danny's own crafting results
  id INTEGER PRIMARY KEY, recipe_id INTEGER REFERENCES recipes(id),
  at TEXT, attempts INTEGER, successes INTEGER, combos INTEGER, notes TEXT
);

CREATE TABLE IF NOT EXISTS trades (          -- Danny's own buys and sells
  id INTEGER PRIMARY KEY, item_key INTEGER,
  side TEXT CHECK (side IN ('buy','sell')),
  qty INTEGER, unit_price INTEGER, fee_paid INTEGER,
  listed_at TEXT, done_at TEXT,          -- done_at NULL = still listed
  notes TEXT
);

CREATE TABLE IF NOT EXISTS api_cache (url TEXT PRIMARY KEY, body TEXT, fetched_at TEXT, status INTEGER);
CREATE TABLE IF NOT EXISTS fetch_log (id INTEGER PRIMARY KEY, at TEXT, endpoint TEXT, status INTEGER, ms INTEGER, error TEXT);
CREATE TABLE IF NOT EXISTS settings (key TEXT PRIMARY KEY, value TEXT);

CREATE INDEX IF NOT EXISTS idx_snapshots_server_time ON item_snapshots (server_id, captured_at);

-- Milestone 4: farming runs and event tags
CREATE TABLE IF NOT EXISTS farm_runs (
  id INTEGER PRIMARY KEY, route TEXT, at TEXT, minutes REAL, kina INTEGER DEFAULT 0, notes TEXT
);
CREATE TABLE IF NOT EXISTS farm_drops (
  run_id INTEGER REFERENCES farm_runs(id), item_key INTEGER, qty INTEGER,
  PRIMARY KEY (run_id, item_key)
);
CREATE TABLE IF NOT EXISTS events (id INTEGER PRIMARY KEY, day TEXT, label TEXT, kind TEXT);

-- Real item icons from NCSOFT's item dictionary; file is NULL when none was found.
CREATE TABLE IF NOT EXISTS item_icons (
  item_key INTEGER PRIMARY KEY, file TEXT, grade TEXT, checked_at TEXT
);