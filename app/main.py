"""Entry point. Until the desktop UI lands (milestone 3) this runs the refresh headless.

    python -m app.main --once         one refresh, print the result
    python -m app.main --report       one refresh, then print the analysis
    python -m app.main                keep refreshing on schedule (Ctrl+C to stop)
    python -m app.main --watch KEY    add an item to the watchlist first
"""

from __future__ import annotations

import argparse
import signal
import sys

from PySide6.QtCore import QCoreApplication

from app.db.repo import DEFAULT_DB_PATH, Database
from app.external.aion2exchange import Aion2ExchangeSource
from app.external.scheduler import RefreshResult, RefreshWorker, refresh_once


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


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="AION 2 Market Assistant")
    parser.add_argument("--db", default=str(DEFAULT_DB_PATH))
    parser.add_argument("--once", action="store_true", help="refresh once and exit")
    parser.add_argument("--report", action="store_true", help="refresh once, print the analysis")
    parser.add_argument("--watch", type=int, action="append", default=[], metavar="ITEM_KEY")
    args = parser.parse_args(argv)

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
    db.close()

    app = QCoreApplication(sys.argv[:1])
    worker = RefreshWorker(args.db, Aion2ExchangeSource)
    worker.refreshed.connect(lambda result: print(describe(result), flush=True))
    signal.signal(signal.SIGINT, lambda *_: app.quit())
    worker.start()
    code = app.exec()
    worker.stop()
    return code


if __name__ == "__main__":
    sys.exit(main())
