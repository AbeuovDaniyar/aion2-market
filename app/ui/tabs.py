"""Opportunities, Crafting, Flips, Trust and Settings tabs."""

from __future__ import annotations

from PySide6.QtCore import Qt, Signal
from PySide6.QtGui import QIcon
from PySide6.QtWidgets import (
    QCheckBox,
    QApplication,
    QComboBox,
    QCompleter,
    QDialog,
    QDialogButtonBox,
    QDoubleSpinBox,
    QFileDialog,
    QFormLayout,
    QGroupBox,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QMessageBox,
    QPushButton,
    QScrollArea,
    QSpinBox,
    QTableWidget,
    QTextBrowser,
    QVBoxLayout,
    QWidget,
)

from app.analysis import service

# Lowest to highest, for sorting the grade filter.
GRADE_ORDER = {"Common": 0, "Rare": 1, "Legend": 2, "Unique": 3, "Epic": 4}
from app.db.repo import DEFAULT_SETTINGS, utcnow
from app.ui import tables
from app.ui.state import AppState
from app.ui.tables import Column, Table


def _buttons(*buttons: QPushButton) -> QHBoxLayout:
    row = QHBoxLayout()
    for button in buttons:
        row.addWidget(button)
    row.addStretch(1)
    return row


# -- Opportunities ----------------------------------------------------------------------


class OpportunitiesTab(QWidget):
    open_item = Signal(int)

    def __init__(self, state: AppState, parent=None):
        super().__init__(parent)
        self.state = state
        self.paused = QLabel()
        self.paused.setWordWrap(True)
        self.table = Table(
            [
                Column("Type", lambda o: o.kind),
                Column("Item", lambda o: o.name, icon=lambda o: state.icon(o.item_key)),
                Column("Expected profit", lambda o: o.profit, tables.kina,
                       "Within the horizon set in Settings. Blank where no number can be put on it"),
                Column("Confidence", lambda o: o.confidence),
                Column("Why", lambda o: o.detail),
            ],
            dim=lambda o: o.confidence == "low",
        )
        self.table.doubleClicked.connect(self._open)
        self.open = QPushButton("Open item")
        self.watch = QPushButton("Add to watchlist")
        self.dismiss = QPushButton("Dismiss")
        self.restore = QPushButton("Restore dismissed")
        self.open.clicked.connect(self._open)
        self.watch.clicked.connect(self._watch)
        self.dismiss.clicked.connect(self._dismiss)
        self.restore.clicked.connect(state.restore_dismissed)
        layout = QVBoxLayout(self)
        layout.addWidget(self.paused)
        layout.addWidget(self.table)
        layout.addLayout(_buttons(self.open, self.watch, self.dismiss, self.restore))
        state.changed.connect(self.refresh)

    def refresh(self) -> None:
        self.table.set_rows(self.state.opportunities, keep=lambda o: o.id)
        f = self.state.freshness
        if f and f.alerts_paused:
            self.paused.setText(f"Data is not fresh ({f.state}): {f.detail}. Stale items are left out.")
        elif not self.state.opportunities:
            self.paused.setText(
                "Nothing yet. Flips need 3+ days of stored price history, so keep the app running."
            )
        else:
            self.paused.setText(
                "Greyed rows are low confidence: thin history, no sales data, or a single price."
            )
        self.restore.setVisible(bool(self.state.dismissed()))

    def _open(self, *_) -> None:
        row = self.table.selected()
        if row and row.item_key in self.state.by_key:
            self.open_item.emit(row.item_key)

    def _watch(self) -> None:
        row = self.table.selected()
        if row and row.item_key in self.state.by_key:
            self.state.db.set_watch(row.item_key)

    def _dismiss(self) -> None:
        row = self.table.selected()
        if row:
            self.state.dismiss(row)


# -- Crafting ---------------------------------------------------------------------------


class RecipeDialog(QDialog):
    """Add or edit one of Danny's own recipes."""

    def __init__(self, state: AppState, recipe=None, inputs=(), parent=None):
        super().__init__(parent)
        self.setWindowTitle("Recipe")
        self.resize(520, 520)
        self.state = state
        self._id = recipe["id"] if recipe else None
        self._names = {v.item_key: v.name or str(v.item_key) for v in state.views}

        self.output = self._item_box(recipe["output_key"] if recipe else None)
        self.output_qty = QSpinBox(minimum=1, maximum=9999, value=(recipe["output_qty"] or 1) if recipe else 1)
        self.success = QDoubleSpinBox(minimum=0, maximum=100, decimals=2, suffix=" %")
        self.success.setValue(100 * (recipe["success_rate"] or 0) if recipe else 0)
        self.success.setSpecialValueText("unknown")
        self.combo = QDoubleSpinBox(minimum=0, maximum=100, decimals=2, suffix=" %")
        self.combo.setValue(100 * (recipe["combo_probability"] or 0) if recipe else 0)
        self.combo_output = self._item_box(recipe["combo_output_key"] if recipe else None, optional=True)
        self.combo_qty = QSpinBox(minimum=0, maximum=9999, value=(recipe["combo_output_qty"] or 0) if recipe else 0)
        self.combo_qty.setSpecialValueText("same as normal")
        self.fee = QSpinBox(minimum=0, maximum=2_000_000_000, value=(recipe["kina_cost"] or 0) if recipe else 0)
        self.fee.setGroupSeparatorShown(True)
        self.seconds = QSpinBox(minimum=0, maximum=86400, value=(recipe["craft_seconds"] or 0) if recipe else 0, suffix=" s")
        self.profession = QLineEdit((recipe["profession"] or "") if recipe else "")

        form = QFormLayout()
        form.addRow("Output item", self.output)
        form.addRow("Output quantity", self.output_qty)
        form.addRow("Success rate (in-game Expected Probability)", self.success)
        form.addRow("COMBO probability", self.combo)
        form.addRow("COMBO output item", self.combo_output)
        form.addRow("COMBO quantity", self.combo_qty)
        form.addRow("Crafting fee (Kina)", self.fee)
        form.addRow("Craft time", self.seconds)
        form.addRow("Profession", self.profession)

        self.inputs = QTableWidget(0, 3)
        self.inputs.setHorizontalHeaderLabels(["Material", "Qty per attempt", "NPC price (0 = none)"])
        self.inputs.horizontalHeader().setStretchLastSection(True)
        self.inputs.setColumnWidth(0, 240)
        for line in inputs:
            self._add_input(line["item_key"], line["qty"], line["npc_price"])
        add = QPushButton("Add material")
        remove = QPushButton("Remove selected")
        add.clicked.connect(lambda: self._add_input())
        remove.clicked.connect(lambda: self.inputs.removeRow(self.inputs.currentRow()))

        buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Save | QDialogButtonBox.StandardButton.Cancel)
        buttons.accepted.connect(self._save)
        buttons.rejected.connect(self.reject)
        layout = QVBoxLayout(self)
        layout.addLayout(form)
        layout.addWidget(self.inputs, 1)
        layout.addLayout(_buttons(add, remove))
        layout.addWidget(buttons)

    def _item_box(self, key: int | None, optional: bool = False) -> QComboBox:
        box = QComboBox(editable=True)
        box.setInsertPolicy(QComboBox.InsertPolicy.NoInsert)
        if optional:
            box.addItem("(same item)", None)
        for item_key, name in sorted(self._names.items(), key=lambda kv: kv[1]):
            box.addItem(self.state.icon(item_key) or QIcon(), name, item_key)
        box.completer().setFilterMode(Qt.MatchFlag.MatchContains)
        box.completer().setCompletionMode(QCompleter.CompletionMode.PopupCompletion)
        index = box.findData(key) if key else (0 if optional else -1)
        box.setCurrentIndex(index)
        return box

    def _add_input(self, key=None, qty=1, npc=None) -> None:
        row = self.inputs.rowCount()
        self.inputs.insertRow(row)
        self.inputs.setCellWidget(row, 0, self._item_box(key))
        self.inputs.setCellWidget(row, 1, QSpinBox(minimum=1, maximum=99999, value=qty or 1))
        price = QSpinBox(minimum=0, maximum=2_000_000_000, value=npc or 0)
        self.inputs.setCellWidget(row, 2, price)

    @staticmethod
    def _key(box: QComboBox) -> int | None:
        index = box.findText(box.currentText())
        return box.itemData(index) if index >= 0 else None

    def _save(self) -> None:
        output = self._key(self.output)
        lines = []
        for row in range(self.inputs.rowCount()):
            key = self._key(self.inputs.cellWidget(row, 0))
            if key is None:
                QMessageBox.warning(self, "Recipe", f"Pick a material from the list in row {row + 1}.")
                return
            lines.append((key, self.inputs.cellWidget(row, 1).value(), self.inputs.cellWidget(row, 2).value() or None))
        if output is None:
            QMessageBox.warning(self, "Recipe", "Pick the output item from the list.")
            return
        if len({k for k, _, _ in lines}) != len(lines):
            QMessageBox.warning(self, "Recipe", "Each material can only be listed once.")
            return
        rate_known = self.success.value() > 0
        self.state.db.save_recipe(
            {
                "id": self._id,
                "output_key": output,
                "output_qty": self.output_qty.value(),
                "success_rate": self.success.value() / 100 if rate_known else None,
                "combo_probability": self.combo.value() / 100,
                "combo_output_key": self._key(self.combo_output),
                "combo_output_qty": self.combo_qty.value() or None,
                "kina_cost": self.fee.value(),
                "craft_seconds": self.seconds.value() or None,
                "profession": self.profession.text().strip() or None,
                "rate_source": "manual" if rate_known else None,
                "rate_updated_at": utcnow().isoformat() if rate_known else None,
            },
            lines,
        )
        self.accept()


class CraftingTab(QWidget):
    def __init__(self, state: AppState, parent=None):
        super().__init__(parent)
        self.state = state
        self.api = Table(
            [
                Column("Item", lambda r: r.name or str(r.target_key), icon=lambda r: state.icon(r.target_key)),
                Column("Skill", lambda r: r.skill),
                Column("Expected cost", lambda r: r.api_cost if r.api_cost is not None else r.buy_cost, tables.kina,
                       "Per finished item, over every step's failures and missed COMBOs (API model)"),
                Column("Buying all here", lambda r: r.buy_cost, tables.kina,
                       "The same chain with every material bought at this server's cheapest price"),
                Column("Est. sale", lambda r: r.sale_price, tables.kina),
                Column("Profit", lambda r: r.profit.profit if r.profit else None, tables.kina),
                Column("Margin", lambda r: r.profit.margin_pct if r.profit else None, tables.pct),
                Column("Break-even price", lambda r: r.profit.break_even_price if r.profit else None, tables.kina),
                Column("Attempts per step", lambda r: " / ".join(f"{a:.1f}" for a in r.attempts)),
                Column("1-in-20 streak budget", lambda r: r.profit.streak_budget if r.profit else None, tables.kina,
                       "Kina needed at the last step to survive a 1-in-20 run without the result"),
                Column("Flags", lambda r: tables.flags(r.flags)),
            ],
            dim=lambda r: r.profit is None or "low_confidence_price" in r.flags,
        )
        self.mine = Table(
            [
                Column("Output", lambda r: r.name, icon=lambda r: state.icon(r.output_key)),
                Column("Grade", lambda r: r.grade),
                Column("Profession", lambda r: (r.profession or "").capitalize() or None),
                Column("Level", lambda r: r.mastery_level, tip="Mastery level of the recipe"),
                Column("Success", lambda r: r.success_rate, tables.rate),
                Column("Rate from", lambda r: {"manual": "you", "estimated": "estimate"}.get(r.rate_source or "", r.rate_source),
                       "Estimates come from the API model's rates at a similar level; set your own from the in-game Expected Probability tab"),
                Column("Break-even rate", lambda r: r.break_even_rate, tables.rate,
                       "The success rate at which this craft earns nothing"),
                Column("COMBO", lambda r: r.combo_probability, tables.rate),
                Column("Materials", lambda r: r.materials, tables.kina),
                Column("EV per attempt", lambda r: r.ev, tables.kina),
                Column("Margin", lambda r: r.margin_pct, tables.pct),
                Column("Profit per hour", lambda r: r.per_hour, tables.kina),
                Column("1-in-20 streak budget", lambda r: r.streak_budget, tables.kina),
                Column("Saturated", lambda r: "yes" if r.saturated else ""),
                Column("Logged attempts", lambda r: r.logged_attempts or None),
                Column("Logged rate", lambda r: r.logged.observed_rate if r.logged else None, tables.rate,
                       "Your own results; flagged when they run well below the expected rate"),
                Column("Flags", lambda r: tables.flags(r.flags)),
            ],
            dim=lambda r: r.ev is None,
        )
        self.mine.doubleClicked.connect(lambda *_: self._edit())
        add = QPushButton("Add recipe")
        edit = QPushButton("Edit")
        delete = QPushButton("Delete")
        add.clicked.connect(lambda: self._dialog())
        edit.clicked.connect(self._edit)
        delete.clicked.connect(self._delete)
        log = QPushButton("Log craft result…")
        log.clicked.connect(self._log)
        self._log_button = log
        set_rate = QPushButton("Set my success rate…")
        set_rate.clicked.connect(self._set_rate)
        import_button = QPushButton("Import all recipes…")
        import_button.clicked.connect(self._import)

        # Filters for the (long) recipe list.
        self.search = QLineEdit(placeholderText="Search recipes…")
        self.search.setClearButtonEnabled(True)
        self.search.textChanged.connect(self.mine.proxy.setFilterFixedString)
        self.grade = QComboBox()
        self.profession = QComboBox()
        self.priced_only = QCheckBox("Only recipes with every material on the market")
        self.priced_only.setChecked(True)
        for widget in (self.grade, self.profession):
            widget.currentIndexChanged.connect(self._filter)
        self.priced_only.toggled.connect(self._filter)
        self.counts = QLabel()
        filters = QHBoxLayout()
        filters.addWidget(self.search, 2)
        filters.addWidget(QLabel("Grade"))
        filters.addWidget(self.grade)
        filters.addWidget(QLabel("Profession"))
        filters.addWidget(self.profession)
        filters.addWidget(self.priced_only)
        filters.addWidget(self.counts, 1)

        self.note = QLabel()
        self.note.setWordWrap(True)
        layout = QVBoxLayout(self)
        layout.addWidget(QLabel("<b>API recipes</b> (rates at the API model's mastery)"))
        layout.addWidget(self.note)
        layout.addWidget(self.api, 3)
        layout.addWidget(QLabel("<b>All recipes</b> (imported, plus your own; double-click to edit one you added)"))
        layout.addLayout(filters)
        layout.addWidget(self.mine, 4)
        layout.addLayout(_buttons(import_button, add, edit, delete, set_rate, log))
        state.changed.connect(self.refresh)

    def _log(self) -> None:
        row = self.mine.selected()
        if row is None:
            QMessageBox.information(self, "Log craft result", "Select one of your recipes first.")
            return
        dialog = QDialog(self)
        dialog.setWindowTitle(f"Craft result: {row.name}")
        attempts = QSpinBox(minimum=1, maximum=100000, value=10)
        successes = QSpinBox(minimum=0, maximum=100000, value=0)
        combos = QSpinBox(minimum=0, maximum=100000, value=0)
        form = QFormLayout(dialog)
        form.addRow("Attempts", attempts)
        form.addRow("Successes (including COMBOs)", successes)
        form.addRow("COMBOs", combos)
        box = QDialogButtonBox(QDialogButtonBox.StandardButton.Save | QDialogButtonBox.StandardButton.Cancel)
        box.accepted.connect(dialog.accept)
        box.rejected.connect(dialog.reject)
        form.addRow(box)
        if not dialog.exec():
            return
        if successes.value() > attempts.value() or combos.value() > successes.value():
            QMessageBox.warning(self, "Log craft result", "Successes cannot exceed attempts, and COMBOs cannot exceed successes.")
            return
        self.state.db.add_craft_outcome(row.recipe_id, attempts.value(), successes.value(), combos.value(), utcnow())
        self.state.reload()

    def refresh(self) -> None:
        self.api.set_rows(self.state.crafts, keep=lambda r: r.target_key)
        self._fill_filter(self.grade, sorted({r.grade for r in self.state.recipes if r.grade}, key=lambda g: GRADE_ORDER.get(g, 9)))
        self._fill_filter(self.profession, sorted({(r.profession or "").capitalize() for r in self.state.recipes if r.profession}))
        self._filter()
        self.note.setText(
            "" if self.state.crafts else "Craft data has not been fetched yet; it arrives with the next refresh."
        )

    @staticmethod
    def _fill_filter(box: QComboBox, values: list[str]) -> None:
        current = box.currentText()
        if [box.itemText(i) for i in range(1, box.count())] == values:
            return
        box.blockSignals(True)
        box.clear()
        box.addItem("All")
        box.addItems(values)
        box.setCurrentText(current if current in values else "All")
        box.blockSignals(False)

    def _filter(self) -> None:
        grade, profession = self.grade.currentText(), self.profession.currentText()
        rows = [
            r for r in self.state.recipes
            if (grade in ("", "All") or r.grade == grade)
            and (profession in ("", "All") or (r.profession or "").capitalize() == profession)
            and (not self.priced_only.isChecked() or r.materials is not None)
        ]
        self.mine.set_rows(rows, keep=lambda r: r.recipe_id)
        imported = sum(r.imported for r in self.state.recipes)
        self.counts.setText(
            f"{len(rows)} shown of {len(self.state.recipes)} ({imported} imported)"
            if self.state.recipes
            else "No recipes yet: use Import all recipes."
        )

    def _import(self) -> None:
        from app.db.recipes_import import import_graph

        if self.state.server_id is None:
            return
        box = QMessageBox(self)
        box.setWindowTitle("Import all recipes")
        box.setText(
            "Open this link in your browser, then either copy the whole page (Ctrl+A, Ctrl+C) "
            "or save it (Ctrl+S):\n\n"
            "https://gamers4.life/aion-2/database/crafting-graph.json\n\n"
            "The app does not download it itself: that site's terms do not allow automated "
            "access. Importing again replaces the imported recipes but keeps the success "
            "rates you set yourself."
        )
        paste = box.addButton("Paste from clipboard", QMessageBox.ButtonRole.AcceptRole)
        choose = box.addButton("Choose saved file…", QMessageBox.ButtonRole.AcceptRole)
        box.addButton(QMessageBox.StandardButton.Cancel)
        box.exec()
        if box.clickedButton() is paste:
            source = QApplication.clipboard().text()
            if not source.strip().startswith("{"):
                QMessageBox.warning(self, "Import all recipes", "The clipboard does not hold the recipe file. Copy the whole page first.")
                return
        elif box.clickedButton() is choose:
            source, _ = QFileDialog.getOpenFileName(self, "crafting-graph.json", "", "JSON files (*.json)")
            if not source:
                return
        else:
            return
        try:
            result = import_graph(self.state.db, source, self.state.server_id)
        except (OSError, ValueError, KeyError, TypeError) as exc:
            QMessageBox.warning(self, "Import all recipes", f"Could not import that: {exc}")
            return        self.state.reload()
        QMessageBox.information(
            self, "Import all recipes",
            f"Imported {result.recipes:,} recipes for your faction "
            f"({result.skipped_other_race:,} of the other faction and {result.skipped_empty:,} "
            "COMBO-result entries left out). "
            + (f"Kept your own rate on {result.kept_own_rates}. " if result.kept_own_rates else "")
            + "Success rates are estimates until you set your own.",
        )

    def _set_rate(self) -> None:
        row = self.mine.selected()
        if row is None:
            QMessageBox.information(self, "Set my success rate", "Select a recipe first.")
            return
        dialog = QDialog(self)
        dialog.setWindowTitle(f"Success rate: {row.name}")
        rate = QDoubleSpinBox(minimum=0.01, maximum=100, decimals=2, suffix=" %")
        rate.setValue(100 * (row.success_rate or 0.9))
        combo = QDoubleSpinBox(minimum=0, maximum=100, decimals=2, suffix=" %")
        combo.setValue(100 * (row.combo_probability or 0))
        form = QFormLayout(dialog)
        form.addRow("Success rate (in-game Expected Probability)", rate)
        form.addRow("COMBO probability", combo)
        box = QDialogButtonBox(QDialogButtonBox.StandardButton.Save | QDialogButtonBox.StandardButton.Cancel)
        box.accepted.connect(dialog.accept)
        box.rejected.connect(dialog.reject)
        form.addRow(box)
        if dialog.exec():
            self.state.db.conn.execute(
                "UPDATE recipes SET success_rate = ?, combo_probability = ?, rate_source = 'manual', "
                "rate_updated_at = ? WHERE id = ?",
                (rate.value() / 100, combo.value() / 100, utcnow().isoformat(), row.recipe_id),
            )
            self.state.db.conn.commit()
            self.state.reload()

    def _dialog(self, recipe=None, inputs=()) -> None:
        if RecipeDialog(self.state, recipe, inputs, self).exec():
            self.state.reload()

    def _edit(self) -> None:
        row = self.mine.selected()
        if row is None:
            return
        if row.imported:
            self._set_rate()  # imported recipes: only the rates are yours to change
            return
        for recipe, inputs in self.state.db.recipes():
            if recipe["id"] == row.recipe_id:
                self._dialog(recipe, inputs)

    def _delete(self) -> None:
        row = self.mine.selected()
        if row and QMessageBox.question(self, "Delete recipe", f"Delete the recipe for {row.name}?") == QMessageBox.StandardButton.Yes:
            self.state.db.delete_recipe(row.recipe_id)
            self.state.reload()


# -- Flips ------------------------------------------------------------------------------


class FlipsTab(QWidget):
    open_item = Signal(int)

    def __init__(self, state: AppState, parent=None):
        super().__init__(parent)
        self.state = state
        self.note = QLabel()
        self.note.setWordWrap(True)
        self.table = Table(
            [
                Column("Item", lambda c: c.name or str(c.item_key), icon=lambda c: state.icon(c.item_key)),
                Column("Buy price", lambda c: c.buy_price, tables.kina),
                Column("Resale target", lambda c: c.resale_target, tables.kina),
                Column("Capped by", lambda c: c.basis.replace("_", " ")),
                Column("Profit per unit", lambda c: c.profit, tables.kina, "After fees"),
                Column("ROI", lambda c: c.roi_pct, tables.pct),
                Column("Units", lambda c: c.units),
                Column("Days to sell", lambda c: c.days_to_sell, tables.days),
                Column("Profit in horizon", lambda c: c.horizon_profit, tables.kina),
                Column("Confidence", lambda c: c.confidence),
            ],
            dim=lambda c: c.confidence == "low",
        )
        self.table.doubleClicked.connect(lambda *_: self._open())
        bought = QPushButton("Mark as bought")
        opened = QPushButton("Open item")
        bought.clicked.connect(self._bought)
        opened.clicked.connect(self._open)
        layout = QVBoxLayout(self)
        layout.addWidget(self.note)
        layout.addWidget(self.table)
        layout.addLayout(_buttons(bought, opened))
        state.changed.connect(self.refresh)

    def refresh(self) -> None:
        self.table.set_rows(self.state.flips, keep=lambda c: c.item_key)
        db = self.state.db
        self.note.setText(
            f"A listing qualifies when reselling it nets at least {db.get_float('flip_min_profit'):,.0f} "
            f"Kina and {db.get_float('flip_min_roi_pct'):.0f}% after fees. Items need "
            f"{db.get_float('flip_min_days'):.0f}+ days of stored history and "
            f"{db.get_float('flip_min_listings'):.0f}+ listings on average."
            + ("" if self.state.flips else " None qualify right now.")
        )

    def _open(self) -> None:
        row = self.table.selected()
        if row:
            self.open_item.emit(row.item_key)

    def _bought(self) -> None:
        row = self.table.selected()
        if row is None:
            return
        answer = QMessageBox.question(
            self, "Mark as bought",
            f"Record a buy of {row.units} × {row.name} at {row.buy_price:,.0f} Kina in your trades?",
        )
        if answer == QMessageBox.StandardButton.Yes:
            self.state.db.add_trade(row.item_key, "buy", row.units, round(row.buy_price), utcnow(), notes="from Flips tab")


# -- Trust ------------------------------------------------------------------------------


class TrustTab(QWidget):
    def __init__(self, state: AppState, parent=None):
        super().__init__(parent)
        self.state = state
        self.text = QTextBrowser()
        self.errors = Table(
            [
                Column("When", lambda r: r["at"]),
                Column("Endpoint", lambda r: r["endpoint"]),
                Column("Status", lambda r: r["status"]),
                Column("ms", lambda r: r["ms"]),
                Column("Error", lambda r: r["error"]),
            ]
        )
        layout = QVBoxLayout(self)
        layout.addWidget(self.text, 3)
        layout.addWidget(QLabel("<b>Recent failed requests</b>"))
        layout.addWidget(self.errors, 1)
        state.changed.connect(self.refresh)

    def refresh(self) -> None:
        s, f = self.state, self.state.freshness
        views = s.views
        lines = []
        if f:
            lines.append(
                f"<h3>API health: {f.state.upper()}</h3>"
                f"<p>Last listings snapshot: {tables.when(f.snapshot_at) or 'unknown'}"
                f" ({tables.age(utcnow() - f.snapshot_at) if f.snapshot_at else 'unknown'} old)<br>"
                f"Last sales batch: {tables.when(f.sales_batch_at) or 'none for this server'}"
                f"{' (stale)' if f.sales_stale else ''}<br>"
                f"Collector failures since last good run: {f.failed_runs_since_ok}"
                f"{'<br>' + f.detail if f.detail else ''}</p>"
            )
        collection = s.collection()
        if collection:
            lines.append(
                f"<h3>Collector, last {collection.window_hours} hours</h3>"
                f"<p>{collection.ok_runs} of {collection.runs} runs succeeded.</p><ul>"
                + "".join(
                    f"<li>{tables.when(k.from_at)} to {tables.when(k.to_at)}: {k.runs} runs, "
                    f"{k.kind}{' — ' + k.detail[:120] if k.detail and k.kind != 'ok' else ''}</li>"
                    for k in collection.streaks[:8]
                )
                + "</ul>"
            )
        overview = s.overview()
        if overview:
            c = overview.coverage
            lines.append(
                "<h3>Coverage</h3>"
                f"<p>{overview.items} items, {overview.total_listings:,} listings. Collecting since "
                f"{tables.when(c.first_snapshot_at)}; {c.snapshots_24h} listing snapshots and "
                f"{c.stats_observations_24h} sales observations in the last 24 hours.</p>"
            )
        if views:
            confidence = {k: sum(v.estimate.confidence == k for v in views) for k in ("high", "medium", "low")}
            sources = {k: sum(v.liquidity.source == k for v in views) for k in ("sales", "listings_proxy", "reference", "unknown")}
            insane = [v for v in views if v.sanity]
            stale = sum(v.age.listings_stale for v in views)
            lines.append(
                "<h3>This server's data</h3>"
                f"<p>Estimate confidence: {confidence['high']} high, {confidence['medium']} medium, "
                f"{confidence['low']} low.<br>"
                f"Sell-through source: {sources['sales']} real sales, {sources['listings_proxy']} "
                f"listing turnover, {sources['reference']} borrowed from reference servers, "
                f"{sources['unknown']} unknown.<br>"
                f"Items with sales statistics: {sum(v.sales.available for v in views)} of {len(views)}.<br>"
                f"Stale items: {stale}. Sanity flags: {len(insane)}.</p>"
            )
            if insane:
                lines.append(
                    "<ul>"
                    + "".join(f"<li>{v.name}: {tables.flags(v.sanity)} (cheapest {v.cheapest:,.0f}, 7-day median {tables.kina(v.estimate.median_7d)})</li>" for v in insane[:15])
                    + "</ul>"
                )
            references = s.db.reference_server_ids(s.server_id)
            demand = s.db.demand_server_ids(s.server_id)
            lines.append(
                "<h3>Reference servers</h3>"
                f"<p>Price comparison: {len(references)} servers in this region. "
                f"Demand reference: {', '.join(map(str, demand)) or 'none'} "
                f"({sum(1 for v in views if v.demand and v.demand.units_per_day is not None)} of this "
                "server's items have sales data there).</p>"
            )
        lines.append("<h3>Your results against the numbers</h3>")
        checks = service.trade_checks(s.db, views, s.server_id) if s.server_id else []
        large = [c for c in checks if c.gap.large]
        if checks:
            lines.append(
                f"<p>{len(checks)} of your sales compared with the API's sale statistics; "
                f"{len(large)} with a large gap.</p><ul>"
                + "".join(
                    f"<li>{c.name}: you sold at {c.price:,.0f}, API average {c.api_avg:,.0f} "
                    f"({tables.pct(c.gap.gap_pct)}){', outside the API range' if c.gap.outside_range else ''}</li>"
                    for c in large[:15]
                )
                + "</ul>"
            )
        else:
            lines.append(
                "<p>No sales to compare: either none are logged in Trades, or the API has no "
                "sale statistics for those items on this server.</p>"
            )
        logged = [r for r in s.recipes if r.logged]
        if logged:
            lines.append(
                "<ul>"
                + "".join(
                    f"<li>{r.name}: {r.logged_attempts} attempts logged, {tables.rate(r.logged.observed_rate)} "
                    f"success against {tables.rate(r.logged.expected_rate)} expected"
                    f"{' — well below expected' if r.logged.below_expected else ''}</li>"
                    for r in logged
                )
                + "</ul>"
            )
        else:
            lines.append("<p>No craft results logged yet (Crafting tab, Log craft result).</p>")
        self.text.setHtml("".join(lines))
        self.errors.set_rows(s.db.recent_fetches(50, errors_only=True))


# -- Settings ---------------------------------------------------------------------------

SETTINGS = [
    ("Fees", [
        ("sales_tax_pct", "Marketplace commission (%)", "float"),
        ("listing_fee_rule", "Listing fee rule", ["none", "flat", "percent"]),
        ("listing_fee_value", "Listing fee (Kina, or % for the percent rule)", "float"),
    ]),
    ("Refresh", [
        ("refresh_minutes", "Refresh every (minutes, 10 minimum)", "float"),
        ("stale_listings_hours", "Listings are stale after (hours)", "float"),
        ("stale_sales_days", "Sales are stale after (days)", "float"),
        ("collector_failing_runs", "Collector is failing after this many failed runs", "float"),
    ]),
    ("Pricing model", [
        ("horizon_days", "Profit horizon (days)", "float"),
        ("undercut_step", "Undercut step (Kina)", "float"),
        ("min_sales_units", "Sales needed to trust the sale average", "float"),
        ("spike_mads", "Spike filter (MADs)", "float"),
        ("spike_persist_refreshes", "A move is real after this many refreshes", "float"),
    ]),
    ("Flip finder", [
        ("flip_min_profit", "Minimum profit (Kina)", "float"),
        ("flip_min_roi_pct", "Minimum ROI (%)", "float"),
        ("flip_min_days", "Minimum days of history", "float"),
        ("flip_min_listings", "Minimum average listings", "float"),
    ]),
    ("Reference servers", [
        ("reference_scope", "Compare prices with", ["region_faction", "region", "off"]),
        ("region_gap_pct", "Gap that counts as cheap or expensive (%)", "float"),
        ("demand_reference_servers", "Demand-reference server ids (comma separated)", "text"),
        ("reference_refresh_hours", "Refresh reference servers every (hours)", "float"),
        ("history_backfill", "Fetch 7 days of price history per item in the background (1 = on, 0 = off)", "float"),
    ]),
    ("Appearance", [
        ("theme", "Theme (applies the next time the app starts)", ["dark", "light", "system"]),
    ]),
    ("Item icons", [
        ("real_item_icons", "Fetch the game's item icons from NCSOFT's item dictionary (1 = on, 0 = off)", "float"),
    ]),
    ("Alerts", [
        ("alert_toasts", "Desktop notifications (1 = on, 0 = off)", "float"),
        ("alert_sound", "Sound with notifications (1 = on, 0 = off)", "float"),
        ("flip_alerts", "Notify when a listing can be flipped", ["all", "confident", "off"]),
        ("alert_min_profit", "Notify for a craft above this profit (Kina)", "float"),
        ("dip_pct", "Watched-item dip below its 7-day median (%)", "float"),
        ("squeeze_drop_pct", "Supply squeeze: listings fell by (%)", "float"),
        ("squeeze_days", "Supply squeeze: over (days)", "float"),
    ]),
    ("Crafting and trust", [
        ("trade_gap_pct", "A trade this far from the API average is a large gap (%)", "float"),
        ("streak_risk", "Bad-streak risk (0.05 = 1 in 20)", "float"),
        ("sanity_jump_factor", "Flag a price above this multiple of its 7-day median", "float"),
        ("sanity_drop_factor", "Flag a price below this fraction of its 7-day median", "float"),
    ]),
]


class SettingsTab(QWidget):
    saved = Signal()

    def __init__(self, state: AppState, parent=None):
        super().__init__(parent)
        self.state = state
        self._widgets: dict[str, QWidget] = {}
        inner = QWidget()
        box = QVBoxLayout(inner)
        for title, fields in SETTINGS:
            group = QGroupBox(title)
            form = QFormLayout(group)
            for key, label, kind in fields:
                if isinstance(kind, list):
                    widget = QComboBox()
                    widget.addItems(kind)
                else:
                    widget = QLineEdit()
                    widget.setMaximumWidth(260)
                self._widgets[key] = widget
                form.addRow(label, widget)
            box.addWidget(group)
        box.addStretch(1)
        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setWidget(inner)

        self.message = QLabel()
        save = QPushButton("Save")
        reset = QPushButton("Reset to defaults")
        save.clicked.connect(self._save)
        reset.clicked.connect(self._reset)
        row = _buttons(save, reset)
        row.addWidget(self.message)
        layout = QVBoxLayout(self)
        layout.addWidget(scroll)
        layout.addLayout(row)
        self.load()

    def load(self, defaults: bool = False) -> None:
        db = self.state.db
        for key, widget in self._widgets.items():
            value = DEFAULT_SETTINGS[key] if defaults else (db.get_setting(key) or "")
            if isinstance(widget, QComboBox):
                widget.setCurrentText(value)
            else:
                widget.setText(value)

    def _reset(self) -> None:
        self.load(defaults=True)
        self.message.setText("Defaults loaded; press Save to keep them.")

    def _save(self) -> None:
        values = {}
        for _, fields in SETTINGS:
            for key, label, kind in fields:
                widget = self._widgets[key]
                value = widget.currentText() if isinstance(widget, QComboBox) else widget.text().strip()
                if kind == "float":
                    try:
                        if float(value) < 0:
                            raise ValueError
                    except ValueError:
                        self.message.setText(f"“{label}” must be a number, zero or above.")
                        return
                values[key] = value
        for key, value in values.items():
            self.state.db.set_setting(key, value)
        self.message.setText("Saved.")
        self.saved.emit()
