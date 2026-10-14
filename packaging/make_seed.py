"""Writes packaging/seed/: what the exe starts from on a new install.

    market.db             a copy of data/market.db (prices, history, recipes, settings)
                          without your trades, craft outcomes, farming runs, events and
                          watchlist
    icons/                the item icons already downloaded to data/icons
    crafting-graph.json   data/crafting-graph.json if you saved it there: the app imports
                          it for the user's race, and again when they switch race
"""

import shutil
import sqlite3
import sys
from pathlib import Path

root = Path(__file__).resolve().parents[1]
data = root / "data"
seed = Path(__file__).with_name("seed")
source = data / "market.db"
target = seed / "market.db"

shutil.rmtree(seed, ignore_errors=True)
seed.mkdir()
if (data / "crafting-graph.json").is_file():
    shutil.copyfile(data / "crafting-graph.json", seed / "crafting-graph.json")
    print(seed / "crafting-graph.json")
if not source.is_file():
    print("no data/market.db: the exe will start empty")
    sys.exit(0)

shutil.copytree(data / "icons", seed / "icons", ignore=shutil.ignore_patterns(".gitkeep"))
print(seed / "icons", len(list((seed / "icons").iterdir())), "files")
src = sqlite3.connect(source)
dst = sqlite3.connect(target)
src.backup(dst)  # consistent copy, including what is still in the WAL
src.close()
for table in ("trades", "craft_outcomes", "farm_drops", "farm_runs", "events", "fetch_log"):
    dst.execute(f"DELETE FROM {table}")
dst.execute("UPDATE items SET watch = 0")
dst.commit()
dst.execute("PRAGMA journal_mode = DELETE")
dst.execute("VACUUM")
dst.close()
print(target, f"{target.stat().st_size / 1e6:.1f} MB")
