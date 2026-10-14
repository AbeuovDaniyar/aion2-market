"""Writes packaging/app.ico from the app's own tray icon, for the exe."""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from PySide6.QtWidgets import QApplication  # noqa: E402

from app.ui.main_window import app_icon  # noqa: E402

app = QApplication([])
target = Path(__file__).with_name("app.ico")
if not app_icon().pixmap(256, 256).save(str(target), "ICO"):
    raise SystemExit("could not write app.ico")
print(target)
