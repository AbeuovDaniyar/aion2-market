"""Watchlist and merge, Trades, and Tools (sell vs. use, farming, event tags)."""

from __future__ import annotations

from datetime import datetime

from PySide6.QtCore import QDate, Qt, Signal
from PySide6.QtWidgets import (
    QCheckBox,
    QComboBox,
    QCompleter,
    QDateEdit,
    QDialog,
    QDialogButtonBox,
    QDoubleSpinBox,
    QFormLayout,
    QGroupBox,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QMessageBox,
    QPushButton,
    QSpinBox,
    QTableWidget,
    QVBoxLayout,
    QWidget,
)

from app.analysis import alerts, farming, ledger, service
from app.analysis.fees import Fees
from app.db.repo import iso, parse_iso, utcnow
from app.ui import tables
from app.ui.icons import item_icon
from app.ui.state import AppState
from app.ui.tables import Column, Table

BIG = 2_000_000_000


def buttons_row(*buttons: QPushButton) -> QHBoxLayout:
    row = QHBoxLayout()
    for button in buttons:
        row.addWidget(button)
    row.addStretch(1)
    return row


def item_box(state: AppState, key: int | None = None) -> QComboBox:
    """A searchable drop-down of every item on the server."""
    box = QComboBox(editable=True)
    box.setInsertPolicy(QComboBox.InsertPolicy.NoInsert)
    for view in sorted(state.views, key=lambda v: v.name or ""):
        box.addItem(item_icon(view.section, view.category_name), view.name or str(view.item_key), view.item_key)
    box.completer().setFilterMode(Qt.MatchFlag.MatchContains)
    box.completer().setCompletionMode(QCompleter.CompletionMode.PopupCompletion)
    box.setCurrentIndex(box.findData(key) if key else -1)
    return box


def box_key(box: QComboBox) -> int | None:
    index = box.findText(box.currentText())
    return box.itemData(index) if index >= 0 else None


def kina_box(value: int = 0) -> QSpinBox:
    box = QSpinBox(minimum=0, maximum=BIG, value=int(value or 0))
    box.setGroupSeparatorShown(True)
    return box


# -- Watchlist and merge ----------------------------------------------------------------


class WatchlistTab(QWidget):
    open_item = Signal(int)

    def __init__(self, state: AppState, parent=None):
        super().__init__(parent)
        self.state = state
        self.merge = QCheckBox("The cross-server market has opened (data from now on counts as post-merge)")
        self.merge.clicked.connect(self._toggle_merge)
        self.table = Table(
            [
                Column("Item", lambda r: r.view.name or str(r.view.item_key), icon=lambda r: state.icon(r.view.item_key)),
                Column("Watched", lambda r: "yes" if r.watched else ""),
                Column("Cheapest", lambda r: r.view.cheapest, tables.kina),
                Column("Est. sale", lambda r: r.view.estimate.price, tables.kina),
                Column("Region median", lambda r: r.view.region.regional_median if r.view.region else None, tables.kina),
                Column("Region gap", lambda r: r.view.region.gap_pct if r.view.region else None, tables.pct,
                       "Negative: cheaper here than on the region's other servers"),
                Column("Ref. sale avg", lambda r: r.view.demand.sold_avg if r.view.demand else None, tables.kina),
                Column("Held", lambda r: r.holding.qty if r.holding else None, tables.kina),
                Column("Avg cost", lambda r: r.avg_cost, tables.kina),
                Column("Value now", lambda r: r.holding.value if r.holding else None, tables.kina,
                       "Held quantity at the estimated sale price, after fees"),
                Column("Profit", lambda r: r.holding.profit if r.holding else None, tables.kina),
                Column("Pre-merge median", lambda r: r.shift.pre_median if r.shift else None, tables.kina),
                Column("Post-merge median", lambda r: r.shift.post_median if r.shift else None, tables.kina),
                Column("Merge change", lambda r: r.shift.change_pct if r.shift else None, tables.pct),
            ],
            dim=lambda r: not r.view.usable,
        )
        self.table.selectionModel().selectionChanged.connect(self._selected)
        self.table.doubleClicked.connect(lambda *_: self._open())
        self.servers = QLabel("Select an item to see its price on each reference server.")
        self.servers.setWordWrap(True)
        self.total = QLabel()
        hold = QPushButton("Set holding…")
        unwatch = QPushButton("Stop watching")
        opened = QPushButton("Open item")
        hold.clicked.connect(self._hold)
        unwatch.clicked.connect(self._unwatch)
        opened.clicked.connect(self._open)
        layout = QVBoxLayout(self)
        layout.addWidget(self.merge)
        layout.addWidget(self.table, 1)
        layout.addWidget(self.total)
        layout.addWidget(self.servers)
        layout.addLayout(buttons_row(hold, unwatch, opened))
        state.changed.connect(self.refresh)

    def refresh(self) -> None:
        if self.state.server_id is None:
            return
        rows = service.watch_rows(self.state.db, self.state.views, self.state.server_id)
        self.table.set_rows(rows, keep=lambda r: r.view.item_key)
        self.merge.setChecked(self.state.db.get_setting("cross_server_open") == "1")
        held = [r.holding for r in rows if r.holding]
        self.total.setText(
            f"Holding {len(held)} items: cost {sum(h.cost for h in held):,.0f}, value now "
            f"{sum(h.value for h in held):,.0f}, profit {sum(h.profit for h in held):,.0f} Kina."
            if held
            else "Watch items from the Items tab; set a holding here for stock you are keeping."
        )

    def _selected(self) -> None:
        row = self.table.selected()
        if row is None:
            return
        prices = service.server_prices(self.state.db, self.state.server_id, row.view.item_key)
        if not prices:
            self.servers.setText("No reference server lists this item.")
            return
        self.servers.setText(
            f"<b>{row.view.name}</b> here: {row.view.cheapest:,.0f}. Other servers, cheapest first: "
            + " · ".join(f"{name} {price:,.0f} ({listings})" for name, price, listings in prices)
        )

    def _toggle_merge(self, checked: bool) -> None:
        question = (
            "Mark the cross-server market as open from now? Prices stored before this moment count as pre-merge."
            if checked
            else "Clear the cross-server mark? The pre/post-merge comparison will be removed."
        )
        if QMessageBox.question(self, "Cross-server market", question) == QMessageBox.StandardButton.Yes:
            service.set_merge_open(self.state.db, self.state.server_id, checked)
        self.state.reload()

    def _hold(self) -> None:
        row = self.table.selected()
        if row is None:
            return
        dialog = QDialog(self)
        dialog.setWindowTitle(f"Holding: {row.view.name}")
        qty = QSpinBox(minimum=0, maximum=BIG, value=row.holding.qty if row.holding else 0)
        cost = kina_box(row.avg_cost or 0)
        form = QFormLayout(dialog)
        form.addRow("Quantity held", qty)
        form.addRow("Average cost per unit (Kina)", cost)
        box = QDialogButtonBox(QDialogButtonBox.StandardButton.Save | QDialogButtonBox.StandardButton.Cancel)
        box.accepted.connect(dialog.accept)
        box.rejected.connect(dialog.reject)
        form.addRow(box)
        if dialog.exec():
            self.state.db.set_holding(row.view.item_key, qty.value(), cost.value() or None)
            self.state.reload()

    def _unwatch(self) -> None:
        row = self.table.selected()
        if row:
            self.state.db.set_watch(row.view.item_key, False)
            self.state.reload()

    def _open(self) -> None:
        row = self.table.selected()
        if row:
            self.open_item.emit(row.view.item_key)


# -- Trades -----------------------------------------------------------------------------


class TradeDialog(QDialog):
    def __init__(self, state: AppState, trade=None, parent=None):
        super().__init__(parent)
        self.setWindowTitle("Trade")
        self.state = state
        self._id = trade["id"] if trade else None
        self.item = item_box(state, trade["item_key"] if trade else None)
        self.side = QComboBox()
        self.side.addItems(["buy", "sell"])
        self.side.setCurrentText(trade["side"] if trade else "buy")
        self.qty = QSpinBox(minimum=1, maximum=BIG, value=trade["qty"] if trade else 1)
        self.price = kina_box(trade["unit_price"] if trade else 0)
        self.fee = kina_box(trade["fee_paid"] or 0 if trade else 0)
        self.auto_fee = QCheckBox("Work out the fee from Settings")
        self.auto_fee.setChecked(trade is None or trade["fee_paid"] is None)
        self.listed = QCheckBox("Still listed (not sold yet)")
        self.listed.setChecked(bool(trade) and trade["done_at"] is None)
        when = parse_iso(trade["done_at"] or trade["listed_at"]) if trade else utcnow()
        local = when.astimezone()
        self.date = QDateEdit(QDate(local.year, local.month, local.day), calendarPopup=True)
        self.notes = QLineEdit((trade["notes"] or "") if trade else "")
        for widget in (self.side, self.qty, self.price, self.auto_fee, self.listed):
            signal = getattr(widget, "currentTextChanged", None) or getattr(widget, "valueChanged", None) or widget.toggled
            signal.connect(self._sync)
        form = QFormLayout(self)
        form.addRow("Item", self.item)
        form.addRow("Side", self.side)
        form.addRow("Quantity", self.qty)
        form.addRow("Unit price (Kina)", self.price)
        form.addRow("", self.auto_fee)
        form.addRow("Total fee paid (Kina)", self.fee)
        form.addRow("", self.listed)
        form.addRow("Date", self.date)
        form.addRow("Notes", self.notes)
        box = QDialogButtonBox(QDialogButtonBox.StandardButton.Save | QDialogButtonBox.StandardButton.Cancel)
        box.accepted.connect(self._save)
        box.rejected.connect(self.reject)
        form.addRow(box)
        self._sync()

    def _sync(self) -> None:
        selling = self.side.currentText() == "sell"
        self.listed.setEnabled(selling)
        self.fee.setEnabled(not self.auto_fee.isChecked())
        if self.auto_fee.isChecked():
            fees = Fees.from_db(self.state.db)
            gross = self.qty.value() * self.price.value()
            self.fee.setValue(round(gross - self.qty.value() * fees.net(self.price.value())) if selling else 0)

    def _save(self) -> None:
        key = box_key(self.item)
        if key is None:
            QMessageBox.warning(self, "Trade", "Pick the item from the list.")
            return
        if self.price.value() <= 0:
            QMessageBox.warning(self, "Trade", "Enter the unit price.")
            return
        day = self.date.date()
        at = datetime(day.year(), day.month(), day.day(), 12).astimezone()
        listed = self.side.currentText() == "sell" and self.listed.isChecked()
        fields = dict(
            item_key=key, side=self.side.currentText(), qty=self.qty.value(),
            unit_price=self.price.value(), fee_paid=None if listed else self.fee.value(),
            listed_at=iso(at), done_at=None if listed else iso(at),
            notes=self.notes.text().strip() or None,
        )
        if self._id:
            self.state.db.update_trade(self._id, **fields)
        else:
            trade_id = self.state.db.add_trade(key, fields["side"], fields["qty"], fields["unit_price"], at, done=not listed, notes=fields["notes"])
            self.state.db.update_trade(trade_id, fee_paid=fields["fee_paid"])
        self.accept()


class TradesTab(QWidget):
    def __init__(self, state: AppState, parent=None):
        super().__init__(parent)
        self.state = state
        self._profit: dict[int, ledger.Realized] = {}
        self.table = Table(
            [
                Column("Date", lambda t: (t["done_at"] or t["listed_at"] or "")[:10]),
                Column("Item", lambda t: t["name"] or str(t["item_key"]), icon=lambda t: state.icon(t["item_key"])),
                Column("Side", lambda t: t["side"]),
                Column("Qty", lambda t: t["qty"], tables.kina),
                Column("Unit price", lambda t: t["unit_price"], tables.kina),
                Column("Fee", lambda t: t["fee_paid"], tables.kina),
                Column("Status", self._status),
                Column("Realized profit", lambda t: self._profit[t["id"]].profit if t["id"] in self._profit else None, tables.kina,
                       "Sale proceeds after fees, minus the cost of the matching buys (oldest first)"),
                Column("Notes", lambda t: t["notes"]),
            ]
        )
        self.table.doubleClicked.connect(lambda *_: self._edit())
        self.weeks = Table([Column("Week", lambda r: r[0]), Column("Realized profit", lambda r: r[1], tables.kina)])
        self.items = Table([Column("Item", lambda r: r[0]), Column("Realized profit", lambda r: r[1], tables.kina)])
        self.summary = QLabel()
        add = QPushButton("Add trade")
        edit = QPushButton("Edit")
        sold = QPushButton("Mark sold")
        delete = QPushButton("Delete")
        add.clicked.connect(lambda: self._dialog())
        edit.clicked.connect(self._edit)
        sold.clicked.connect(self._sold)
        delete.clicked.connect(self._delete)
        totals = QHBoxLayout()
        for title, table in (("By week", self.weeks), ("By item", self.items)):
            box = QGroupBox(title)
            inner = QVBoxLayout(box)
            inner.addWidget(table)
            totals.addWidget(box)
        layout = QVBoxLayout(self)
        layout.addWidget(self.summary)
        layout.addWidget(self.table, 3)
        layout.addLayout(buttons_row(add, edit, sold, delete))
        layout.addLayout(totals, 2)
        state.changed.connect(self.refresh)

    def _status(self, trade) -> str:
        if trade["done_at"]:
            realized = self._profit.get(trade["id"])
            if realized and realized.unmatched_qty:
                return f"sold ({realized.unmatched_qty} with no recorded buy)"
            return "sold" if trade["side"] == "sell" else "bought"
        view = self.state.by_key.get(trade["item_key"])
        if view and alerts.is_undercut(trade["unit_price"], view.cheapest):
            return f"listed — undercut, cheapest is {view.cheapest:,.0f}"
        return "listed"

    def refresh(self) -> None:
        db = self.state.db
        rows = service.realized(db)
        self._profit = {r.trade_id: r for r in rows}
        trades = db.trades()
        self.table.set_rows(trades, keep=lambda t: t["id"])
        names = {t["item_key"]: t["name"] or str(t["item_key"]) for t in trades}
        self.weeks.set_rows(list(ledger.by_week(rows).items()))
        self.items.set_rows([(names.get(k, str(k)), v) for k, v in ledger.by_item(rows).items()])
        active = sum(1 for t in trades if t["side"] == "sell" and not t["done_at"])
        self.summary.setText(
            f"{len(trades)} trades, {active} active listings. Total realized profit: "
            f"{sum(r.profit for r in rows):,.0f} Kina."
        )

    def _dialog(self, trade=None) -> None:
        if not self.state.views:
            QMessageBox.information(self, "Trade", "Wait for the first refresh so the item list is loaded.")
            return
        if TradeDialog(self.state, trade, self).exec():
            self.state.reload()

    def _edit(self) -> None:
        trade = self.table.selected()
        if trade is not None:
            self._dialog(trade)

    def _sold(self) -> None:
        trade = self.table.selected()
        if trade is None or trade["side"] != "sell" or trade["done_at"]:
            return
        fees = Fees.from_db(self.state.db)
        fee = round(trade["qty"] * trade["unit_price"] - trade["qty"] * fees.net(trade["unit_price"]))
        self.state.db.update_trade(trade["id"], done_at=iso(utcnow()), fee_paid=fee)
        self.state.reload()

    def _delete(self) -> None:
        trade = self.table.selected()
        if trade is not None and QMessageBox.question(self, "Delete trade", "Delete this trade?") == QMessageBox.StandardButton.Yes:
            self.state.db.delete_trade(trade["id"])
            self.state.reload()


# -- Tools ------------------------------------------------------------------------------


class FarmRunDialog(QDialog):
    def __init__(self, state: AppState, routes: list[str], parent=None):
        super().__init__(parent)
        self.setWindowTitle("Farming run")
        self.resize(480, 420)
        self.state = state
        self.route = QComboBox(editable=True)
        self.route.addItems(routes)
        self.minutes = QDoubleSpinBox(minimum=1, maximum=1440, value=30, suffix=" min")
        self.kina = kina_box()
        self.drops = QTableWidget(0, 2)
        self.drops.setHorizontalHeaderLabels(["Drop", "Quantity"])
        self.drops.setColumnWidth(0, 280)
        add = QPushButton("Add drop")
        remove = QPushButton("Remove selected")
        add.clicked.connect(self._add)
        remove.clicked.connect(lambda: self.drops.removeRow(self.drops.currentRow()))
        form = QFormLayout()
        form.addRow("Route", self.route)
        form.addRow("Duration", self.minutes)
        form.addRow("Raw Kina picked up", self.kina)
        box = QDialogButtonBox(QDialogButtonBox.StandardButton.Save | QDialogButtonBox.StandardButton.Cancel)
        box.accepted.connect(self._save)
        box.rejected.connect(self.reject)
        layout = QVBoxLayout(self)
        layout.addLayout(form)
        layout.addWidget(self.drops, 1)
        layout.addLayout(buttons_row(add, remove))
        layout.addWidget(box)

    def _add(self) -> None:
        row = self.drops.rowCount()
        self.drops.insertRow(row)
        self.drops.setCellWidget(row, 0, item_box(self.state))
        self.drops.setCellWidget(row, 1, QSpinBox(minimum=1, maximum=BIG, value=1))

    def _save(self) -> None:
        totals: dict[int, int] = {}
        for row in range(self.drops.rowCount()):
            key = box_key(self.drops.cellWidget(row, 0))
            if key is None:
                QMessageBox.warning(self, "Farming run", f"Pick the drop from the list in row {row + 1}.")
                return
            totals[key] = totals.get(key, 0) + self.drops.cellWidget(row, 1).value()
        route = self.route.currentText().strip()
        if not route:
            QMessageBox.warning(self, "Farming run", "Name the route.")
            return
        self.state.db.add_farm_run(route, utcnow(), self.minutes.value(), self.kina.value(), list(totals.items()))
        self.accept()


class ToolsTab(QWidget):
    def __init__(self, state: AppState, parent=None):
        super().__init__(parent)
        self.state = state

        # Sell vs. use / deliver
        self.sell_item = item_box(state)
        self.sell_item.setMinimumWidth(280)
        self.alternative = kina_box()
        self.verdict = QLabel("Pick an item and enter what the alternative is worth.")
        self.verdict.setWordWrap(True)
        self.sell_item.currentIndexChanged.connect(self._compare)
        self.alternative.valueChanged.connect(self._compare)
        sell = QGroupBox("Sell vs. use or deliver")
        sell_form = QFormLayout(sell)
        sell_form.addRow("Item", self.sell_item)
        sell_form.addRow("The alternative pays (Kina: supply-order reward, value of keeping it…)", self.alternative)
        sell_form.addRow(self.verdict)

        # Farming
        self.routes = Table(
            [
                Column("Route", lambda r: r.route),
                Column("Runs", lambda r: r.runs),
                Column("Hours", lambda r: round(r.hours, 1)),
                Column("Value", lambda r: r.value, tables.kina, "Drops at net market prices plus raw Kina"),
                Column("Kina per hour", lambda r: r.per_hour, tables.kina),
            ]
        )
        self.runs = Table(
            [
                Column("Date", lambda r: r[0]["at"][:10]),
                Column("Route", lambda r: r[0]["route"]),
                Column("Minutes", lambda r: r[0]["minutes"]),
                Column("Value", lambda r: r[1].value, tables.kina),
                Column("Kina per hour", lambda r: r[1].per_hour, tables.kina),
                Column("Unpriced drops", lambda r: r[1].unpriced or None),
            ]
        )
        add_run = QPushButton("Log a run")
        delete_run = QPushButton("Delete run")
        add_run.clicked.connect(self._add_run)
        delete_run.clicked.connect(self._delete_run)
        farm = QGroupBox("Farming value per hour")
        farm_box = QVBoxLayout(farm)
        tables_row = QHBoxLayout()
        tables_row.addWidget(self.routes, 1)
        tables_row.addWidget(self.runs, 1)
        farm_box.addLayout(tables_row)
        farm_box.addLayout(buttons_row(add_run, delete_run))

        # Event tags
        self.events = Table([Column("Date", lambda e: e["day"]), Column("Kind", lambda e: e["kind"]), Column("Label", lambda e: e["label"])])
        today = utcnow().astimezone()
        self.event_date = QDateEdit(QDate(today.year, today.month, today.day), calendarPopup=True)
        self.event_kind = QComboBox()
        self.event_kind.addItems(["patch", "event", "merge", "other"])
        self.event_label = QLineEdit(placeholderText="What happened")
        add_event = QPushButton("Add tag")
        delete_event = QPushButton("Delete tag")
        add_event.clicked.connect(self._add_event)
        delete_event.clicked.connect(self._delete_event)
        tags = QGroupBox("Event tags (shown as lines on price charts)")
        tags_box = QVBoxLayout(tags)
        tags_box.addWidget(self.events)
        entry = QHBoxLayout()
        for widget in (self.event_date, self.event_kind):
            entry.addWidget(widget)
        entry.addWidget(self.event_label, 1)
        entry.addWidget(add_event)
        entry.addWidget(delete_event)
        tags_box.addLayout(entry)

        layout = QVBoxLayout(self)
        layout.addWidget(sell)
        layout.addWidget(farm, 2)
        layout.addWidget(tags, 1)
        state.changed.connect(self.refresh)

    def refresh(self) -> None:
        if self.sell_item.count() != len(self.state.views):
            key = box_key(self.sell_item)
            self.sell_item.blockSignals(True)
            self.sell_item.clear()
            for view in sorted(self.state.views, key=lambda v: v.name or ""):
                self.sell_item.addItem(item_icon(view.section, view.category_name), view.name or str(view.item_key), view.item_key)
            self.sell_item.setCurrentIndex(self.sell_item.findData(key) if key else -1)
            self.sell_item.blockSignals(False)
        fees = Fees.from_db(self.state.db)
        by_key = self.state.by_key

        def price(key: int) -> float | None:
            return by_key[key].estimate.price if key in by_key else None

        valued = [
            (run, farming.run_value(run["minutes"], run["kina"], [(d["item_key"], d["qty"]) for d in drops], price, fees))
            for run, drops in self.state.db.farm_runs()
        ]
        self.runs.set_rows(valued, keep=lambda r: r[0]["id"])
        self.routes.set_rows(farming.rank_routes((r["route"], r["minutes"], v) for r, v in valued))
        self.events.set_rows(self.state.db.events(), keep=lambda e: e["id"])
        self._compare()

    def _compare(self) -> None:
        view = self.state.by_key.get(box_key(self.sell_item))
        if view is None:
            return
        result = alerts.sell_vs_alternative(view.estimate.price, self.alternative.value(), Fees.from_db(self.state.db))
        better = "Sell it" if result.better == "sell" else "Use or deliver it"
        self.verdict.setText(
            f"<b>{better}</b>: selling nets {result.sell_net:,.0f} Kina after fees (estimated sale price "
            f"{view.estimate.price:,.0f}, {view.estimate.confidence} confidence) against "
            f"{result.alternative:,.0f}. Difference: {result.difference:,.0f} Kina."
        )

    def _add_run(self) -> None:
        routes = sorted({run["route"] for run, _ in self.state.db.farm_runs() if run["route"]})
        if FarmRunDialog(self.state, routes, self).exec():
            self.state.reload()

    def _delete_run(self) -> None:
        row = self.runs.selected()
        if row is not None:
            self.state.db.delete_farm_run(row[0]["id"])
            self.state.reload()

    def _add_event(self) -> None:
        label = self.event_label.text().strip()
        if not label:
            return
        self.state.db.add_event(self.event_date.date().toString("yyyy-MM-dd"), label, self.event_kind.currentText())
        self.event_label.clear()
        self.state.reload()

    def _delete_event(self) -> None:
        event = self.events.selected()
        if event is not None:
            self.state.db.delete_event(event["id"])
            self.state.reload()
