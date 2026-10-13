"""Items tab: searchable catalog on the left, the selected item's charts on the right."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pyqtgraph as pg
from PySide6.QtCore import Qt, Signal
from PySide6.QtWidgets import (
    QButtonGroup,
    QCheckBox,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QPlainTextEdit,
    QPushButton,
    QSplitter,
    QTabWidget,
    QVBoxLayout,
    QWidget,
)

from app.analysis import farming, pricing, service
from app.db.repo import parse_iso, utcnow
from app.ui import tables
from app.ui.icons import item_icon
from app.ui.state import AppState
from app.ui.tables import Column, Table

RANGES = {"24 h": 1, "7 days": 7, "30 days": 30, "All": None}

COLUMNS = [
    Column("Item", lambda v: v.name or str(v.item_key),
           icon=lambda v: item_icon(v.section, v.category_name)),
    Column("Category", lambda v: v.category_name),
    Column("Cheapest", lambda v: v.cheapest, tables.kina),
    Column("Est. sale", lambda v: v.estimate.price, tables.kina,
           "Realistic sale price before fees: the lowest of the sale average, the depth price "
           "and the cheapest listing minus one undercut"),
    Column("Confidence", lambda v: v.estimate.confidence),
    Column("Listings", lambda v: v.listings, tables.kina),
    Column("Sold (28 d)", lambda v: v.sales.units_sold, tables.kina),
    Column("Per day", lambda v: v.liquidity.units_per_day, tables.per_day,
           "Units per day; see the Source column for where the number comes from"),
    Column("Source", lambda v: v.liquidity.source.replace("_", " ")),
    Column("Region gap", lambda v: v.region.gap_pct if v.region else None, tables.pct,
           "This server's cheapest price against the median of the region's other servers"),
    Column("Region median", lambda v: v.region.regional_median if v.region else None, tables.kina),
    Column("Ref. sale avg", lambda v: v.demand.sold_avg if v.demand else None, tables.kina,
           "Average real sale price on the demand-reference servers"),
    Column("Ref. per day", lambda v: v.demand.units_per_day if v.demand else None, tables.per_day,
           "Units sold per day on the demand-reference servers"),
    Column("Flags", lambda v: tables.flags(v.estimate.flags + v.sanity)),
]


class ItemsTab(QWidget):
    watch_changed = Signal()

    def __init__(self, state: AppState, request_item, parent=None):
        super().__init__(parent)
        self.state = state
        self._request_item = request_item
        self._key: int | None = None
        self._days: int | None = 7

        self.search = QLineEdit(placeholderText="Search items…")
        self.search.setClearButtonEnabled(True)
        self.watched_only = QCheckBox("Watched only")
        self.table = Table(COLUMNS, dim=lambda v: not v.usable)
        self.search.textChanged.connect(self.table.proxy.setFilterFixedString)
        self.watched_only.toggled.connect(self.refresh)
        self.table.selectionModel().selectionChanged.connect(self._selected)

        left = QWidget()
        box = QVBoxLayout(left)
        box.setContentsMargins(0, 0, 0, 0)
        top = QHBoxLayout()
        top.addWidget(self.search, 1)
        top.addWidget(self.watched_only)
        box.addLayout(top)
        box.addWidget(self.table)

        # -- detail panel --
        self.title = QLabel("Select an item")
        self.title.setStyleSheet("font-size: 15px; font-weight: 600;")
        self.title_icon = QLabel()
        self.stats = QLabel()
        self.stats.setWordWrap(True)
        self.reference = QLabel()
        self.reference.setWordWrap(True)
        self.status = QLabel()
        self.watch = QCheckBox("Watch")
        self.watch.clicked.connect(self._toggle_watch)

        ranges = QHBoxLayout()
        self._range_group = QButtonGroup(self)
        for label, span in RANGES.items():
            button = QPushButton(label, checkable=True)
            button.setChecked(span == self._days)
            button.clicked.connect(lambda _=False, s=span: self._set_range(s))
            self._range_group.addButton(button)
            ranges.addWidget(button)
        ranges.addStretch(1)
        ranges.addWidget(self.watch)

        self.price_chart = pg.GraphicsLayoutWidget()
        self.price_plot = self.price_chart.addPlot(row=0, col=0, axisItems={"bottom": pg.DateAxisItem()})
        self.price_plot.setLabel("left", "Kina")
        self.price_plot.addLegend(offset=(8, 8))
        self.price_plot.showGrid(y=True, alpha=0.2)
        self.listing_plot = self.price_chart.addPlot(row=1, col=0, axisItems={"bottom": pg.DateAxisItem()})
        self.listing_plot.setLabel("left", "Listings")
        self.listing_plot.setXLink(self.price_plot)
        self.price_chart.ci.layout.setRowStretchFactor(0, 3)
        self.price_chart.ci.layout.setRowStretchFactor(1, 1)

        self.depth_plot = pg.PlotWidget()
        self.depth_plot.setLabel("left", "Unit price (Kina)")
        self.depth_plot.setLabel("bottom", "Units you could buy, cheapest first")
        self.depth_note = QLabel()
        depth = QWidget()
        depth_box = QVBoxLayout(depth)
        depth_box.addWidget(self.depth_note)
        depth_box.addWidget(self.depth_plot)

        self.sales_plot = pg.PlotWidget(axisItems={"bottom": pg.DateAxisItem()})
        self.sales_plot.setLabel("left", "Units sold (28-day total)")
        self.sales_note = QLabel()
        sales = QWidget()
        sales_box = QVBoxLayout(sales)
        sales_box.addWidget(self.sales_note)
        sales_box.addWidget(self.sales_plot)

        self.notes = QPlainTextEdit(placeholderText="Notes for this item…")
        save_notes = QPushButton("Save notes")
        save_notes.clicked.connect(self._save_notes)
        notes = QWidget()
        notes_box = QVBoxLayout(notes)
        notes_box.addWidget(self.notes)
        notes_box.addWidget(save_notes, 0, Qt.AlignmentFlag.AlignRight)

        self.charts = QTabWidget()
        self.charts.addTab(self.price_chart, "Price")
        self.charts.addTab(depth, "Depth")
        self.charts.addTab(sales, "Sales")
        self.timing_plot = pg.PlotWidget()
        self.timing_plot.setLabel("left", "Average price (Kina)")
        self.timing_note = QLabel()
        self.timing_note.setWordWrap(True)
        timing = QWidget()
        timing_box = QVBoxLayout(timing)
        timing_box.addWidget(self.timing_note)
        timing_box.addWidget(self.timing_plot)
        self.charts.addTab(timing, "Timing")
        self.charts.addTab(notes, "Notes")

        right = QWidget()
        detail = QVBoxLayout(right)
        heading = QHBoxLayout()
        heading.addWidget(self.title_icon)
        heading.addWidget(self.title, 1)
        detail.addLayout(heading)
        detail.addWidget(self.stats)
        detail.addWidget(self.reference)
        detail.addLayout(ranges)
        detail.addWidget(self.charts, 1)
        detail.addWidget(self.status)

        splitter = QSplitter()
        splitter.addWidget(left)
        splitter.addWidget(right)
        splitter.setStretchFactor(0, 3)
        splitter.setStretchFactor(1, 2)
        splitter.setSizes([860, 600])
        layout = QVBoxLayout(self)
        layout.addWidget(splitter)

        state.changed.connect(self.refresh)

    # -- list ---------------------------------------------------------------------

    def refresh(self) -> None:
        views = self.state.views
        if self.watched_only.isChecked():
            watched = set(self.state.db.watched_keys())
            views = [v for v in views if v.item_key in watched]
        self.table.set_rows(views, keep=lambda v: v.item_key)
        if self._key is not None:
            self._draw()

    def open_item(self, item_key: int) -> None:
        self.search.clear()
        self.watched_only.setChecked(False)
        self.table.select_where(lambda v: v.item_key == item_key)

    def _selected(self) -> None:
        view = self.table.selected()
        if view is None or view.item_key == self._key:
            return
        self._key = view.item_key
        self.status.setText("Loading history…")
        self._draw()
        self._request_item(self.state.server_id, view.item_key)

    def on_item_loaded(self, item_key: int, error: str | None) -> None:
        if item_key != self._key:
            return
        self.status.setText(f"History could not be refreshed: {error}" if error else "")
        self._draw()

    # -- detail -------------------------------------------------------------------

    def _set_range(self, span: int | None) -> None:
        self._days = span
        self._draw()

    def _toggle_watch(self, checked: bool) -> None:
        if self._key is not None:
            self.state.db.set_watch(self._key, checked)
            self.watch_changed.emit()
            if self.watched_only.isChecked():
                self.refresh()

    def _save_notes(self) -> None:
        if self._key is not None:
            self.state.db.set_notes(self._key, self.notes.toPlainText())
            self.status.setText("Notes saved.")

    def _draw(self) -> None:
        view = self.state.by_key.get(self._key)
        if view is None:
            return
        db, server = self.state.db, self.state.server_id
        now = utcnow()
        self.title.setText(view.name or str(view.item_key))
        self.title_icon.setPixmap(item_icon(view.section, view.category_name).pixmap(32, 32))
        self.watch.setChecked(view.item_key in db.watched_keys())
        row = db.conn.execute("SELECT notes FROM items WHERE item_key = ?", (view.item_key,)).fetchone()
        if not self.notes.hasFocus():
            self.notes.setPlainText((row["notes"] if row else "") or "")

        # Hourly buckets from the API when loaded, our own snapshots otherwise.
        buckets = service.chart_points(db, server, view.item_key)
        if buckets:
            ts = [parse_iso(b["ts"]) for b in buckets]
            low = [b["low"] for b in buckets]
            avg = [b["avg"] for b in buckets]
            listings = [b["listings"] or 0 for b in buckets]
        else:
            ts = [o.ts for o in view.history]
            low = avg = [o.price for o in view.history]
            listings = [o.listings or 0 for o in view.history]
        observations = [pricing.Obs(t, p) for t, p in zip(ts, low)]

        stats = service.price_stats(observations, now)
        e = view.estimate
        self.stats.setText(
            f"Cheapest {tables.kina(view.cheapest)} · est. sale {tables.kina(e.price)} "
            f"({e.basis.replace('_', ' ')}, {e.confidence} confidence) · "
            f"{view.listings} listings · data {tables.age(view.age.listings_age)} old<br>"
            f"7-day median {tables.kina(stats.median_7d)} · low {tables.kina(stats.low_7d)} · "
            f"high {tables.kina(stats.high_7d)} · vs previous week "
            f"{tables.pct(stats.change_vs_prev_week_pct) or 'n/a'} · volatility "
            f"{tables.rate(stats.volatility) or 'n/a'}"
        )
        parts = []
        if view.region and view.region.regional_median is not None:
            r = view.region
            gap = f" ({tables.pct(r.gap_pct)} here)" if r.gap_pct is not None else ""
            parts.append(
                f"Region: median {tables.kina(r.regional_median)}{gap}, range "
                f"{tables.kina(r.regional_low)} to {tables.kina(r.regional_high)} on {r.servers} servers"
            )
        if view.demand:
            d = view.demand
            parts.append(
                f"Demand reference: {tables.per_day(d.units_per_day) or 'no'} sold/day, sale avg "
                f"{tables.kina(d.sold_avg) or 'n/a'}, cheapest {tables.kina(d.price) or 'n/a'} "
                "(another market, indicative only)"
            )
        self.reference.setText("<br>".join(parts))

        if self._days is not None:
            start = now - timedelta(days=self._days)
            keep = [i for i, t in enumerate(ts) if t >= start]
            ts = [ts[i] for i in keep]
            low = [low[i] for i in keep]
            avg = [avg[i] for i in keep]
            listings = [listings[i] for i in keep]
        self._draw_price(ts, low, avg, listings)
        self._draw_depth(view)
        self._draw_sales(view)
        self._draw_timing(view)

    def _draw_price(self, ts, low, avg, listings) -> None:
        x = [t.timestamp() for t in ts]
        self.price_plot.clear()
        self.listing_plot.clear()
        if not x:
            return
        accent = self.palette().highlight().color()
        text = self.palette().text().color()
        for start, end in _weekends(ts[0], ts[-1]):
            for plot in (self.price_plot, self.listing_plot):
                band = pg.LinearRegionItem((start, end), movable=False, brush=(128, 128, 128, 28), pen=None)
                band.setZValue(-10)
                plot.addItem(band)
        self.price_plot.plot(x, avg, pen=pg.mkPen(text, width=1, style=Qt.PenStyle.DashLine), name="Average")
        self.price_plot.plot(x, low, pen=pg.mkPen(accent, width=2), name="Lowest", symbol="o" if len(x) < 40 else None, symbolSize=5, symbolBrush=accent)
        width = min((b - a for a, b in zip(x, x[1:])), default=3600) * 0.8
        self.listing_plot.addItem(pg.BarGraphItem(x=x, height=listings, width=width, brush=(128, 128, 128, 150), pen=None))
        # Danny's event tags (patch, event, merge) as labelled lines.
        for event in self.state.db.events():
            at = parse_iso(event["day"] + "T12:00:00Z").timestamp()
            if x[0] <= at <= x[-1]:
                self.price_plot.addItem(
                    pg.InfiniteLine(at, angle=90, movable=False, pen=pg.mkPen("#d9930d", width=1),
                                    label=event["label"], labelOpts={"position": 0.92, "color": "#d9930d"})
                )
        self.price_plot.enableAutoRange()
        self.listing_plot.enableAutoRange()

    def _draw_timing(self, view) -> None:
        self.timing_plot.clear()
        rows = self.state.db.full_price_history(self.state.server_id, view.item_key)
        points = [(parse_iso(r["ts"]), r["price"]) for r in rows]
        profile = farming.timing_profile(points)
        if profile is None:
            have = (points[-1][0] - points[0][0]).total_seconds() / 86400 if len(points) > 1 else 0
            self.timing_note.setText(
                f"Timing insights need 2+ weeks of price history; this item has {have:.1f} days so far."
            )
            return
        self.timing_note.setText(
            f"Over {profile.days:.0f} days, prices ran highest on {profile.best_weekday} "
            f"({profile.weekday_spread_pct:+.0f}% against the average) and around "
            f"{profile.best_hour:02d}:00 local time. That is when a listing fetches most."
        )
        names = list(profile.by_weekday)
        self.timing_plot.addItem(
            pg.BarGraphItem(x=list(range(len(names))), height=list(profile.by_weekday.values()),
                            width=0.6, brush=self.palette().highlight().color(), pen=None)
        )
        self.timing_plot.getAxis("bottom").setTicks([list(enumerate(names))])

    def _draw_depth(self, view) -> None:
        self.depth_plot.clear()
        ladder = self.state.ladder(view.item_key)
        if ladder is None or not ladder.levels:
            self.depth_note.setText("No depth data for this item.")
            return
        old = ladder.captured_at is None or utcnow() - ladder.captured_at > timedelta(hours=2)
        self.depth_note.setText(
            f"{ladder.units_total:,} units in {ladder.listings_total} listings, polled "
            f"{tables.when(ladder.captured_at)}" + (" (old: do not rely on it)" if old else "")
        )
        x = [0] + [level.cum_units for level in ladder.levels]
        y = [level.unit_price for level in ladder.levels]
        self.depth_plot.plot(x, y, stepMode="center", pen=pg.mkPen(self.palette().highlight().color(), width=2), fillLevel=0, brush=(128, 128, 128, 50))

    def _draw_sales(self, view) -> None:
        self.sales_plot.clear()
        days = self.state.db.sales_days(self.state.server_id, view.item_key)
        if not days:
            self.sales_note.setText(
                "The API has no sales statistics for this item on this server."
                if not view.sales.available
                else "Sales history not loaded yet."
            )
            return
        self.sales_note.setText(
            f"{tables.kina(view.sales.units_sold)} sold in the last 28 days, average "
            f"{tables.kina(view.sales.avg_price)} (range {tables.kina(view.sales.min_price)} to "
            f"{tables.kina(view.sales.max_price)})"
        )
        x = [parse_iso(d["registered"]).timestamp() for d in days]
        self.sales_plot.addItem(pg.BarGraphItem(x=x, height=[d["units_sold"] for d in days], width=86400 * 0.7, brush=self.palette().highlight().color(), pen=None))


def _weekends(start: datetime, end: datetime) -> list[tuple[float, float]]:
    """(start, end) timestamps of every Saturday-Sunday block in the range, in UTC."""
    day = start.astimezone(UTC).replace(hour=0, minute=0, second=0, microsecond=0)
    bands = []
    while day <= end:
        if day.weekday() == 5:
            bands.append((day.timestamp(), (day + timedelta(days=2)).timestamp()))
        day += timedelta(days=1)
    return bands
