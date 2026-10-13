"""Real item icons from NCSOFT's official AION 2 item dictionary.

The dictionary (the one behind aion2.plaync.com/en-us/info/item) gives an icon
URL and a grade per item. Each item is looked up once by its exact name, the
image is saved under data/icons/, and neither is asked for again. This is not
market data and is kept apart from the PriceSource.
"""

from __future__ import annotations

import time
from collections.abc import Callable
from datetime import timedelta
from pathlib import Path
from urllib.parse import urlsplit

import httpx

from app.db.repo import Database, utcnow

DICTIONARY_URL = "https://api-goats.plaync.com/aion2/v2.0/dict/search/item"
IMAGE_HOST = "assets.playnccdn.com"  # images are only ever downloaded from here
USER_AGENT = "aion2-market-assistant/0.1 (personal desktop app)"
RETRY_MISSING_AFTER = timedelta(days=7)
MAX_IMAGE_BYTES = 2_000_000


class IconFetcher:
    def __init__(
        self,
        db: Database,
        icons_dir: str | Path,
        client: httpx.Client | None = None,
        min_interval_s: float = 1.0,
        sleep: Callable[[float], None] = time.sleep,
    ):
        self.db = db
        self.dir = Path(icons_dir)
        self.client = client or httpx.Client(timeout=15, headers={"User-Agent": USER_AGENT})
        self.min_interval_s = min_interval_s
        self._sleep = sleep
        self._last = float("-inf")
        self._failures = 0

    def close(self) -> None:
        self.client.close()

    def enabled(self) -> bool:
        # A run of network failures stops the fetcher until the app restarts.
        return self.db.get_setting("real_item_icons") == "1" and self._failures < 5

    def pending(self, server_id: int, limit: int = 5) -> list[tuple[int, str]]:
        """Named items of this server that have not been looked up (or not recently)."""
        cutoff = (utcnow() - RETRY_MISSING_AFTER).isoformat()
        rows = self.db.conn.execute(
            "SELECT i.item_key, i.name FROM items i "
            "JOIN (SELECT DISTINCT item_key FROM item_snapshots WHERE server_id = ?) s USING (item_key) "
            "LEFT JOIN item_icons c USING (item_key) "
            "WHERE i.name IS NOT NULL AND (c.item_key IS NULL OR (c.file IS NULL AND c.checked_at < ?)) "
            "ORDER BY i.watch DESC, i.name LIMIT ?",
            (server_id, cutoff, limit),
        )
        return [(r["item_key"], r["name"]) for r in rows]

    def fetch_some(self, server_id: int, limit: int = 5) -> int:
        """Look up and download icons for a few items. Returns how many were resolved."""
        if not self.enabled():
            return 0
        done = 0
        for item_key, name in self.pending(server_id, limit):
            try:
                self._fetch_one(item_key, name)
                self._failures = 0
                done += 1
            except (httpx.HTTPError, OSError, ValueError):
                self._failures += 1
                if not self.enabled():
                    break
        return done

    # -- internals ------------------------------------------------------------------

    def _get(self, url: str, **params) -> httpx.Response:
        wait = self._last + self.min_interval_s - time.monotonic()
        if wait > 0:
            self._sleep(wait)
        self._last = time.monotonic()
        response = self.client.get(url, params=params or None)
        response.raise_for_status()
        return response

    def _fetch_one(self, item_key: int, name: str) -> None:
        body = self._get(
            DICTIONARY_URL, searchKeyword=name, exact="true", size=10, locale="en-US"
        ).json()
        matches = [c for c in body.get("contents", []) if isinstance(c, dict)]
        # The dictionary uses the same item ids; fall back on the exact name.
        entry = next((c for c in matches if c.get("id") == item_key), None) or next(
            (c for c in matches if c.get("name") == name), None
        )
        url = (entry or {}).get("image")
        file = self._download(url) if url else None
        self.db.conn.execute(
            "INSERT OR REPLACE INTO item_icons (item_key, file, grade, checked_at) VALUES (?, ?, ?, ?)",
            (item_key, file, (entry or {}).get("grade"), utcnow().isoformat()),
        )
        self.db.conn.commit()

    def _download(self, url: str) -> str | None:
        parts = urlsplit(url)
        name = Path(parts.path).name
        safe = name.replace("_", "").replace("-", "").replace(".", "").isalnum()
        if parts.scheme != "https" or parts.hostname != IMAGE_HOST or not safe:
            return None
        if not name.lower().endswith((".png", ".webp", ".jpg", ".jpeg")):
            return None
        target = self.dir / name
        if not target.exists():  # many items share one image
            content = self._get(url).content
            if not content or len(content) > MAX_IMAGE_BYTES:
                return None
            self.dir.mkdir(parents=True, exist_ok=True)
            target.write_bytes(content)
        return name


def icon_files(db: Database) -> dict[int, tuple[str, str | None]]:
    """item_key -> (file name under the icons folder, grade) for every icon on disk."""
    rows = db.conn.execute("SELECT item_key, file, grade FROM item_icons WHERE file IS NOT NULL")
    return {r["item_key"]: (r["file"], r["grade"]) for r in rows}
