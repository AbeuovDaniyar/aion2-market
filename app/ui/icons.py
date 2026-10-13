"""Item icons by category, drawn locally.

The API has no item artwork and the site's pages are off limits, so an item gets
its category's symbol on a tile coloured by section: enough to tell a bow from a
potion at a glance. They are not the game's own icons.
"""

from __future__ import annotations

from functools import lru_cache

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
