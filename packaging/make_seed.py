"""Writes packaging/seed/market.db: a copy of data/market.db (prices, history, recipes,
settings) for the exe to start from on a new install. Your own trades, holdings, craft
outcomes, farming runs and events are left out. Icon rows are cleared so the new
install downloads its own icons instead of pointing at files it does not have."""

import sqlite3
import sys
from pathlib import Path

root = Path(__file__).resolve().parents[1]
source = root / "data" / "market.db"
target = Path(__file__).with_name("seed") / "market.db"

if not source.is_file():
    print("no data/market.db: the exe will start empty")
    target.unlink(missing_ok=True)
    sys.exit(0)

target.parent.mkdir(exist_ok=True)
target.unlink(missing_ok=True)
src = sqlite3.connect(source)
dst = sqlite3.connect(target)
src.backup(dst)  # consistent copy, including what is still in the WAL
src.close()
for table in ("trades", "craft_outcomes", "farm_drops", "farm_runs", "events", "item_icons", "fetch_log"):
    dst.execute(f"DELETE FROM {table}")
dst.execute("UPDATE items SET watch = 0")
dst.commit()
dst.execute("PRAGMA journal_mode = DELETE")
dst.execute("VACUUM")
dst.close()
print(target, f"{target.stat().st_size / 1e6:.1f} MB")
