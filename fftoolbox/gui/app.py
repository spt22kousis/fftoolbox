from __future__ import annotations

import sys
from pathlib import Path

from PySide6.QtWidgets import QApplication

from .main_window import MainWindow


def main(argv: list[str] | None = None) -> int:
    argv = sys.argv if argv is None else argv
    app = QApplication(argv)
    app.setApplicationName("自製壓制工具箱")
    window = MainWindow()
    window.show()
    files = [Path(a) for a in argv[1:] if Path(a).exists()]  # 拖到 exe 上的檔案
    if files:
        window.add_paths(files)
    return app.exec()
