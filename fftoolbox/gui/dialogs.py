"""預覽截圖視窗與文字顯示視窗。"""

from __future__ import annotations

import tempfile
from pathlib import Path

from PySide6.QtCore import Qt
from PySide6.QtGui import QFontDatabase, QGuiApplication, QPixmap
from PySide6.QtWidgets import (QDialog, QDialogButtonBox, QHBoxLayout, QLabel, QLineEdit,
                               QPlainTextEdit, QPushButton, QSizePolicy, QVBoxLayout)

from ..job import parse_time, render_preview
from .workers import FuncWorker


class TextDialog(QDialog):
    def __init__(self, title: str, text: str, parent=None) -> None:
        super().__init__(parent)
        self.setWindowTitle(title)
        self.resize(720, 480)
        edit = QPlainTextEdit(text)
        edit.setReadOnly(True)
        edit.setFont(QFontDatabase.systemFont(QFontDatabase.FixedFont))
        buttons = QDialogButtonBox(QDialogButtonBox.Close)
        copy = buttons.addButton("複製", QDialogButtonBox.ActionRole)
        copy.clicked.connect(lambda: QGuiApplication.clipboard().setText(text))
        buttons.rejected.connect(self.reject)
        layout = QVBoxLayout(self)
        layout.addWidget(edit)
        layout.addWidget(buttons)


class PreviewDialog(QDialog):
    """指定時間點輸出一張帶字幕的截圖。"""

    def __init__(self, parent=None) -> None:
        super().__init__(parent)
        self.setWindowTitle("字幕預覽")
        self.resize(1000, 640)
        self.video: Path | None = None
        self.ass: Path | None = None
        self.fonts_dir: Path | None = None
        self._worker: FuncWorker | None = None
        self._pixmap: QPixmap | None = None
        self._png = Path(tempfile.gettempdir()) / "fftoolbox" / "preview.png"
        self._png.parent.mkdir(parents=True, exist_ok=True)

        self.image = QLabel("（尚未截圖）")
        self.image.setAlignment(Qt.AlignCenter)
        self.image.setMinimumSize(320, 180)
        self.image.setSizePolicy(QSizePolicy.Ignored, QSizePolicy.Ignored)
        self.time_edit = QLineEdit("0:30")
        self.time_edit.setMaximumWidth(120)
        self.time_edit.setPlaceholderText("例：1:23.5")
        self.time_edit.returnPressed.connect(self.refresh)
        self.button = QPushButton("截圖")
        self.button.clicked.connect(self.refresh)
        self.status = QLabel()
        self.status.setWordWrap(True)
        self.status.setTextInteractionFlags(Qt.TextSelectableByMouse)

        row = QHBoxLayout()
        row.addWidget(QLabel("時間點："))
        row.addWidget(self.time_edit)
        row.addWidget(self.button)
        row.addWidget(self.status, 1)
        layout = QVBoxLayout(self)
        layout.addWidget(self.image, 1)
        layout.addLayout(row)

    def show_for(self, video: Path, ass: Path | None, fonts_dir: Path | None, time_text: str) -> None:
        self.video, self.ass, self.fonts_dir = video, ass, fonts_dir
        self.time_edit.setText(time_text)
        self.show()
        self.raise_()
        self.refresh()

    def refresh(self) -> None:
        if self.video is None or (self._worker and self._worker.isRunning()):
            return
        try:
            t = parse_time(self.time_edit.text())
        except ValueError as e:
            self.status.setText(str(e))
            return
        video, ass, fonts, png = self.video, self.ass, self.fonts_dir, self._png
        self.status.setText(f"截圖中…（{video.name}）")
        self.button.setEnabled(False)
        self._worker = FuncWorker(lambda: render_preview(video, t, png, ass, fonts), self)
        self._worker.done.connect(self._on_done)
        self._worker.failed.connect(self._on_failed)
        self._worker.start()

    def _on_done(self, png: Path) -> None:
        self.button.setEnabled(True)
        self._pixmap = QPixmap(str(png))
        self.status.setText(f"{self.video.name}　{self.time_edit.text()}　"
                            f"{self._pixmap.width()}x{self._pixmap.height()}")
        self._update_image()

    def _on_failed(self, msg: str) -> None:
        self.button.setEnabled(True)
        self.status.setText(f"截圖失敗：{msg}")

    def _update_image(self) -> None:
        if self._pixmap and not self._pixmap.isNull():
            self.image.setPixmap(self._pixmap.scaled(self.image.size(), Qt.KeepAspectRatio,
                                                     Qt.SmoothTransformation))

    def resizeEvent(self, event) -> None:
        super().resizeEvent(event)
        self._update_image()

    def time_text(self) -> str:
        return self.time_edit.text()
