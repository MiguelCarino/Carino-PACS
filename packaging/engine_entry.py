"""PyInstaller entry point — runs the Carino PACS CLI as a frozen binary.

    pacs-engine serve --host H --port P --receive --watch

behaves exactly like `python -m pacs serve ...`. The Electron desktop app
launches this binary in packaged builds instead of requiring a Python install.
"""

import sys

from pacs.__main__ import main

if __name__ == "__main__":
    sys.exit(main())
