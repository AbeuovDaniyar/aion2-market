"""SQLite access. One Database per thread: the refresh worker and the UI each open their own."""

from __future__ import annotations

import os
import sqlite3
import sys
from collections.abc import Iterable
from datetime import UTC, datetime
from pathlib import Path

from app.external import models as m

SCHEMA_PATH = Path(__file__).with_name("schema.sql")


def _default_db_path() -> Path:
    """The repo's data folder when run from source; the user's AppData folder when run as
    the packaged exe (which unpacks to a temporary folder on every start)."""
    if getattr(sys, "frozen", False):
        base = Path(os.environ.get("LOCALAPPDATA") or Path.home()) / "AION2 Market Assistant"
        return base / "market.db"
    return Path(__file__).resolve().parents[2] / "data" / "market.db"


DEFAULT_DB_PATH = _default_db_path()


def icons_dir(db_path: str | Path) -> Path:
    """Downloaded item icons live next to the database."""
    return Path(db_path).parent / "icons"

# Defaults for the design doc's open questions; all editable in Settings.
DEFAULT_SETTINGS: dict[str, str] = {
    "server_id": "",  # empty = the API's default server
    "sales_tax_pct": "10",
    "listing_fee_rule": "none",  # 'none' | 'flat' | 'percent'
    "listing_fee_value": "0",
    "has_buy_orders": "0",
    "refresh_minutes": "10",
    "horizon_days": "3",
    "stale_listings_hours": "2",
    "stale_sales_days": "2",
    "collector_failing_runs": "3",
    "sales_refresh_hours": "6",
    # Pricing model
    "undercut_step": "1",  # Kina below the cheapest seller
    "min_sales_units": "10",  # sales needed before the sale average anchors the estimate
    "sales_window_days": "28",
    "spike_mads": "3",
    "spike_persist_refreshes": "3",
    "depth_units": "10",
    # Flip finder
    "flip_min_profit": "5000",
    "flip_min_roi_pct": "8",
    "flip_min_days": "3",
    "flip_min_listings": "5",
    # Reference servers (same region as mine)
    "reference_scope": "region_faction",  # 'region_faction' | 'region' | 'off'
    "reference_refresh_hours": "6",
    # Established servers with sales statistics, as a demand reference (comma separated ids).
    "demand_reference_servers": "11304",
    "region_gap_pct": "15",  # how far from the regional median counts as cheap/expensive
    # Trust
    "sanity_jump_factor": "5",
    "sanity_drop_factor": "0.2",
    "streak_risk": "0.05",  # the 1-in-20 bad streak
    "theme": "dark",  # 'dark' | 'light' | 'system'; applies at the next start
    "real_item_icons": "1",  # fetch the game's item icons from NCSOFT's item dictionary
    # Alerts
    "flip_alerts": "all",  # 'all' flips the finder accepts | 'confident' (medium or high) | 'off'
    "history_backfill": "1",  # fetch 7 days of price history once per item, in the background
    "alert_min_profit": "50000",  # toast for a craft above this profit
    "dip_pct": "20",  # watched item this far below its 7-day median
    "squeeze_drop_pct": "40",  # listing count fell this much...
    "squeeze_days": "3",  # ...over this many days while the price rose
    "alert_toasts": "1",
    "alert_sound": "0",
    "trade_gap_pct": "25",  # a trade this far from the API's sale average is a large gap
}


def utcnow() -> datetime:
    return datetime.now(UTC)


def iso(dt: datetime | None) -> str | None:
    """UTC ISO string with fixed millisecond precision, so strings sort by time."""
    if dt is None:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=UTC)
    return dt.astimezone(UTC).isoformat(timespec="milliseconds").replace("+00:00", "Z")


def parse_iso(value: str | None) -> datetime | None:
    if not value:
        return None
    return datetime.fromisoformat(value.replace("Z", "+00:00"))


class Database:
    def __init__(self, path: str | Path = DEFAULT_DB_PATH):
        self.path = str(path)
        if self.path != ":memory:":
            Path(self.path).parent.mkdir(parents=True, exist_ok=True)
        self.conn = sqlite3.connect(self.path, timeout=30)
        self.conn.row_factory = sqlite3.Row
        self.conn.execute("PRAGMA journal_mode=WAL")
        self.conn.execute("PRAGMA foreign_keys=ON")
        self.conn.executescript(SCHEMA_PATH.read_text(encoding="utf-8"))
        columns = {r["name"] for r in self.conn.execute("PRAGMA table_info(items)")}
        if "grade" not in columns:  # added after the first release of the schema
            self.conn.execute("ALTER TABLE items ADD COLUMN grade TEXT")
        self.conn.executemany(
            "INSERT OR IGNORE INTO settings (key, value) VALUES (?, ?)",
            DEFAULT_SETTINGS.items(),
        )
        self.conn.commit()

    def close(self) -> None:
        self.conn.close()

    # -- settings ---------------------------------------------------------

    def get_setting(self, key: str) -> str | None:
        row = self.conn.execute("SELECT value FROM settings WHERE key = ?", (key,)).fetchone()
        return row["value"] if row else DEFAULT_SETTINGS.get(key)

    def get_float(self, key: str) -> float:
        return float(self.get_setting(key) or 0)

    def set_setting(self, key: str, value: object) -> None:
        self.conn.execute(
            "INSERT INTO settings (key, value) VALUES (?, ?) "
            "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
            (key, str(value)),
        )
        self.conn.commit()

    # -- servers ----------------------------------------------------------

    def upsert_servers(self, servers: Iterable[m.ServerInfo]) -> None:
        self.conn.executemany(
            "INSERT INTO servers (id, api_server_id, name, region, faction) VALUES (?, ?, ?, ?, ?) "
            "ON CONFLICT(id) DO UPDATE SET name = excluded.name, region = excluded.region, "
            "faction = excluded.faction",
            [(s.server_id, s.server_id, s.name, s.region, s.faction) for s in servers],
        )
        self.conn.commit()

    def set_my_server(self, server_id: int) -> None:
        self.conn.execute("UPDATE servers SET is_mine = (id = ?)", (server_id,))
        self.conn.commit()

    def servers(self) -> list[sqlite3.Row]:
        return self.conn.execute("SELECT * FROM servers ORDER BY region, name").fetchall()

    # -- items and snapshots ------------------------------------------------

    def save_item_summaries(
        self, server_id: int, items: Iterable[m.ItemSummary], fetched_at: datetime
    ) -> int:
        """Upsert the catalog rows and add one snapshot per item. Returns the item count."""
        items = list(items)
        self.conn.executemany(
            "INSERT INTO items (item_key, name, section, category_id, category_name) "
            "VALUES (?, ?, ?, ?, ?) "
            "ON CONFLICT(item_key) DO UPDATE SET name = COALESCE(excluded.name, items.name), "
            "section = excluded.section, category_id = excluded.category_id, "
            "category_name = excluded.category_name",
            [
                (i.item_key, i.name, i.category.section_id, i.category.id, i.category.name)
                for i in items
            ],
        )
        self.conn.executemany(
            "INSERT OR REPLACE INTO item_snapshots (item_key, server_id, captured_at, price, "
            "listings, change_pct, units_sold, sold_avg, sold_min, sold_max, fetched_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            [
                (
                    i.item_key,
                    server_id,
                    iso(i.captured_at),
                    round(i.price),
                    i.listings,
                    i.change_pct,
                    i.units_sold,
                    i.sold_avg_price,
                    i.sold_min_price,
                    i.sold_max_price,
                    iso(fetched_at),
                )
                for i in items
                if i.price is not None  # not on the market right now: catalog row only
            ],
        )
        self.conn.commit()
        return len(items)

    def latest_snapshots(self, server_id: int, watched_only: bool = False) -> list[sqlite3.Row]:
        """Newest snapshot per item, joined with the catalog."""
        return self.conn.execute(
            "SELECT i.*, s.server_id, s.captured_at, s.price, s.listings, s.change_pct, "
            "s.units_sold, s.sold_avg, s.sold_min, s.sold_max, s.fetched_at "
            "FROM item_snapshots s JOIN items i USING (item_key) "
            "WHERE s.server_id = ? AND s.captured_at = (SELECT MAX(captured_at) FROM item_snapshots "
            "WHERE item_key = s.item_key AND server_id = s.server_id) "
            + ("AND i.watch = 1 " if watched_only else "")
            + "ORDER BY i.name",
            (server_id,),
        ).fetchall()

    def snapshot_history(
        self, server_id: int, since: datetime, item_key: int | None = None
    ) -> list[sqlite3.Row]:
        """Every stored snapshot since a time, oldest first."""
        return self.conn.execute(
            "SELECT item_key, captured_at, price, listings, units_sold, sold_avg FROM item_snapshots "
            "WHERE server_id = ? AND captured_at >= ? "
            + ("AND item_key = ? " if item_key is not None else "")
            + "ORDER BY item_key, captured_at",
            (server_id, iso(since)) + ((item_key,) if item_key is not None else ()),
        ).fetchall()

    def price_point_history(self, server_id: int, since: datetime) -> list[sqlite3.Row]:
        return self.conn.execute(
            "SELECT item_key, ts, close AS price, listings FROM price_points "
            "WHERE server_id = ? AND ts >= ? ORDER BY item_key, ts",
            (server_id, iso(since)),
        ).fetchall()

    def sales_days(self, server_id: int, item_key: int) -> list[sqlite3.Row]:
        return self.conn.execute(
            "SELECT * FROM sales_days WHERE server_id = ? AND item_key = ? ORDER BY registered",
            (server_id, item_key),
        ).fetchall()

    def reference_server_ids(self, my_server: int) -> list[int]:
        """Other servers to compare with, per the reference_scope setting."""
        scope = self.get_setting("reference_scope")
        if scope == "off":
            return []
        rows = self.conn.execute(
            "SELECT o.id FROM servers o JOIN servers mine ON mine.id = ? "
            "WHERE o.id != mine.id AND o.region = mine.region "
            + ("AND o.faction = mine.faction " if scope == "region_faction" else "")
            + "ORDER BY o.id",
            (my_server,),
        )
        return [r["id"] for r in rows]

    def demand_server_ids(self, my_server: int) -> list[int]:
        """Servers used as a demand reference: they have the sales statistics mine lacks."""
        raw = self.get_setting("demand_reference_servers") or ""
        ids = [int(part) for part in raw.replace(";", ",").split(",") if part.strip().isdigit()]
        return [i for i in dict.fromkeys(ids) if i != my_server]

    def set_notes(self, item_key: int, notes: str) -> None:
        self.conn.execute("UPDATE items SET notes = ? WHERE item_key = ?", (notes, item_key))
        self.conn.commit()

    def add_trade(
        self, item_key: int, side: str, qty: int, unit_price: int, at: datetime,
        fee_paid: int | None = None, done: bool = True, notes: str | None = None,
    ) -> int:
        cursor = self.conn.execute(
            "INSERT INTO trades (item_key, side, qty, unit_price, fee_paid, listed_at, done_at, "
            "notes) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            (item_key, side, qty, unit_price, fee_paid, iso(at), iso(at) if done else None, notes),
        )
        self.conn.commit()
        return cursor.lastrowid

    # -- recipes (entered by hand) ----------------------------------------------

    def save_recipe(self, recipe: dict, inputs: list[tuple[int, int, int | None]]) -> int:
        """Insert or update (when recipe has an id). inputs = (item_key, qty, npc_price)."""
        fields = [
            "output_key", "output_qty", "success_rate", "combo_probability", "combo_output_key",
            "combo_output_qty", "kina_cost", "craft_seconds", "profession", "mastery_level",
            "rate_source", "rate_updated_at", "notes",
        ]
        values = [recipe.get(f) for f in fields]
        for key in [recipe.get("output_key"), recipe.get("combo_output_key")] + [i[0] for i in inputs]:
            if key:
                self.conn.execute("INSERT OR IGNORE INTO items (item_key) VALUES (?)", (key,))
        if recipe.get("id"):
            self.conn.execute(
                f"UPDATE recipes SET {', '.join(f + ' = ?' for f in fields)} WHERE id = ?",
                values + [recipe["id"]],
            )
            recipe_id = recipe["id"]
            self.conn.execute("DELETE FROM recipe_inputs WHERE recipe_id = ?", (recipe_id,))
        else:
            recipe_id = self.conn.execute(
                f"INSERT INTO recipes ({', '.join(fields)}) VALUES ({', '.join('?' * len(fields))})",
                values,
            ).lastrowid
        self.conn.executemany(
            "INSERT INTO recipe_inputs (recipe_id, item_key, qty, npc_price) VALUES (?, ?, ?, ?)",
            [(recipe_id, *i) for i in inputs],
        )
        self.conn.commit()
        return recipe_id

    def delete_recipe(self, recipe_id: int) -> None:
        self.conn.execute("DELETE FROM recipe_inputs WHERE recipe_id = ?", (recipe_id,))
        self.conn.execute("DELETE FROM recipes WHERE id = ?", (recipe_id,))
        self.conn.commit()

    def recipes(self) -> list[tuple[sqlite3.Row, list[sqlite3.Row]]]:
        rows = self.conn.execute(
            "SELECT r.*, i.grade AS output_grade FROM recipes r "
            "LEFT JOIN items i ON i.item_key = r.output_key ORDER BY r.id"
        ).fetchall()
        inputs: dict[int, list[sqlite3.Row]] = {}
        for line in self.conn.execute("SELECT * FROM recipe_inputs ORDER BY recipe_id, item_key"):
            inputs.setdefault(line["recipe_id"], []).append(line)
        return [(r, inputs.get(r["id"], [])) for r in rows]

    def item_names(self, keys) -> dict[int, str]:
        keys = list(keys)
        if not keys:
            return {}
        marks = ",".join("?" * len(keys))
        rows = self.conn.execute(f"SELECT item_key, name FROM items WHERE item_key IN ({marks})", keys)
        return {r["item_key"]: r["name"] for r in rows if r["name"]}

    # -- trades -----------------------------------------------------------------

    def trades(self) -> list[sqlite3.Row]:
        return self.conn.execute(
            "SELECT t.*, i.name FROM trades t LEFT JOIN items i USING (item_key) "
            "ORDER BY COALESCE(t.done_at, t.listed_at) DESC, t.id DESC"
        ).fetchall()

    def update_trade(self, trade_id: int, **fields) -> None:
        allowed = {"item_key", "side", "qty", "unit_price", "fee_paid", "listed_at", "done_at", "notes"}
        fields = {k: v for k, v in fields.items() if k in allowed}
        self.conn.execute(
            f"UPDATE trades SET {', '.join(k + ' = ?' for k in fields)} WHERE id = ?",
            [*fields.values(), trade_id],
        )
        self.conn.commit()

    def delete_trade(self, trade_id: int) -> None:
        self.conn.execute("DELETE FROM trades WHERE id = ?", (trade_id,))
        self.conn.commit()

    # -- holdings, craft outcomes, farming, events ----------------------------------

    def set_holding(self, item_key: int, qty: int, avg_cost: int | None) -> None:
        self.conn.execute(
            "UPDATE items SET hold_qty = ?, hold_avg_cost = ? WHERE item_key = ?",
            (qty, avg_cost, item_key),
        )
        self.conn.commit()

    def holdings(self) -> dict[int, sqlite3.Row]:
        rows = self.conn.execute(
            "SELECT item_key, watch, hold_qty, hold_avg_cost FROM items "
            "WHERE watch = 1 OR hold_qty > 0"
        )
        return {r["item_key"]: r for r in rows}

    def add_craft_outcome(
        self, recipe_id: int, attempts: int, successes: int, combos: int, at: datetime,
        notes: str | None = None,
    ) -> None:
        self.conn.execute(
            "INSERT INTO craft_outcomes (recipe_id, at, attempts, successes, combos, notes) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            (recipe_id, iso(at), attempts, successes, combos, notes),
        )
        self.conn.commit()

    def craft_outcome_totals(self) -> dict[int, sqlite3.Row]:
        rows = self.conn.execute(
            "SELECT recipe_id, SUM(attempts) AS attempts, SUM(successes) AS successes, "
            "SUM(combos) AS combos, COUNT(*) AS batches FROM craft_outcomes GROUP BY recipe_id"
        )
        return {r["recipe_id"]: r for r in rows}

    def add_farm_run(
        self, route: str, at: datetime, minutes: float, kina: int,
        drops: list[tuple[int, int]], notes: str | None = None,
    ) -> int:
        run_id = self.conn.execute(
            "INSERT INTO farm_runs (route, at, minutes, kina, notes) VALUES (?, ?, ?, ?, ?)",
            (route, iso(at), minutes, kina, notes),
        ).lastrowid
        self.conn.executemany(
            "INSERT INTO farm_drops (run_id, item_key, qty) VALUES (?, ?, ?)",
            [(run_id, key, qty) for key, qty in drops],
        )
        self.conn.commit()
        return run_id

    def farm_runs(self) -> list[tuple[sqlite3.Row, list[sqlite3.Row]]]:
        runs = self.conn.execute("SELECT * FROM farm_runs ORDER BY at DESC, id DESC").fetchall()
        return [
            (r, self.conn.execute("SELECT * FROM farm_drops WHERE run_id = ?", (r["id"],)).fetchall())
            for r in runs
        ]

    def delete_farm_run(self, run_id: int) -> None:
        self.conn.execute("DELETE FROM farm_drops WHERE run_id = ?", (run_id,))
        self.conn.execute("DELETE FROM farm_runs WHERE id = ?", (run_id,))
        self.conn.commit()

    def add_event(self, day: str, label: str, kind: str) -> None:
        self.conn.execute("INSERT INTO events (day, label, kind) VALUES (?, ?, ?)", (day, label, kind))
        self.conn.commit()

    def delete_event(self, event_id: int) -> None:
        self.conn.execute("DELETE FROM events WHERE id = ?", (event_id,))
        self.conn.commit()

    def events(self) -> list[sqlite3.Row]:
        return self.conn.execute("SELECT * FROM events ORDER BY day").fetchall()

    def full_price_history(self, server_id: int, item_key: int) -> list[sqlite3.Row]:
        """Every stored price for one item: hourly buckets plus our own snapshots, oldest first."""
        return self.conn.execute(
            "SELECT ts, price, listings FROM ("
            " SELECT ts, close AS price, listings FROM price_points"
            "  WHERE server_id = :s AND item_key = :k"
            " UNION SELECT captured_at, price, listings FROM item_snapshots"
            "  WHERE server_id = :s AND item_key = :k) ORDER BY ts",
            {"s": server_id, "k": item_key},
        ).fetchall()

    def recent_fetches(self, limit: int = 50, errors_only: bool = False) -> list[sqlite3.Row]:
        return self.conn.execute(
            "SELECT * FROM fetch_log "
            + ("WHERE error IS NOT NULL " if errors_only else "")
            + "ORDER BY id DESC LIMIT ?",
            (limit,),
        ).fetchall()

    def set_watch(self, item_key: int, watch: bool = True) -> None:
        self.conn.execute(
            "INSERT INTO items (item_key, watch) VALUES (?, ?) "
            "ON CONFLICT(item_key) DO UPDATE SET watch = excluded.watch",
            (item_key, int(watch)),
        )
        self.conn.commit()

    def watched_keys(self) -> list[int]:
        rows = self.conn.execute("SELECT item_key FROM items WHERE watch = 1 ORDER BY item_key")
        return [r["item_key"] for r in rows]

    # -- history ------------------------------------------------------------

    def save_price_series(self, server_id: int, series: m.PriceSeries) -> None:
        self.conn.executemany(
            "INSERT OR REPLACE INTO price_points (item_key, server_id, bucket, ts, low, high, avg, "
            "close, listings, samples) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            [
                (
                    series.item_key,
                    server_id,
                    series.bucket,
                    iso(p.ts),
                    p.low,
                    p.high,
                    p.avg,
                    p.close,
                    p.listings,
                    p.samples,
                )
                for p in series.points
            ],
        )
        self.conn.commit()

    def save_volume_series(self, server_id: int, series: m.VolumeSeries) -> None:
        self.conn.executemany(
            "INSERT OR REPLACE INTO sales_days (item_key, server_id, registered, units_sold, "
            "units_delta, days_since_previous, avg_price, min_price, max_price, window_from, "
            "window_to) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            [
                (
                    series.item_key,
                    server_id,
                    iso(d.registered),
                    d.units_sold,
                    d.units_delta,
                    d.days_since_previous,
                    d.avg_price,
                    d.min_price,
                    d.max_price,
                    iso(d.window_from),
                    iso(d.window_to),
                )
                for d in series.days
            ],
        )
        self.conn.commit()

    # -- raw response cache and fetch log -------------------------------------

    def cache_get(self, url: str) -> sqlite3.Row | None:
        return self.conn.execute("SELECT * FROM api_cache WHERE url = ?", (url,)).fetchone()

    def cache_put(self, url: str, body: str, fetched_at: datetime, status: int = 200) -> None:
        self.conn.execute(
            "INSERT OR REPLACE INTO api_cache (url, body, fetched_at, status) VALUES (?, ?, ?, ?)",
            (url, body, iso(fetched_at), status),
        )
        self.conn.commit()

    def log_fetch(
        self, at: datetime, endpoint: str, status: int | None, ms: int, error: str | None = None
    ) -> None:
        self.conn.execute(
            "INSERT INTO fetch_log (at, endpoint, status, ms, error) VALUES (?, ?, ?, ?, ?)",
            (iso(at), endpoint, status, ms, error),
        )
        self.conn.commit()
