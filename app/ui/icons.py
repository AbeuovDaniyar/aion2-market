"""Item icons: the game's own icon when it has been downloaded (app.external.icons),
otherwise the category's symbol on a tile coloured by section, drawn locally."""

from __future__ import annotations

from functools import lru_cache
from pathlib import Path

from PySide6.QtCore import QRectF, Qt
from PySide6.QtGui import QColor, QFont, QIcon, QPainter, QPixmap

SIZE = 48

SECTION_COLOURS = {
    "weapons": "#b9483f",
    "armor": "#3f6fb9",
    "accessories": "#8a52b8",
    "consumables": "#3f9a62",
    "skins": "#c2578f",
    "materials": "#9a7340",
    "other": "#7a7f87",
}

CATEGORY_SYMBOLS = {
    "Greatswords": "⚔️",
    "Longswords": "🗡️",
    "Daggers": "🔪",
    "Bows": "🏹",
    "Orbs": "🔮",
    "Spellbooks": "📖",
    "Staves": "🪄",
    "Maces": "🔨",
    "Guards": "🛡️",
    "Helms": "🪖",
    "Pauldrons": "🎽",
    "Breastplates": "🥋",
    "Greaves": "👖",
    "Gloves": "🧤",
    "Boots": "🥾",
    "Cloaks": "🧥",
    "Necklaces": "📿",
    "Earrings": "💎",
    "Rings": "💍",
    "Potions & serums": "🧪",
    "Food": "🍖",
    "Scrolls": "📜",
    "Manastones & soulstones": "💠",
    "Transformation candy": "🍬",
    "Wings": "🪶",
    "Artwork": "🖼️",
    "Other items": "📦",
    "Armor skins": "👘",
    "Accessory skins": "🎀",
    "Gathered resources": "🌿",
    "Crafting materials": "⚙️",
    "Sealed wings": "🪶",
    "Amplify stones": "🔆",
}

SECTION_SYMBOLS = {
    "weapons": "⚔️",
    "armor": "🛡️",
    "accessories": "💍",
    "consumables": "🧪",
    "skins": "👘",
    "materials": "⚙️",
    "other": "📦",
}


def symbol_for(section: str | None, category: str | None) -> str:
    return CATEGORY_SYMBOLS.get(category or "") or SECTION_SYMBOLS.get(section or "", "📦")


@lru_cache(maxsize=None)
def item_icon(section: str | None, category: str | None) -> QIcon:
    """Needs a QApplication. One icon per (section, category), cached."""
    pixmap = QPixmap(SIZE, SIZE)
    pixmap.fill(Qt.GlobalColor.transparent)
    painter = QPainter(pixmap)
    painter.setRenderHint(QPainter.RenderHint.Antialiasing)
    tile = QColor(SECTION_COLOURS.get(section or "", SECTION_COLOURS["other"]))
    painter.setPen(Qt.PenStyle.NoPen)
    painter.setBrush(tile)
    painter.drawRoundedRect(QRectF(1, 1, SIZE - 2, SIZE - 2), 10, 10)
    font = QFont("Segoe UI Emoji")
    font.setPixelSize(int(SIZE * 0.58))
    painter.setFont(font)
    painter.setPen(QColor("white"))
    painter.drawText(QRectF(0, 0, SIZE, SIZE), Qt.AlignmentFlag.AlignCenter, symbol_for(section, category))
    painter.end()
    return QIcon(pixmap)


_files: dict[int, tuple[str, str | None]] = {}
_dir: Path | None = None


def set_icon_files(directory: Path, files: dict[int, tuple[str, str | None]]) -> None:
    """Tell the UI which real icons are on disk: item_key -> (file name, grade)."""
    global _dir
    _dir = directory
    _files.clear()
    _files.update(files)


@lru_cache(maxsize=None)
def _file_icon(path: str) -> QIcon | None:
    pixmap = QPixmap(path)
    return None if pixmap.isNull() else QIcon(pixmap)


def icon_for(item_key: int | None, section: str | None, category: str | None) -> QIcon:
    """The item's real icon if downloaded, else its category icon."""
    entry = _files.get(item_key)
    if entry and _dir is not None:
        icon = _file_icon(str(_dir / entry[0]))
        if icon is not None:
            return icon
    return item_icon(section, category)


def grade_of(item_key: int | None) -> str | None:
    entry = _files.get(item_key)
    return entry[1] if entry else None