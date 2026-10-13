"""The main window: status bar on top, tabs below. Reads SQLite only; the worker does the network."""

from __future__ import annotations

from pathlib import Path

import pyqtgraph as pg
from PySide6.QtCore import QTimer
from PySide6.QtGui import QPalette
from PySide6.QtWidgets import (
    QApplication,
    QComboBox,
    QFrame,
    QHBoxLayout,
    QLabel,
    QMainWindow,
    QPushButton,
    QTabWidget,
    QVBoxLayout,
    QWidget,
)

from app.db.repo import Database, utcnow
from app.external.scheduler import RefreshResult, RefreshWorker
from app.ui import tables
from app.ui.items_tab import ItemsTab
from app.ui.state import AppState
from app.ui.tabs import CraftingTab, FlipsTab, OpportunitiesTab, SettingsTab, TrustTab

HEALTH = {  # state -> (colour, label)
    "ok": ("#2e9e5b", "API healthy"),
    "stale": ("#d9930d", "Data is stale"),
    "failing": ("#d0443a", "Collector failing"),
    "down": ("#d0443a", "API unreachable"),
}


class StatusBar(QFrame):
    """Always visible: server, Refresh now, data age, API health, next refresh."""

    def __init__(self, state: AppState, worker: RefreshWorker, parent=None):
        super().__init__(parent)
        self.state = state
        self.worker = worker
        self._busy = False
        self.setFrameShape(QFrame.Shape.StyledPanel)

        self.server = QComboBox()
        self.server.setMinimumWidth(260)
        self.server.activated.connect(self._server_chosen)
        self.refresh_button = QPushButton("Refresh now")
        self.refresh_button.clicked.connect(lambda: self.worker.request_refresh())
        self.dot = QLabel("●")
        self.health = QLabel("Starting…")
        self.ages = QLabel()
        self.next = QLabel()
        self.new = QLabel()

        row = QHBoxLayout(self)
        row.addWidget(QLabel("Server"))
        row.addWidget(self.server)
        row.addWidget(self.refresh_button)
        row.addSpacing(12)
        row.addWidget(self.dot)
        row.addWidget(self.health)
        row.addSpacing(12)
        row.addWidget(self.ages, 1)
        row.addWidget(self.new)
        row.addWidget(self.next)

        self._timer = QTimer(self, interval=20_000)
        self._timer.timeout.connect(self.update_text)
        self._timer.start()
        state.changed.connect(self.refresh)

    def set_busy(self, busy: bool) -> None:
        self._busy = busy
        self.refresh_button.setEnabled(not busy)
        self.refresh_button.setText("Refreshing…" if busy else "Refresh now")

    def refresh(self) -> None:
        servers = self.state.db.servers()
        if self.server.count() != len(servers):
            self.server.clear()
            for s in servers:
                self.server.addItem(f"{s['name']} — {s['region']} {s['faction']}", s["id"])
        index = self.server.findData(self.state.server_id)
        if index >= 0:
            self.server.setCurrentIndex(index)
        self.update_text()

    def update_text(self) -> None:
        f = self.state.freshness
        if f is None:
            return
        colour, label = HEALTH[f.state]
        self.dot.setStyleSheet(f"color: {colour}; font-size: 16px;")
        self.health.setText(label)
        self.health.setToolTip(f.detail)
        now = utcnow()
        listings = tables.age(now - f.snapshot_at) if f.snapshot_at else "unknown"
        sales = tables.age(now - f.sales_batch_at) if f.sales_batch_at else "none for this server"
        self.ages.setText(f"Listings: {listings} old   ·   Sales batch: {sales}")
        due = self.worker.next_refresh_at
        if due:
            minutes = max(0, int((due - now).total_seconds() // 60))
            self.next.setText(f"Next refresh in {minutes} min")
        count = self.state.new_opportunities
        self.new.setText(f"{count} new opportunit{'y' if count == 1 else 'ies'}   ·" if count else "")

    def _server_chosen(self, index: int) -> None:
        server_id = self.server.itemData(index)
        if server_id is None or server_id == self.state.server_id:
            return
        self.state.db.set_setting("server_id", server_id)
        self.state.server_id = server_id
        self.state.freshness = None
        self.health.setText("Switching server…")
        self.state.reload()  # show whatever is stored for that server straight away
        self.worker.request_refresh(force=True)


class MainWindow(QMainWindow):
    def __init__(self, db_path: str | Path, source_factory, parent=None):
        super().__init__(parent)
        self.setWindowTitle("AION 2 Market Assistant")
        self.resize(1500, 900)
        self.db = Database(db_path)
        self.state = AppState(self.db, self)
        self.worker = RefreshWorker(db_path, source_factory, self)

        self.status = StatusBar(self.state, self.worker)
        self.banner = QLabel()
        self.banner.setWordWrap(True)
        self.banner.setStyleSheet("background: #d0443a; color: white; padding: 6px; font-weight: 600;")
        self.banner.hide()

        self.opportunities = OpportunitiesTab(self.state)
        self.items = ItemsTab(self.state, self.worker.request_item)
        self.crafting = CraftingTab(self.state)
        self.flips = FlipsTab(self.state)
        self.trust = TrustTab(self.state)
        self.settings = SettingsTab(self.state)
        self.tabs = QTabWidget()
        for widget, title in (
            (self.opportunities, "Opportunities"),
            (self.items, "Items"),
            (self.crafting, "Crafting"),
            (self.flips, "Flips"),
            (self.trust, "Trust"),
            (self.settings, "Settings"),
        ):
            self.tabs.addTab(widget, title)

        central = QWidget()
        layout = QVBoxLayout(central)
        layout.addWidget(self.status)
        layout.addWidget(self.banner)
        layout.addWidget(self.tabs, 1)
        self.setCentralWidget(central)

        self.opportunities.open_item.connect(self.open_item)
        self.flips.open_item.connect(self.open_item)
        self.settings.saved.connect(self._settings_saved)
        self.items.watch_changed.connect(self.state.reload)
        self.state.changed.connect(self._update_banner)
        self.worker.started_refresh.connect(lambda: self.status.set_busy(True))
        self.worker.refreshed.connect(self._refreshed)
        self.worker.item_loaded.connect(self.items.on_item_loaded)

        self.state.reload()  # show stored data before the first refresh returns
        self.worker.start()

    def open_item(self, item_key: int) -> None:
        self.tabs.setCurrentWidget(self.items)
        self.items.open_item(item_key)

    def _refreshed(self, result: RefreshResult) -> None:
        self.status.set_busy(False)
        self.state.on_refreshed(result)

    def _settings_saved(self) -> None:
        self.state.reload()
        self.worker.request_refresh(force=True)

    def _update_banner(self) -> None:
        f = self.state.freshness
        if f and f.state in ("failing", "down"):
            self.banner.setText(
                ("The API cannot be reached. " if f.state == "down" else "The API's collector is failing. ")
                + "Showing stored data; alerts are paused. "
                + f.detail
            )
            self.banner.show()
        else:
            self.banner.hide()

    def closeEvent(self, event) -> None:
        self.worker.stop()
        self.db.close()
        super().closeEvent(event)


def style_charts(app: QApplication) -> None:
    """Charts follow the system's light or dark palette."""
    palette = app.palette()
    pg.setConfigOptions(
        antialias=True,
        background=palette.color(QPalette.ColorRole.Base),
        foreground=palette.color(QPalette.ColorRole.Text),
    )
