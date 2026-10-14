"""Entry point.

    python -m app.main                the desktop window
    python -m app.main --once         one refresh, print the result, no window
    python -m app.main --report       one refresh, then print the analysis, no window
    python -m app.main --watch KEY    add an item to the watchlist first
"""

from __future__ import annotations

import argparse
import shutil
import sys
from pathlib import Path

from app.db.repo import DEFAULT_DB_PATH, Database, icons_dir
from app.external.aion2exchange import Aion2ExchangeSource
from app.external.icons import IconFetcher
from app.external.scheduler import RefreshResult, refresh_once


def describe(result: RefreshResult) -> str:
    f = result.freshness
    age = "?" if f.snapshot_age_seconds is None else f"{f.snapshot_age_seconds // 60} min"
    line = (
        f"[{result.at:%H:%M:%S}Z] server {result.server_id}: {f.state.upper()}"
        f" | listings snapshot {age} old | sales batch {f.sales_batch_at or '?'}"
        f"{' (stale)' if f.sales_stale else ''}"
        f" | {result.items_updated} items, {result.sales_updated} sales histories updated"
    )
    if result.reference_updated:
        line += f" | {result.reference_updated} reference servers updated"
    if result.served_stale:
        line += " | showing cached data"
    for extra in (f.detail, result.error):
        if extra:
            line += f" | {extra}"
    return line


def seed_new_install(db_path: str) -> None:
    """First run of the packaged exe: start from the history bundled into it
    (packaging/make_seed.py) instead of an empty database."""
    seed = Path(getattr(sys, "_MEIPASS", "")) / "seed"
    if getattr(sys, "frozen", False) and (seed / "market.db").is_file() and not Path(db_path).exists():
        Path(db_path).parent.mkdir(parents=True, exist_ok=True)
        if (seed / "icons").is_dir():
            shutil.copytree(seed / "icons", icons_dir(db_path), dirs_exist_ok=True)
        shutil.copyfile(seed / "market.db", db_path)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="AION 2 Market Assistant")
    parser.add_argument("--db", default=str(DEFAULT_DB_PATH))
    parser.add_argument("--once", action="store_true", help="refresh once and exit")
    parser.add_argument("--report", action="store_true", help="refresh once, print the analysis")
    parser.add_argument("--watch", type=int, action="append", default=[], metavar="ITEM_KEY")
    args = parser.parse_args(argv)

    seed_new_install(args.db)
    db = Database(args.db)
    for key in args.watch:
        db.set_watch(key)

    if args.once or args.report:
        source = Aion2ExchangeSource(db)
        result = refresh_once(db, source)
        print(describe(result))
        if args.report and result.server_id is not None:
            from app.report import build_report

            print()
            print(build_report(db, source, result.server_id))
        source.close()
        db.close()
        return 0
    theme = db.get_setting("theme") or "dark"
    db.close()

    from PySide6.QtWidgets import QApplication

    from app.ui.main_window import MainWindow, apply_theme

    app = QApplication(sys.argv[:1])
    app.setApplicationName("AION 2 Market Assistant")
    apply_theme(app, theme)
    window = MainWindow(
        args.db,
        Aion2ExchangeSource,
        icon_fetcher_factory=lambda db: IconFetcher(db, icons_dir(args.db)),
    )
    window.show()
    return app.exec()


if __name__ == "__main__":
    sys.exit(main())
