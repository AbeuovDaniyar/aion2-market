"""Record one real response per endpoint into tests/fixtures/api/.

Run once by hand (python scripts/record_fixtures.py). Tests replay these files
with respx and never hit the live API. Only /api/... endpoints are called, at
under one request per second.
"""

from __future__ import annotations

import json
import sys
import time
from pathlib import Path

import httpx

BASE = "https://aion2.exchange"
OUT = Path(__file__).resolve().parents[1] / "tests" / "fixtures" / "api"
HEADERS = {"User-Agent": "aion2-market-assistant/0.1 (personal use; fixture recording)"}
PAUSE = 1.2


def main() -> int:
    OUT.mkdir(parents=True, exist_ok=True)
    with httpx.Client(base_url=BASE, timeout=15, headers=HEADERS) as client:

        def record(name: str, path: str, **params) -> dict | None:
            time.sleep(PAUSE)
            r = client.get(path, params=params)
            print(f"{r.status_code} {r.url} ({len(r.content)} bytes)")
            if r.status_code != 200:
                return None
            body = r.json()
            (OUT / f"{name}.json").write_text(
                json.dumps(body, ensure_ascii=False, indent=1), encoding="utf-8"
            )
            return body

        servers = record("servers", "/api/servers")
        if servers is None:
            return 1
        server = servers["default"]

        record("health", "/api/health", server=server)
        record("collection", "/api/collection", server=server)
        record("categories", "/api/categories", server=server)
        record("overview", "/api/overview", server=server, window="24h")
        items = record(
            "items", "/api/items", server=server, sort="units_sold", order="desc", limit=20
        )
        if not items or not items["items"]:
            return 1
        key = items["items"][0]["item_key"]
        keys = ",".join(str(i["item_key"]) for i in items["items"][:5])
        record("items_keys", "/api/items", server=server, keys=keys)
        record("prices", f"/api/items/{key}/prices", server=server, bucket="1h")
        record("depth", f"/api/items/{key}/depth", server=server, units=10)
        record("depth_ladder", f"/api/items/{key}/depth/ladder", server=server)
        record("volume", f"/api/items/{key}/volume", server=server)
        record("observations", f"/api/items/{key}/observations", server=server)
        crafts = record("crafts", "/api/crafts", server=server)
        record("crafts_model", "/api/crafts/model", server=server)
        if crafts and crafts["targets"]:
            target = crafts["targets"][0]["target_key"]
            record("craft_detail", f"/api/crafts/{target}", server=server)
    return 0


if __name__ == "__main__":
    sys.exit(main())
