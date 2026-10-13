"""A small table model shared by every tab: columns are (title, getter, formatter)."""

from __future__ import annotations

from collections.abc import Callable, Sequence
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any

from PySide6.QtCore import QAbstractTableModel, QModelIndex, QSortFilterProxyModel, Qt
from PySide6.QtGui import QBrush, QPalette
from PySide6.QtWidgets import QAbstractItemView, QApplication, QHeaderView, QTableView


def kina(value: float | None) -> str:
    return "" if value is None else f"{value:,.0f}"


def pct(value: float | None) -> str:
    return "" if value is None else f"{value:+,.0f}%"


def rate(value: float | None) -> str:
    return "" if value is None else f"{100 * value:.1f}%"


def per_day(value: float | None) -> str:
    if value is None:
        return ""
    return f"{value:,.0f}" if value >= 10 else f"{value:.1f}"


def days(value: float | None) -> str:
    return "" if value is None else f"{value:.1f}"


def age(value: timedelta | None) -> str:
    if value is None:
        return "unknown"
    minutes = int(value.total_seconds() // 60)
    if minutes < 90:
        return f"{minutes} min"
    hours = minutes / 60
    return f"{hours:.0f} h" if hours < 48 else f"{hours / 24:.0f} d"


def when(value: datetime | None) -> str:
    return "" if value is None else value.astimezone().strftime("%b %d %H:%M")


def flags(value: Sequence[str]) -> str:
    return ", ".join(f.replace("_", " ") for f in value)


@dataclass(frozen=True)
class Column:
    title: str
    value: Callable[[Any], Any]
    fmt: Callable[[Any], str] = lambda v: "" if v is None else str(v)
    tip: str = ""


class TableModel(QAbstractTableModel):
    def __init__(self, columns: list[Column], dim: Callable[[Any], bool] | None = None, parent=None):
        super().__init__(parent)
        self.columns = columns
        self.rows: list[Any] = []
        self._dim = dim

    def set_rows(self, rows: Sequence[Any]) -> None:
        self.beginResetModel()
        self.rows = list(rows)
        self.endResetModel()

    def rowCount(self, parent=QModelIndex()) -> int:
        return 0 if parent.isValid() else len(self.rows)

    def columnCount(self, parent=QModelIndex()) -> int:
        return 0 if parent.isValid() else len(self.columns)

    def headerData(self, section, orientation, role=Qt.ItemDataRole.DisplayRole):
        if orientation != Qt.Orientation.Horizontal:
            return None
        if role == Qt.ItemDataRole.DisplayRole:
            return self.columns[section].title
        if role == Qt.ItemDataRole.ToolTipRole:
            return self.columns[section].tip or None
        return None

    def data(self, index: QModelIndex, role=Qt.ItemDataRole.DisplayRole):
        if not index.isValid():
            return None
        row = self.rows[index.row()]
        column = self.columns[index.column()]
        if role == Qt.ItemDataRole.DisplayRole:
            return column.fmt(column.value(row))
        if role == Qt.ItemDataRole.UserRole:
            return column.value(row)
        if role == Qt.ItemDataRole.TextAlignmentRole:
            if isinstance(column.value(row), (int, float)):
                return int(Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter)
            return int(Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter)
        if role == Qt.ItemDataRole.ForegroundRole and self._dim and self._dim(row):
            # Stale or untrusted rows are greyed out.
            return QBrush(
                QApplication.palette().color(QPalette.ColorGroup.Disabled, QPalette.ColorRole.Text)
            )
        return None


class SortProxy(QSortFilterProxyModel):
    """Sorts on raw values (missing values always sort as smallest) and filters on any column."""

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setSortRole(Qt.ItemDataRole.UserRole)
        self.setFilterCaseSensitivity(Qt.CaseSensitivity.CaseInsensitive)
        self.setFilterKeyColumn(-1)

    def lessThan(self, left: QModelIndex, right: QModelIndex) -> bool:
        a = left.data(Qt.ItemDataRole.UserRole)
        b = right.data(Qt.ItemDataRole.UserRole)
        if a is None or b is None:
            return a is None and b is not None
        try:
            return a < b
        except TypeError:
            return str(a) < str(b)


class Table(QTableView):
    """A sortable, row-selecting view over a TableModel."""

    def __init__(self, columns: list[Column], dim=None, parent=None):
        super().__init__(parent)
        self.model_ = TableModel(columns, dim, self)
        self.proxy = SortProxy(self)
        self.proxy.setSourceModel(self.model_)
        self.setModel(self.proxy)
        self.setSortingEnabled(True)
        self.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        self.setSelectionMode(QAbstractItemView.SelectionMode.SingleSelection)
        self.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        self.setAlternatingRowColors(True)
        self.verticalHeader().hide()
        self.verticalHeader().setDefaultSectionSize(24)
        header = self.horizontalHeader()
        header.setSectionResizeMode(QHeaderView.ResizeMode.Interactive)
        header.setStretchLastSection(True)
        header.setSortIndicator(-1, Qt.SortOrder.AscendingOrder)
        self.resizeColumnsToContents()  # fit the headers until rows arrive

    def set_rows(self, rows: Sequence[Any], keep: Callable[[Any], Any] | None = None) -> None:
        """Replace the rows; `keep` (a key function) restores the selected row."""
        selected = keep(self.selected()) if keep and self.selected() is not None else None
        first = not self.model_.rows
        self.model_.set_rows(rows)
        if first and rows:
            self.resizeColumnsToContents()
            for i in range(len(self.model_.columns)):
                self.setColumnWidth(i, min(self.columnWidth(i) + 12, 320))
        if selected is not None:
            for i, row in enumerate(self.model_.rows):
                if keep(row) == selected:
                    self.selectRow(self.proxy.mapFromSource(self.model_.index(i, 0)).row())
                    break

    def selected(self) -> Any | None:
        indexes = self.selectionModel().selectedRows() if self.selectionModel() else []
        if not indexes:
            return None
        return self.model_.rows[self.proxy.mapToSource(indexes[0]).row()]

    def select_where(self, predicate: Callable[[Any], bool]) -> bool:
        for i, row in enumerate(self.model_.rows):
            if predicate(row):
                index = self.proxy.mapFromSource(self.model_.index(i, 0))
                if index.isValid():
                    self.selectRow(index.row())
                    self.scrollTo(index)
                    return True
        return False
