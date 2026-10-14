"""Entry point for the packaged exe (PyInstaller)."""

import sys

from app.main import main

if __name__ == "__main__":
    sys.exit(main())
