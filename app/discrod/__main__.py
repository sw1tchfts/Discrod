"""Entry point: ``python -m discrod``."""

from __future__ import annotations

import sys


def main() -> int:
    try:
        from PySide6 import QtWidgets
    except Exception as exc:  # pragma: no cover
        print(f"PySide6 is required to run the GUI: {exc}", file=sys.stderr)
        print("Install dependencies with: pip install -r requirements.txt",
              file=sys.stderr)
        return 1

    from .ui import MainWindow

    app = QtWidgets.QApplication(sys.argv)
    app.setApplicationName("Discrod")
    window = MainWindow()
    window.show()
    return app.exec()


if __name__ == "__main__":
    raise SystemExit(main())
