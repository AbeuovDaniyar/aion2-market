"""SQLite access. One Database per thread: the refresh worker and the UI each open their own."""

from __future__ import annotations

import sqlite3
from collections.abc import Iterable
from datetime import UTC, datetime
from pathlib import Path

from app.external import models as m

SCHEMA_PATH = Path(__file__).with_name("schema.sql")
DEFAULT_DB_PATH = Path(__file__).resolve().parents[2] / "data" / "market.db"

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
    "region_gap_pct": "15",  # how far from the regional median counts as cheap/expensive
    # Trust
    "sanity_jump_factor": "5",
    "sanity_drop_factor": "0.2",
    "streak_risk": "0.05",  # the 1-in-20 bad streak
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
