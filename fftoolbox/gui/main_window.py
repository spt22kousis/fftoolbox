"""主視窗。"""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

from PySide6.QtCore import QSize, Qt, QTimer, QUrl
from PySide6.QtGui import QBrush, QColor, QDesktopServices, QFontDatabase, QGuiApplication, \
    QStandardItem, QStandardItemModel
from PySide6.QtWidgets import (QAbstractItemView, QApplication, QButtonGroup, QComboBox,
                               QCheckBox, QFileDialog, QFormLayout, QGroupBox, QHBoxLayout,
                               QHeaderView, QInputDialog, QLabel, QLineEdit, QMainWindow, QMenu,
                               QMessageBox, QPlainTextEdit, QProgressBar, QPushButton,
                               QRadioButton, QScrollArea, QSlider, QSpinBox, QSplitter,
                               QTreeWidget, QTreeWidgetItem, QVBoxLayout, QWidget)

from .. import __version__, ass_fonts, config, encoders, presets
from ..encoders import (CODEC_LABELS, ENCODER_MAP, H264, HEVC, MAIN, NVENC, QSV, AMF,
                        QUALITY_MAX, QUALITY_MIN, SPEED_MAX, SPEED_MIN, VENDOR_LABELS,
                        DetectionReport, VideoSettings, encoders_for, format_report, speed_value)
from ..job import (AUDIO_MODES, CONTAINERS, TASK_LABELS, VIDEO_MODES, AudioSettings, Job, JobError,
                   JobResult, Progress, Task, build_command, default_output, plan_streams, unique_path)
from ..paths import BUNDLED, SYSTEM_PATH, app_dir, describe_build, main_build
from ..presets import Preset
from ..probe import MediaInfo, format_duration, probe
from .dialogs import PreviewDialog, TextDialog
from .workers import DetectWorker, FuncWorker, QueueWorker

SUB_EXTS = {".ass", ".ssa"}
VIDEO_EXTS = {".mp4", ".mkv", ".mov", ".m4v", ".avi", ".flv", ".wmv", ".webm", ".ts", ".m2ts",
              ".mts", ".mpg", ".mpeg", ".vob", ".rmvb", ".rm", ".3gp", ".ogv", ".y4m"}

WAITING, RUNNING, DONE, FAILED, CANCELLED = "等待", "執行中", "完成", "失敗", "已取消"
STATUS_COLORS = {DONE: "#2e7d32", FAILED: "#c62828", CANCELLED: "#8d6e63"}
RED, GREEN, ORANGE = "#c62828", "#2e7d32", "#b26a00"

COL_FILE, COL_STATUS, COL_OUTPUT = 0, 1, 2

QUALITY_NAMES = {NVENC: "CQ", QSV: "ICQ", AMF: "QP"}


@dataclass
class QueueItem:
    id: int
    path: Path
    status: str = WAITING
    output: Path | None = None
    info: MediaInfo | None = None
    info_error: str = ""
    error: str = ""


def _fmt_eta(seconds: float | None) -> str:
    if seconds is None:
        return "--:--"
    s = int(seconds)
    return f"{s // 3600}:{s % 3600 // 60:02d}:{s % 60:02d}" if s >= 3600 else f"{s // 60:02d}:{s % 60:02d}"


def _colored(label: QLabel, text: str, color: str | None = None) -> None:
    label.setText(text)
    label.setStyleSheet(f"color: {color};" if color else "")


class PathEdit(QWidget):
    """路徑輸入框 + 選擇按鈕。"""

    def __init__(self, placeholder: str, pick, parent=None) -> None:
        super().__init__(parent)
        self.edit = QLineEdit()
        self.edit.setPlaceholderText(placeholder)
        self.edit.setAcceptDrops(False)  # 拖放由主視窗統一處理
        self.edit.setClearButtonEnabled(True)
        btn = QPushButton("選擇…")
        btn.clicked.connect(pick)
        lay = QHBoxLayout(self)
        lay.setContentsMargins(0, 0, 0, 0)
        lay.addWidget(self.edit, 1)
        lay.addWidget(btn)

    def path(self) -> Path | None:
        t = self.edit.text().strip().strip('"')
        return Path(t) if t else None

    def set_path(self, p: Path | str | None) -> None:
        self.edit.setText(str(p) if p else "")


class MainWindow(QMainWindow):
    def __init__(self) -> None:
        super().__init__()
        self.setWindowTitle(f"自製壓制工具箱 {__version__}")
        self.setAcceptDrops(True)
        self.resize(1280, 860)

        self.report: DetectionReport | None = None
        self.items: dict[int, QueueItem] = {}
        self._next_id = 1
        self._wanted_encoder = "auto"
        self.presets = presets.load_all()
        self.worker: QueueWorker | None = None
        self._threads: set = set()
        self._probe_pending: list[int] = []  # 等待 ffprobe 的項目 id（一次只跑一個）
        self._probe_busy = False
        self._closing = False
        self._font_result: ass_fonts.FontCheckResult | None = None
        self._font_check_seq = 0
        self._run_total = 0
        self._run_index = 0
        self._run_counts: dict[str, int] = {}
        self.preview_dialog: PreviewDialog | None = None

        self._cmd_timer = QTimer(self, singleShot=True, interval=250)
        self._cmd_timer.timeout.connect(self._update_command_preview)

        self._build_ui()
        self._load_last()
        self._on_task_changed()
        self._refresh_encoder_combo()
        self._check_ffmpeg_and_detect()

    # ------------------------------------------------------------------ UI 建立

    def _build_ui(self) -> None:
        central = QWidget()
        self.setCentralWidget(central)
        root = QVBoxLayout(central)

        # 頂列：任務 + 預設檔
        top = QHBoxLayout()
        top.addWidget(QLabel("任務："))
        self.task_group = QButtonGroup(self)
        self.task_buttons: dict[Task, QRadioButton] = {}
        for task in Task:
            rb = QRadioButton(TASK_LABELS[task])
            self.task_group.addButton(rb)
            self.task_buttons[task] = rb
            top.addWidget(rb)
        self.task_buttons[Task.BURN].setChecked(True)
        self.task_group.buttonToggled.connect(lambda *_: self._on_task_changed())
        top.addStretch(1)
        top.addWidget(QLabel("預設檔："))
        self.preset_combo = QComboBox()
        self.preset_combo.setMinimumWidth(180)
        self.preset_combo.activated.connect(self._apply_selected_preset)
        top.addWidget(self.preset_combo)
        for text, slot in (("儲存…", self._save_preset), ("刪除", self._delete_preset),
                           ("匯出…", self._export_preset), ("匯入…", self._import_preset)):
            b = QPushButton(text)
            b.clicked.connect(slot)
            top.addWidget(b)
        self.preset_bar = [self.preset_combo]
        root.addLayout(top)
        self._refresh_preset_combo()

        vsplit = QSplitter(Qt.Vertical)
        hsplit = QSplitter(Qt.Horizontal)
        hsplit.addWidget(self._build_queue_panel())
        hsplit.addWidget(self._build_settings_panel())
        hsplit.setStretchFactor(0, 3)
        hsplit.setStretchFactor(1, 2)
        hsplit.setSizes([700, 560])
        vsplit.addWidget(hsplit)
        vsplit.addWidget(self._build_run_panel())
        vsplit.setStretchFactor(0, 4)
        vsplit.setStretchFactor(1, 1)
        vsplit.setSizes([700, 160])
        root.addWidget(vsplit, 1)

        self.statusBar().showMessage("就緒")

    def _build_queue_panel(self) -> QWidget:
        w = QWidget()
        lay = QVBoxLayout(w)
        lay.setContentsMargins(0, 0, 0, 0)

        self.queue = QTreeWidget()
        self.queue.setHeaderLabels(["檔案", "狀態", "輸出"])
        self.queue.setRootIsDecorated(False)
        self.queue.setSelectionMode(QAbstractItemView.ExtendedSelection)
        self.queue.setAcceptDrops(False)
        self.queue.setDragDropMode(QAbstractItemView.InternalMove)
        self.queue.header().setSectionResizeMode(COL_FILE, QHeaderView.Stretch)
        self.queue.header().setSectionResizeMode(COL_STATUS, QHeaderView.ResizeToContents)
        self.queue.header().setSectionResizeMode(COL_OUTPUT, QHeaderView.Stretch)
        self.queue.itemSelectionChanged.connect(self._on_selection_changed)
        self.queue.setContextMenuPolicy(Qt.CustomContextMenu)
        self.queue.customContextMenuRequested.connect(self._queue_menu)
        self.queue.setToolTip("把影片（和 .ass 字幕）拖進視窗即可加入")

        hint = QLabel("把影片拖進來加入佇列；拖入 .ass 會設定為字幕檔、拖入字型資料夾會設定為 fontsdir")
        hint.setStyleSheet("color: gray;")
        hint.setWordWrap(True)

        btns = QHBoxLayout()
        self.add_btn = QPushButton("加入檔案…")
        self.add_btn.clicked.connect(self._pick_files)
        self.remove_btn = QPushButton("移除")
        self.remove_btn.clicked.connect(self._remove_selected)
        self.clear_done_btn = QPushButton("清除已完成")
        self.clear_done_btn.clicked.connect(self._clear_done)
        for b in (self.add_btn, self.remove_btn, self.clear_done_btn):
            btns.addWidget(b)
        btns.addStretch(1)

        info_box = QGroupBox("來源資訊")
        il = QVBoxLayout(info_box)
        self.info_text = QPlainTextEdit()
        self.info_text.setReadOnly(True)
        self.info_text.setAcceptDrops(False)
        self.info_text.setMaximumHeight(170)
        self.info_text.setPlaceholderText("選取佇列中的檔案以顯示 ffprobe 資訊")
        il.addWidget(self.info_text)

        lay.addWidget(self.queue, 1)
        lay.addWidget(hint)
        lay.addLayout(btns)
        lay.addWidget(info_box)
        return w

    def _build_settings_panel(self) -> QWidget:
        panel = QWidget()
        lay = QVBoxLayout(panel)
        lay.setContentsMargins(0, 0, 6, 0)

        # 視訊
        self.video_box = QGroupBox("視訊")
        vf = QFormLayout(self.video_box)
        self.vmode_combo = QComboBox()
        for key, text in VIDEO_MODES.items():
            self.vmode_combo.addItem(text, key)
        self.vmode_combo.currentIndexChanged.connect(self._schedule_command_preview)
        self.vmode_fixed = QLabel("重新編碼（燒錄字幕一定要重新編碼視訊）")
        vf.addRow("處理", _stack_row(self.vmode_combo, self.vmode_fixed))
        self.codec_combo = QComboBox()
        for codec in (H264, HEVC):
            self.codec_combo.addItem(CODEC_LABELS[codec], codec)
        self.codec_combo.currentIndexChanged.connect(self._on_codec_changed)
        vf.addRow("格式", self.codec_combo)

        enc_row = QHBoxLayout()
        self.encoder_combo = QComboBox()
        self.encoder_combo.setMinimumWidth(240)
        self.encoder_combo.currentIndexChanged.connect(self._on_encoder_changed)
        self.redetect_btn = QPushButton("重新偵測")
        self.redetect_btn.clicked.connect(lambda: self._start_detection(force=True))
        self.detail_btn = QPushButton("詳細")
        self.detail_btn.clicked.connect(self._show_detection_detail)
        enc_row.addWidget(self.encoder_combo, 1)
        enc_row.addWidget(self.redetect_btn)
        enc_row.addWidget(self.detail_btn)
        vf.addRow("編碼器", enc_row)
        self.encoder_note = QLabel()
        self.encoder_note.setWordWrap(True)
        self.encoder_note.setTextInteractionFlags(Qt.TextSelectableByMouse)
        vf.addRow("", self.encoder_note)

        q_row = QHBoxLayout()
        self.quality_spin = QSpinBox()
        self.quality_spin.setRange(QUALITY_MIN, QUALITY_MAX)
        self.quality_spin.setValue(VideoSettings.quality)
        self.quality_spin.valueChanged.connect(self._schedule_command_preview)
        self.quality_hint = QLabel()
        self.quality_hint.setStyleSheet("color: gray;")
        q_row.addWidget(self.quality_spin)
        q_row.addWidget(self.quality_hint, 1)
        vf.addRow("品質", q_row)

        s_row = QHBoxLayout()
        self.speed_slider = QSlider(Qt.Horizontal)
        self.speed_slider.setRange(SPEED_MIN, SPEED_MAX)
        self.speed_slider.setValue(VideoSettings.speed)
        self.speed_slider.setTickPosition(QSlider.TicksBelow)
        self.speed_slider.valueChanged.connect(self._on_speed_changed)
        self.speed_label = QLabel()
        self.speed_label.setMinimumWidth(150)
        s_row.addWidget(QLabel("快"))
        s_row.addWidget(self.speed_slider, 1)
        s_row.addWidget(QLabel("慢"))
        s_row.addWidget(self.speed_label)
        vf.addRow("速度", s_row)

        # 進階
        self.adv_box = QGroupBox("進階")
        af = QFormLayout(self.adv_box)
        rate_row = QHBoxLayout()
        self.maxrate_edit = QLineEdit()
        self.maxrate_edit.setPlaceholderText("不限，例：20M")
        self.bufsize_edit = QLineEdit()
        self.bufsize_edit.setPlaceholderText("自動（上限×2）")
        rate_row.addWidget(self.maxrate_edit)
        rate_row.addWidget(QLabel("緩衝"))
        rate_row.addWidget(self.bufsize_edit)
        af.addRow("碼率上限", rate_row)
        misc_row = QHBoxLayout()
        self.tenbit_check = QCheckBox("10bit 輸出")
        self.gop_spin = QSpinBox()
        self.gop_spin.setRange(0, 9999)
        self.gop_spin.setSpecialValueText("預設")
        misc_row.addWidget(self.tenbit_check)
        misc_row.addSpacing(20)
        misc_row.addWidget(QLabel("GOP"))
        misc_row.addWidget(self.gop_spin)
        misc_row.addStretch(1)
        af.addRow("", misc_row)
        self.extra_edit = QLineEdit()
        self.extra_edit.setPlaceholderText("原樣加在視訊參數最後，例：-tune animation")
        af.addRow("額外參數", self.extra_edit)
        for w in (self.maxrate_edit, self.bufsize_edit, self.extra_edit):
            w.setAcceptDrops(False)
            w.textChanged.connect(self._schedule_command_preview)
        self.tenbit_check.toggled.connect(self._on_encoder_state_changed)
        self.gop_spin.valueChanged.connect(self._schedule_command_preview)

        # 音訊
        self.audio_box = QGroupBox("音訊（AAC）")
        aform = QFormLayout(self.audio_box)
        self.amode_combo = QComboBox()
        for key, text in AUDIO_MODES.items():
            self.amode_combo.addItem(text, key)
        self.amode_combo.currentIndexChanged.connect(self._schedule_command_preview)
        self.amode_fixed = QLabel("轉成 AAC（只轉音訊一定會轉檔）")
        aform.addRow("處理", _stack_row(self.amode_combo, self.amode_fixed))
        self.abitrate_combo = QComboBox()
        self.abitrate_combo.setEditable(True)
        self.abitrate_combo.addItems(["128k", "192k", "256k", "320k"])
        self.abitrate_combo.setCurrentText("256k")
        self.samplerate_combo = QComboBox()
        for text, val in (("保持原始", 0), ("44100 Hz", 44100), ("48000 Hz", 48000)):
            self.samplerate_combo.addItem(text, val)
        self.channels_combo = QComboBox()
        for text, val in (("保持原始", 0), ("2（立體聲）", 2), ("1（單聲道）", 1)):
            self.channels_combo.addItem(text, val)
        a_row = QHBoxLayout()
        a_row.addWidget(self.abitrate_combo)
        a_row.addWidget(QLabel("取樣率"))
        a_row.addWidget(self.samplerate_combo)
        a_row.addWidget(QLabel("聲道"))
        a_row.addWidget(self.channels_combo)
        aform.addRow("碼率", a_row)
        self.abitrate_combo.currentTextChanged.connect(self._schedule_command_preview)
        self.samplerate_combo.currentIndexChanged.connect(self._schedule_command_preview)
        self.channels_combo.currentIndexChanged.connect(self._schedule_command_preview)

        # 字幕
        self.sub_box = QGroupBox("字幕")
        sform = QFormLayout(self.sub_box)
        self.ass_edit = PathEdit("拖入或選擇 .ass", self._pick_ass)
        self.fonts_edit = PathEdit("（選填）字型資料夾", self._pick_fonts_dir)
        self.ass_edit.edit.editingFinished.connect(self._on_sub_changed)
        self.fonts_edit.edit.editingFinished.connect(self._on_sub_changed)
        sform.addRow("字幕檔", self.ass_edit)
        sform.addRow("字型資料夾", self.fonts_edit)
        self.font_summary = QLabel()
        self.font_summary.setWordWrap(True)
        self.font_recheck_btn = QPushButton("重新檢查")
        self.font_recheck_btn.setToolTip("安裝字型或修改字幕後，按這裡重新檢查")
        self.font_recheck_btn.clicked.connect(self._run_font_check)
        font_row = QHBoxLayout()
        font_row.addWidget(self.font_summary, 1)
        font_row.addWidget(self.font_recheck_btn, 0, Qt.AlignTop)
        sform.addRow("字型", font_row)
        self.font_tree = QTreeWidget()
        self.font_tree.setHeaderLabels(["字型", "狀態", "行數", "來源"])
        self.font_tree.setRootIsDecorated(False)
        self.font_tree.setAcceptDrops(False)
        self.font_tree.setMaximumHeight(120)
        self.font_tree.header().setSectionResizeMode(0, QHeaderView.Stretch)
        sform.addRow(self.font_tree)
        p_row = QHBoxLayout()
        self.preview_time = QLineEdit("0:30")
        self.preview_time.setMaximumWidth(100)
        self.preview_time.setAcceptDrops(False)
        self.preview_btn = QPushButton("預覽截圖")
        self.preview_btn.clicked.connect(self._open_preview)
        p_row.addWidget(QLabel("時間點"))
        p_row.addWidget(self.preview_time)
        p_row.addWidget(self.preview_btn)
        p_row.addStretch(1)
        sform.addRow("預覽", p_row)

        # 輸出
        self.out_box = QGroupBox("輸出")
        oform = QFormLayout(self.out_box)
        c_row = QHBoxLayout()
        self.container_combo = QComboBox()
        self.container_combo.addItems(CONTAINERS)
        self.container_combo.currentIndexChanged.connect(self._on_output_settings_changed)
        self.container_note = QLabel("只轉音訊時沿用來源容器（放不了 AAC 的改用 mkv / m4a）")
        self.container_note.setStyleSheet("color: gray;")
        c_row.addWidget(self.container_combo)
        c_row.addWidget(self.container_note, 1)
        oform.addRow("容器", c_row)
        self.out_same = QRadioButton("來源資料夾")
        self.out_custom = QRadioButton("指定：")
        self.out_same.setChecked(True)
        self.out_dir_edit = PathEdit("輸出資料夾", self._pick_out_dir)
        self.out_dir_edit.edit.editingFinished.connect(self._on_output_settings_changed)
        self.out_same.toggled.connect(self._on_output_settings_changed)
        od_row = QHBoxLayout()
        od_row.addWidget(self.out_same)
        od_row.addWidget(self.out_custom)
        od_row.addWidget(self.out_dir_edit, 1)
        oform.addRow("位置", od_row)

        for box in (self.video_box, self.adv_box, self.audio_box, self.sub_box, self.out_box):
            lay.addWidget(box)
        lay.addStretch(1)

        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setWidget(panel)
        scroll.setMinimumWidth(460)
        self.settings_panel = panel
        return scroll

    def _build_run_panel(self) -> QWidget:
        w = QWidget()
        lay = QVBoxLayout(w)
        lay.setContentsMargins(0, 0, 0, 0)

        row = QHBoxLayout()
        self.progress = QProgressBar()
        self.progress.setRange(0, 1000)
        self.progress.setValue(0)
        self.progress.setFormat("%p%")
        self.progress_label = QLabel("")
        self.progress_label.setMinimumWidth(380)
        self.start_btn = QPushButton("開始")
        self.start_btn.setMinimumSize(QSize(90, 32))
        self.start_btn.clicked.connect(self._start)
        self.stop_btn = QPushButton("停止")
        self.stop_btn.setMinimumSize(QSize(90, 32))
        self.stop_btn.setEnabled(False)
        self.stop_btn.clicked.connect(self._stop)
        row.addWidget(self.progress, 1)
        row.addWidget(self.progress_label)
        row.addWidget(self.start_btn)
        row.addWidget(self.stop_btn)

        cmd_row = QHBoxLayout()
        self.cmd_label = QLabel("指令")
        self.cmd_edit = QLineEdit()
        self.cmd_edit.setReadOnly(True)
        self.cmd_edit.setAcceptDrops(False)
        self.cmd_edit.setFont(QFontDatabase.systemFont(QFontDatabase.FixedFont))
        copy_btn = QPushButton("複製")
        copy_btn.clicked.connect(lambda: QGuiApplication.clipboard().setText(self.cmd_edit.text()))
        cmd_row.addWidget(self.cmd_label)
        cmd_row.addWidget(self.cmd_edit, 1)
        cmd_row.addWidget(copy_btn)

        self.log = QPlainTextEdit()
        self.log.setReadOnly(True)
        self.log.setAcceptDrops(False)
        self.log.setMaximumBlockCount(5000)
        self.log.setFont(QFontDatabase.systemFont(QFontDatabase.FixedFont))

        lay.addLayout(row)
        lay.addLayout(cmd_row)
        lay.addWidget(self.log, 1)
        return w

    # ------------------------------------------------------------------ 設定讀寫

    def current_task(self) -> Task:
        for task, rb in self.task_buttons.items():
            if rb.isChecked():
                return task
        return Task.BURN

    def current_video(self) -> VideoSettings:
        return VideoSettings(
            mode=self.vmode_combo.currentData(),
            codec=self.codec_combo.currentData(),
            encoder=self._wanted_encoder,
            quality=self.quality_spin.value(),
            speed=self.speed_slider.value(),
            tenbit=self.tenbit_check.isChecked() and self.tenbit_check.isEnabled(),
            maxrate=self.maxrate_edit.text().strip(),
            bufsize=self.bufsize_edit.text().strip(),
            gop=self.gop_spin.value(),
            extra=self.extra_edit.text().strip(),
        )

    def current_audio(self) -> AudioSettings:
        return AudioSettings(
            mode=self.amode_combo.currentData(),
            bitrate=self.abitrate_combo.currentText().strip() or "256k",
            sample_rate=self.samplerate_combo.currentData(),
            channels=self.channels_combo.currentData(),
        )

    def current_preset(self) -> Preset:
        v = self.current_video()
        v.tenbit = self.tenbit_check.isChecked()  # 存使用者的選擇，不管目前能不能用
        return Preset(v, self.current_audio(), self.container_combo.currentText())

    def apply_preset(self, p: Preset) -> None:
        v, a = p.video, p.audio
        self.vmode_combo.setCurrentIndex(max(self.vmode_combo.findData(v.mode), 0))
        self.amode_combo.setCurrentIndex(max(self.amode_combo.findData(a.mode), 0))
        idx = self.codec_combo.findData(v.codec)
        self.codec_combo.setCurrentIndex(max(idx, 0))
        self._wanted_encoder = v.encoder
        self.quality_spin.setValue(v.quality)
        self.speed_slider.setValue(v.speed)
        self.tenbit_check.setChecked(v.tenbit)
        self.maxrate_edit.setText(v.maxrate)
        self.bufsize_edit.setText(v.bufsize)
        self.gop_spin.setValue(v.gop)
        self.extra_edit.setText(v.extra)
        self.abitrate_combo.setCurrentText(a.bitrate)
        self.samplerate_combo.setCurrentIndex(max(self.samplerate_combo.findData(a.sample_rate), 0))
        self.channels_combo.setCurrentIndex(max(self.channels_combo.findData(a.channels), 0))
        self.container_combo.setCurrentText(p.container)
        self._refresh_encoder_combo()
        if v.encoder != "auto" and self._wanted_encoder == "auto" and self.report:
            self.statusBar().showMessage(f"預設檔指定的 {v.encoder} 在這台電腦不可用，已改為自動選擇", 8000)

    def out_dir(self) -> Path | None:
        return self.out_dir_edit.path() if self.out_custom.isChecked() else None

    def _load_last(self) -> None:
        last = config.load().get("last") or {}
        try:
            self.apply_preset(Preset.from_dict(last.get("preset") or {}))
        except (TypeError, ValueError):
            pass
        task = last.get("task")
        if task in {t.value for t in Task}:
            self.task_buttons[Task(task)].setChecked(True)
        self.fonts_edit.set_path(last.get("fonts_dir"))
        self.out_dir_edit.set_path(last.get("out_dir"))
        self.out_custom.setChecked(bool(last.get("out_custom")) and bool(last.get("out_dir")))
        self.preview_time.setText(last.get("preview_time") or "0:30")

    def _save_last(self) -> None:
        fonts = self.fonts_edit.path()
        out = self.out_dir_edit.path()
        config.update(last={
            "task": self.current_task().value,
            "preset": self.current_preset().to_dict(),
            "fonts_dir": str(fonts) if fonts else "",
            "out_dir": str(out) if out else "",
            "out_custom": self.out_custom.isChecked(),
            "preview_time": self.preview_time.text(),
        })

    # ------------------------------------------------------------------ 編碼器偵測

    def _check_ffmpeg_and_detect(self) -> None:
        build = main_build()
        if not build.exists():
            where = build.ffmpeg if build.source != BUNDLED else app_dir() / "bin" / "ffmpeg"
            msg = (f"找不到 ffmpeg。已找過：\n・{where}\n・系統 PATH\n\n"
                   f"請把 ffmpeg.exe 與 ffprobe.exe 放到 {app_dir() / 'bin' / 'ffmpeg'}，"
                   f"或把含有兩者的資料夾加到系統 PATH。")
            _colored(self.encoder_note, "找不到 ffmpeg，無法偵測編碼器", RED)
            self._log(msg.replace("\n\n", "\n"))
            QTimer.singleShot(0, lambda: QMessageBox.critical(self, "找不到 ffmpeg", msg))
            return
        if build.source == SYSTEM_PATH:
            self._log(f"沒有附帶的 ffmpeg（{app_dir() / 'bin' / 'ffmpeg'}），改用系統 PATH 上的：{build.ffmpeg}")
        else:
            self._log(f"ffmpeg：{describe_build(build)}")
        self._start_detection(force=False)

    def _start_detection(self, force: bool) -> None:
        if any(isinstance(t, DetectWorker) for t in self._threads):
            return
        self.redetect_btn.setEnabled(False)
        _colored(self.encoder_note, "偵測編碼器中…", None)
        w = DetectWorker(force, self)
        w.message.connect(lambda m: self.statusBar().showMessage(m))
        w.done.connect(self._on_detected)
        w.failed.connect(self._on_detect_failed)
        self._run_thread(w)

    def _on_detected(self, report: DetectionReport) -> None:
        self.report = report
        self.redetect_btn.setEnabled(True)
        avail = [n for n, s in report.statuses.items() if s.available]
        self.statusBar().showMessage(f"編碼器偵測完成（{report.detected_at}）：可用 {', '.join(avail) or '無'}", 10000)
        self._refresh_encoder_combo()

    def _on_detect_failed(self, msg: str) -> None:
        self.redetect_btn.setEnabled(True)
        _colored(self.encoder_note, f"偵測失敗：{msg}", RED)
        self._log(f"編碼器偵測失敗：{msg}")

    def _show_detection_detail(self) -> None:
        if not self.report:
            QMessageBox.information(self, "編碼器偵測", "尚未完成偵測")
            return
        TextDialog("編碼器偵測結果", format_report(self.report), self).exec()

    def _refresh_encoder_combo(self) -> None:
        codec = self.codec_combo.currentData()
        model = QStandardItemModel()
        auto_spec = self.report.pick(codec, self.tenbit_check.isChecked()) if self.report else None
        if self.report is None:
            auto_text = "自動（偵測中…）"
        else:
            auto_text = f"自動（→ {auto_spec.name}）" if auto_spec else "自動（無可用編碼器）"
        auto = QStandardItem(auto_text)
        auto.setData("auto", Qt.UserRole)
        model.appendRow(auto)
        for spec in encoders_for(codec):
            st = self.report.status(spec.name) if self.report else None
            text = f"{spec.name}　{VENDOR_LABELS[spec.vendor]}"
            item = QStandardItem(text)
            item.setData(spec.name, Qt.UserRole)
            if st is not None and not st.available:
                item.setText(f"{text}　— 不可用")
                item.setEnabled(False)
                item.setToolTip(st.reason)
            elif st is not None:
                tips = []
                if st.build != MAIN:
                    item.setText(f"{text}　[相容版]")
                if st.note:
                    tips.append(st.note)
                if st.tenbit is False:
                    tips.append("不支援 10bit：" + st.tenbit_reason)
                item.setToolTip("\n".join(tips))
            model.appendRow(item)

        wanted = self._wanted_encoder
        self.encoder_combo.blockSignals(True)
        self.encoder_combo.setModel(model)
        index = 0
        for row in range(model.rowCount()):
            it = model.item(row)
            if it.data(Qt.UserRole) == wanted and it.isEnabled():
                index = row
        self.encoder_combo.setCurrentIndex(index)
        self.encoder_combo.blockSignals(False)
        if self.report is not None:
            self._wanted_encoder = self.encoder_combo.currentData()
        self._on_encoder_state_changed()

    def _on_codec_changed(self) -> None:
        self._refresh_encoder_combo()

    def _on_encoder_changed(self) -> None:
        self._wanted_encoder = self.encoder_combo.currentData() or "auto"
        self._on_encoder_state_changed()

    def _resolved_spec(self):
        if not self.report:
            return None
        spec, _ = self.report.resolve(self.current_video())
        return spec

    def _on_encoder_state_changed(self) -> None:
        codec = self.codec_combo.currentData()
        # 10bit 是否可用
        can10 = False
        if self.report and codec == HEVC:
            if self._wanted_encoder == "auto":
                can10 = any(self.report.status(s.name).available and self.report.status(s.name).tenbit
                            for s in encoders_for(HEVC))
            else:
                can10 = bool(self.report.status(self._wanted_encoder).tenbit)
        self.tenbit_check.blockSignals(True)
        self.tenbit_check.setEnabled(can10)
        self.tenbit_check.blockSignals(False)
        self.tenbit_check.setToolTip("" if can10 else
                                     "H.264 不提供 10bit；或目前的 HEVC 編碼器不支援 10bit")
        # 自動選擇的文字會受 10bit 影響
        if self.report and self.encoder_combo.count():
            model = self.encoder_combo.model()
            spec = self.report.pick(codec, self.tenbit_check.isChecked())
            model.item(0).setText(f"自動（→ {spec.name}）" if spec else "自動（無可用編碼器）")

        spec = self._resolved_spec()
        if self.report is not None:
            if spec is None:
                _, err = self.report.resolve(self.current_video())
                _colored(self.encoder_note, err, RED)
            else:
                st = self.report.status(spec.name)
                if st.note:
                    _colored(self.encoder_note, st.note, ORANGE)
                elif self.tenbit_check.isChecked() and can10 and not st.tenbit:
                    _colored(self.encoder_note, f"{spec.name} 不支援 10bit，會輸出 8bit", ORANGE)
                else:
                    _colored(self.encoder_note, f"使用 {spec.name}（{VENDOR_LABELS[spec.vendor]}）", GREEN)
        name = QUALITY_NAMES.get(spec.vendor, "CRF") if spec else "品質值"
        self.quality_hint.setText(f"{name}，越小品質越好、檔案越大（常用 18–24）")
        self._on_speed_changed()

    def _on_speed_changed(self) -> None:
        level = self.speed_slider.value()
        spec = self._resolved_spec()
        native = f" → {speed_value(spec, level)}" if spec else ""
        self.speed_label.setText(f"{level}{native}")
        self._schedule_command_preview()

    # ------------------------------------------------------------------ 任務/字幕/輸出

    def _on_task_changed(self) -> None:
        task = self.current_task()
        self.video_box.setVisible(task != Task.AUDIO)
        self.adv_box.setVisible(task != Task.AUDIO)
        self.sub_box.setVisible(task == Task.BURN)
        self.vmode_combo.setVisible(task == Task.TRANSCODE)
        self.vmode_fixed.setVisible(task != Task.TRANSCODE)
        self.amode_combo.setVisible(task != Task.AUDIO)
        self.amode_fixed.setVisible(task == Task.AUDIO)
        self.container_combo.setEnabled(task != Task.AUDIO)
        self.container_note.setVisible(task == Task.AUDIO)
        self._on_output_settings_changed()

    def _on_output_settings_changed(self) -> None:
        self.out_dir_edit.setEnabled(self.out_custom.isChecked())
        self._refresh_output_column()
        self._schedule_command_preview()

    def _planned_output(self, item: QueueItem) -> Path:
        return default_output(item.path, self.current_task(), self.container_combo.currentText(),
                              self.out_dir())

    def _refresh_output_column(self) -> None:
        for row in range(self.queue.topLevelItemCount()):
            tw = self.queue.topLevelItem(row)
            item = self.items[tw.data(0, Qt.UserRole)]
            if item.status in (WAITING, FAILED, CANCELLED):
                tw.setText(COL_OUTPUT, self._planned_output(item).name)

    def _on_sub_changed(self) -> None:
        self._run_font_check()
        self._schedule_command_preview()

    def _run_font_check(self) -> None:
        ass = self.ass_edit.path()
        fonts = self.fonts_edit.path()
        self.font_tree.clear()
        self._font_result = None
        self._font_check_seq += 1
        seq = self._font_check_seq
        if not ass:
            _colored(self.font_summary, "")
            return
        if not ass.is_file():
            _colored(self.font_summary, f"找不到字幕檔：{ass}", RED)
            return
        if fonts and not fonts.is_dir():
            _colored(self.font_summary, f"找不到字型資料夾：{fonts}", RED)
            return
        _colored(self.font_summary, "檢查字型中…")
        w = FuncWorker(lambda: ass_fonts.check_fonts(ass, fonts), self)
        w.done.connect(lambda r: self._on_font_checked(seq, r))
        w.failed.connect(lambda m: seq == self._font_check_seq and
                         _colored(self.font_summary, f"字型檢查失敗：{m}", RED))
        self._run_thread(w)

    def _on_font_checked(self, seq: int, result: ass_fonts.FontCheckResult) -> None:
        if seq != self._font_check_seq:
            return
        self._font_result = result
        self.font_tree.clear()
        for f in result.fonts:
            where = "、".join(sorted({loc.source for loc in f.locations}))
            tw = QTreeWidgetItem([f.usage.name, "✓" if f.found else "✗ 缺少", str(f.usage.lines), where])
            tw.setToolTip(0, "\n".join(loc.path for loc in f.locations) or "找不到")
            tw.setToolTip(3, tw.toolTip(0))
            if not f.found:
                for c in range(4):
                    tw.setForeground(c, QBrush(QColor(RED)))
            self.font_tree.addTopLevelItem(tw)
        info = f"{result.dialogue_count} 行對話，用到 {len(result.fonts)} 個字型（編碼 {result.encoding}）"
        if result.missing:
            _colored(self.font_summary,
                     f"⚠ 缺少 {len(result.missing)} 個字型：" + "、".join(f.usage.name for f in result.missing)
                     + f"\n{info}", RED)
        else:
            _colored(self.font_summary, f"字型齊全。{info}", GREEN)
        if result.unknown_styles:
            self.font_summary.setText(self.font_summary.text() + "\n找不到樣式（會用 Default）："
                                      + "、".join(sorted(result.unknown_styles)))

    def _open_preview(self) -> None:
        item = self._selected_item() or self._first_item()
        if item is None:
            QMessageBox.information(self, "預覽", "請先加入影片")
            return
        ass = self.ass_edit.path() if self.current_task() == Task.BURN else None
        if ass and not ass.is_file():
            QMessageBox.warning(self, "預覽", f"找不到字幕檔：{ass}")
            return
        if self.preview_dialog is None:
            self.preview_dialog = PreviewDialog(self)
            self.preview_dialog.time_edit.editingFinished.connect(
                lambda: self.preview_time.setText(self.preview_dialog.time_text()))
        self.preview_dialog.show_for(item.path, ass, self.fonts_edit.path(), self.preview_time.text())

    # ------------------------------------------------------------------ 佇列

    def _first_item(self) -> QueueItem | None:
        if self.queue.topLevelItemCount() == 0:
            return None
        return self.items[self.queue.topLevelItem(0).data(0, Qt.UserRole)]

    def _selected_item(self) -> QueueItem | None:
        sel = self.queue.selectedItems()
        return self.items[sel[0].data(0, Qt.UserRole)] if sel else None

    def _tree_item(self, item_id: int) -> QTreeWidgetItem | None:
        for row in range(self.queue.topLevelItemCount()):
            tw = self.queue.topLevelItem(row)
            if tw.data(0, Qt.UserRole) == item_id:
                return tw
        return None

    def add_paths(self, paths: list[Path]) -> None:
        videos: list[Path] = []
        for p in paths:
            if p.is_dir():
                children = list(p.iterdir())
                has_fonts = any(c.suffix.lower() in ass_fonts.FONT_EXTS for c in children)
                if has_fonts and not any(c.suffix.lower() in VIDEO_EXTS for c in children):
                    self.fonts_edit.set_path(p)
                    self._on_sub_changed()
                    self.statusBar().showMessage(f"已設定字型資料夾：{p}", 5000)
                else:
                    videos += sorted(c for c in children if c.suffix.lower() in VIDEO_EXTS)
            elif p.suffix.lower() in SUB_EXTS:
                self.ass_edit.set_path(p)
                self.task_buttons[Task.BURN].setChecked(True)
                self._on_sub_changed()
                self.statusBar().showMessage(f"已設定字幕檔：{p.name}", 5000)
            elif p.suffix.lower() in ass_fonts.FONT_EXTS:
                self.statusBar().showMessage("字型請拖入整個資料夾（設定為字型資料夾）", 5000)
            elif p.is_file():
                videos.append(p)
        if not videos:
            return
        if self.worker is not None:
            self.statusBar().showMessage("執行中無法加入檔案", 5000)
            return
        existing = {it.path for it in self.items.values()}
        added = 0
        for v in videos:
            if v in existing:
                continue
            item = QueueItem(self._next_id, v)
            self._next_id += 1
            self.items[item.id] = item
            tw = QTreeWidgetItem([v.name, WAITING, self._planned_output(item).name])
            tw.setData(0, Qt.UserRole, item.id)
            tw.setToolTip(0, str(v))
            tw.setFlags(tw.flags() & ~Qt.ItemIsDropEnabled)
            self.queue.addTopLevelItem(tw)
            added += 1
            self._enqueue_probe(item)
            # 燒字幕時，字幕欄空著就找同名 .ass
            if self.current_task() == Task.BURN and not self.ass_edit.path():
                for ext in (".ass", ".ssa"):
                    cand = v.with_suffix(ext)
                    if cand.is_file():
                        self.ass_edit.set_path(cand)
                        self._on_sub_changed()
                        break
        if added and not self.queue.selectedItems():
            self.queue.setCurrentItem(self.queue.topLevelItem(self.queue.topLevelItemCount() - added))

    def _pick_files(self) -> None:
        files, _ = QFileDialog.getOpenFileNames(self, "加入影片", "",
                                                "影片 (*" + " *".join(sorted(VIDEO_EXTS)) + ");;所有檔案 (*)")
        self.add_paths([Path(f) for f in files])

    def _pick_ass(self) -> None:
        f, _ = QFileDialog.getOpenFileName(self, "選擇字幕", str(self.ass_edit.path() or ""),
                                           "ASS 字幕 (*.ass *.ssa)")
        if f:
            self.ass_edit.set_path(f)
            self._on_sub_changed()

    def _pick_fonts_dir(self) -> None:
        d = QFileDialog.getExistingDirectory(self, "選擇字型資料夾", str(self.fonts_edit.path() or ""))
        if d:
            self.fonts_edit.set_path(d)
            self._on_sub_changed()

    def _pick_out_dir(self) -> None:
        d = QFileDialog.getExistingDirectory(self, "選擇輸出資料夾", str(self.out_dir_edit.path() or ""))
        if d:
            self.out_dir_edit.set_path(d)
            self.out_custom.setChecked(True)
            self._on_output_settings_changed()

    def _remove_selected(self) -> None:
        for tw in self.queue.selectedItems():
            item_id = tw.data(0, Qt.UserRole)
            item = self.items[item_id]
            if item.status == RUNNING:
                continue
            if self.worker is not None and item.status == WAITING:
                self.worker.skip(item_id)  # 這輪壓制裡還沒輪到它
                self._run_total -= 1
            self.queue.takeTopLevelItem(self.queue.indexOfTopLevelItem(tw))
            del self.items[item_id]

    def _clear_done(self) -> None:
        for row in reversed(range(self.queue.topLevelItemCount())):
            tw = self.queue.topLevelItem(row)
            if self.items[tw.data(0, Qt.UserRole)].status == DONE:
                self.queue.takeTopLevelItem(row)
                del self.items[tw.data(0, Qt.UserRole)]

    def _queue_menu(self, pos) -> None:
        tw = self.queue.itemAt(pos)
        if tw is None:
            return
        item = self.items[tw.data(0, Qt.UserRole)]
        menu = QMenu(self)
        open_src = menu.addAction("開啟來源資料夾")
        open_out = menu.addAction("開啟輸出檔所在資料夾")
        open_out.setEnabled(item.output is not None and item.output.exists())
        show_err = menu.addAction("顯示錯誤訊息")
        show_err.setEnabled(bool(item.error))
        reset = menu.addAction("重設為等待")
        reset.setEnabled(item.status in (DONE, FAILED, CANCELLED) and self.worker is None)
        remove = menu.addAction("移除")
        remove.setEnabled(item.status != RUNNING)
        act = menu.exec(self.queue.viewport().mapToGlobal(pos))
        if act == open_src:
            _reveal(item.path)
        elif act == open_out and item.output:
            _reveal(item.output)
        elif act == show_err:
            TextDialog(f"錯誤：{item.path.name}", item.error, self).exec()
        elif act == reset:
            item.status, item.error = WAITING, ""
            self._set_row_status(item)
            self._refresh_output_column()
        elif act == remove:
            self._remove_selected()

    def _on_selection_changed(self) -> None:
        item = self._selected_item()
        if item is None:
            self.info_text.clear()
            return
        if item.info or item.info_error:
            self._show_info(item)
        else:
            self.info_text.setPlainText(f"{item.path}\n讀取中…")
            self._enqueue_probe(item, front=True)  # 選取的優先讀
        self._schedule_command_preview()

    def _show_info(self, item: QueueItem) -> None:
        """來源資訊 + 依目前設定，每條串流會怎麼處理。"""
        if item.info_error:
            self.info_text.setPlainText(f"{item.path}\nffprobe 失敗：{item.info_error}")
            return
        if not item.info:
            return
        plan = plan_streams(self.current_task(), self.current_video(), self.current_audio(), item.info)
        lines = [str(item.path), item.info.summary(), "", "處理方式：" + "；".join(plan.notes)]
        lines += [f"⚠ {w}" for w in plan.warnings]
        self.info_text.setPlainText("\n".join(lines))

    def _enqueue_probe(self, item: QueueItem, front: bool = False) -> None:
        if item.info or item.info_error:
            return
        if item.id in self._probe_pending:
            self._probe_pending.remove(item.id)
        if front:
            self._probe_pending.insert(0, item.id)
        else:
            self._probe_pending.append(item.id)
        self._probe_next()

    def _probe_next(self) -> None:
        if self._probe_busy or self._closing:
            return
        while self._probe_pending:
            item = self.items.get(self._probe_pending.pop(0))
            if item and not (item.info or item.info_error):
                break
        else:
            return
        self._probe_busy = True
        w = FuncWorker(lambda p=item.path: probe(p), self)
        w.done.connect(lambda info, i=item: self._on_probed(i, info))
        w.failed.connect(lambda m, i=item: self._on_probe_failed(i, m))
        w.finished.connect(self._on_probe_finished)
        self._run_thread(w)

    def _on_probe_finished(self) -> None:
        self._probe_busy = False
        self._probe_next()

    def _on_probed(self, item: QueueItem, info: MediaInfo) -> None:
        item.info = info
        self._schedule_command_preview()

    def _on_probe_failed(self, item: QueueItem, msg: str) -> None:
        item.info_error = msg
        self._schedule_command_preview()

    def _set_row_status(self, item: QueueItem, text: str | None = None) -> None:
        tw = self._tree_item(item.id)
        if tw is None:
            return
        tw.setText(COL_STATUS, text or item.status)
        color = STATUS_COLORS.get(item.status)
        tw.setForeground(COL_STATUS, QBrush(QColor(color)) if color else QBrush())
        if item.output and item.status in (RUNNING, DONE):
            tw.setText(COL_OUTPUT, item.output.name)
            tw.setToolTip(COL_OUTPUT, str(item.output))

    # ------------------------------------------------------------------ 指令預覽

    def _schedule_command_preview(self, *_) -> None:
        if self.worker is None:
            self._cmd_timer.start()

    def _update_command_preview(self) -> None:
        if self.worker is not None:
            return
        item = self._selected_item() or self._first_item()
        if item is None:
            self.cmd_label.setText("指令")
            self.cmd_edit.setText("")
            return
        if self._selected_item() is item:
            self._show_info(item)
        if item.info is None:
            self.cmd_label.setText("指令（預覽）")
            self.cmd_edit.setText("（讀取來源資訊中…）" if not item.info_error else "（無法讀取來源）")
            return
        try:
            cmd = build_command(self._make_job(item, self._planned_output(item)), self.report, item.info)
        except (JobError, ValueError, OSError) as e:
            self.cmd_label.setText("指令（預覽）")
            self.cmd_edit.setText(f"（無法產生指令：{e}）")
            return
        cmd.cleanup()
        self.cmd_label.setText("指令（預覽）")
        self.cmd_edit.setText(cmd.text)
        self.cmd_edit.setCursorPosition(0)

    # ------------------------------------------------------------------ 執行

    def _make_job(self, item: QueueItem, output: Path) -> Job:
        task = self.current_task()
        return Job(task, item.path, output,
                   subtitle=self.ass_edit.path() if task == Task.BURN else None,
                   fonts_dir=self.fonts_edit.path() if task == Task.BURN else None,
                   video=self.current_video(), audio=self.current_audio())

    def _start(self) -> None:
        if self.worker is not None:
            return
        order = [self.items[self.queue.topLevelItem(r).data(0, Qt.UserRole)]
                 for r in range(self.queue.topLevelItemCount())]
        pending = [it for it in order if it.status != DONE]
        if not pending:
            QMessageBox.information(self, "開始", "佇列裡沒有待處理的檔案")
            return
        task = self.current_task()
        if not main_build().exists():
            QMessageBox.critical(self, "開始", f"找不到 ffmpeg：{main_build().ffmpeg}")
            return
        needs_encoder = task == Task.BURN or (
            task == Task.TRANSCODE and self.current_video().mode != "copy")
        if needs_encoder:
            if self.report is None:
                QMessageBox.information(self, "開始", "編碼器還在偵測中，請稍候")
                return
            spec, err = self.report.resolve(self.current_video())
            if spec is None and task == Task.BURN:
                QMessageBox.warning(self, "開始", err)
                return
        if self.out_custom.isChecked():
            d = self.out_dir_edit.path()
            if not d:
                QMessageBox.warning(self, "開始", "請選擇輸出資料夾")
                return
        if task == Task.BURN:
            ass = self.ass_edit.path()
            if not ass or not ass.is_file():
                QMessageBox.warning(self, "開始", "請指定字幕檔（.ass）")
                return
            if len(pending) > 1 and QMessageBox.question(
                    self, "開始", f"佇列有 {len(pending)} 個影片，都會燒錄同一份字幕：\n{ass.name}\n\n確定要繼續？"
            ) != QMessageBox.Yes:
                return
            if self._font_result and self._font_result.missing and QMessageBox.question(
                    self, "缺少字型",
                    "以下字型找不到，libass 會用其他字型代替：\n"
                    + "\n".join(f"・{f.usage.name}" for f in self._font_result.missing)
                    + "\n\n仍要繼續？") != QMessageBox.Yes:
                return

        self._save_last()
        taken: set[Path] = set()
        jobs: list[tuple[int, Job]] = []
        for item in pending:
            out = unique_path(self._planned_output(item), taken)
            taken.add(out)
            item.output, item.status, item.error = out, WAITING, ""
            self._set_row_status(item)
            jobs.append((item.id, self._make_job(item, out)))

        self._run_total, self._run_index = len(jobs), 0
        self._run_counts = {DONE: 0, FAILED: 0, CANCELLED: 0}
        self._log(f"===== 開始：{TASK_LABELS[task]}，共 {len(jobs)} 個 =====")
        self.worker = QueueWorker(jobs, self.report, self)
        self.worker.job_started.connect(self._on_job_started)
        self.worker.job_progress.connect(self._on_job_progress)
        self.worker.job_log.connect(self._log)
        self.worker.job_finished.connect(self._on_job_finished)
        self.worker.finished.connect(self._on_queue_finished)
        self._set_running(True)
        self.worker.start()

    def _stop(self) -> None:
        if self.worker is not None:
            self.stop_btn.setEnabled(False)
            self.progress_label.setText("停止中…")
            self.worker.stop()

    def _set_running(self, running: bool) -> None:
        self.start_btn.setEnabled(not running)
        self.stop_btn.setEnabled(running)
        self.settings_panel.setEnabled(not running)
        for w in (*self.task_buttons.values(), self.preset_combo, self.add_btn, self.clear_done_btn):
            w.setEnabled(not running)
        self.cmd_label.setText("執行中指令" if running else "指令")

    def _on_job_started(self, item_id: int, command: str) -> None:
        self._run_index += 1
        item = self.items.get(item_id)
        if item is None:
            return
        item.status = RUNNING
        self._set_row_status(item)
        self.cmd_edit.setText(command)
        self.cmd_edit.setCursorPosition(0)
        self.progress.setValue(0)
        self.progress_label.setText(f"{self._run_index}/{self._run_total}　{item.path.name}")
        self._log(f"--- [{self._run_index}/{self._run_total}] {item.path.name}\n{command}")

    def _on_job_progress(self, item_id: int, p: Progress) -> None:
        item = self.items.get(item_id)
        if item is None:
            return
        name = item.path.name
        if len(name) > 28:
            name = name[:26] + "…"
        parts = [f"{self._run_index}/{self._run_total}　{name}"]
        if p.fraction is not None:
            self.progress.setRange(0, 1000)
            self.progress.setValue(int(p.fraction * 1000))
            self._set_row_status(item, f"{RUNNING} {p.fraction * 100:.1f}%")
        else:
            self.progress.setRange(0, 0)
            parts.append(format_duration(p.out_time))
        if p.speed:
            parts.append(f"{p.speed:.2f}x")
        if p.fps:
            parts.append(f"{p.fps:.0f} fps")
        parts.append(f"剩餘 {_fmt_eta(p.eta)}")
        self.progress_label.setText("　".join(parts))

    def _on_job_finished(self, item_id: int, r: JobResult) -> None:
        item = self.items.get(item_id)
        if item is None:
            return
        for w in r.warnings:
            self._log(f"⚠ {w}")
        if r.ok:
            item.status = DONE
            size = item.output.stat().st_size / 1024 / 1024 if item.output and item.output.exists() else 0
            self._log(f"✓ 完成：{item.output}（{size:.1f} MB，用時 {_fmt_eta(r.elapsed)}）")
        elif r.cancelled:
            item.status = CANCELLED
            self._log(f"■ 已取消：{item.path.name}")
        else:
            item.status, item.error = FAILED, r.error
            self._log(f"✗ 失敗：{item.path.name}\n{r.error}")
        self._run_counts[item.status] = self._run_counts.get(item.status, 0) + 1
        self._set_row_status(item)

    def _on_queue_finished(self) -> None:
        self.worker = None
        self._set_running(False)
        self.progress.setRange(0, 1000)
        c = self._run_counts
        summary = f"結束：完成 {c.get(DONE, 0)}、失敗 {c.get(FAILED, 0)}、取消 {c.get(CANCELLED, 0)}"
        not_run = self._run_total - sum(c.values())
        if not_run > 0:
            summary += f"、未執行 {not_run}"
        self.progress_label.setText(summary)
        self._log(f"===== {summary} =====")
        self.statusBar().showMessage(summary)
        self._refresh_output_column()
        QApplication.alert(self)

    # ------------------------------------------------------------------ 預設檔

    def _refresh_preset_combo(self, select: str | None = None) -> None:
        self.preset_combo.blockSignals(True)
        self.preset_combo.clear()
        self.preset_combo.addItem("（選擇預設檔）", None)
        for name in sorted(self.presets):
            self.preset_combo.addItem(name, name)
        if select:
            self.preset_combo.setCurrentIndex(max(self.preset_combo.findData(select), 0))
        self.preset_combo.blockSignals(False)

    def _apply_selected_preset(self) -> None:
        name = self.preset_combo.currentData()
        if name and name in self.presets:
            self.apply_preset(self.presets[name])
            self.statusBar().showMessage(f"已套用預設檔：{name}", 5000)

    def _save_preset(self) -> None:
        current = self.preset_combo.currentData() or ""
        name, ok = QInputDialog.getText(self, "儲存預設檔", "名稱：", text=current)
        name = name.strip()
        if not ok or not name:
            return
        if name in self.presets and name != current and QMessageBox.question(
                self, "儲存預設檔", f"「{name}」已存在，要覆蓋嗎？") != QMessageBox.Yes:
            return
        self.presets[name] = self.current_preset()
        presets.save_all(self.presets)
        self._refresh_preset_combo(select=name)

    def _delete_preset(self) -> None:
        name = self.preset_combo.currentData()
        if not name:
            return
        if QMessageBox.question(self, "刪除預設檔", f"刪除「{name}」？") == QMessageBox.Yes:
            self.presets.pop(name, None)
            presets.save_all(self.presets)
            self._refresh_preset_combo()

    def _export_preset(self) -> None:
        name = self.preset_combo.currentData() or "我的設定"
        f, _ = QFileDialog.getSaveFileName(self, "匯出預設檔", f"{name}.json", "預設檔 (*.json)")
        if f:
            presets.export_preset(f, name, self.current_preset())
            self.statusBar().showMessage(f"已匯出：{f}", 5000)

    def _import_preset(self) -> None:
        f, _ = QFileDialog.getOpenFileName(self, "匯入預設檔", "", "預設檔 (*.json)")
        if not f:
            return
        try:
            name, preset = presets.import_preset(f)
        except ValueError as e:
            QMessageBox.warning(self, "匯入預設檔", str(e))
            return
        base, n = name, 2
        while name in self.presets:
            name = f"{base} ({n})"
            n += 1
        self.presets[name] = preset
        presets.save_all(self.presets)
        self._refresh_preset_combo(select=name)
        self.apply_preset(preset)
        self.statusBar().showMessage(f"已匯入並套用：{name}", 5000)

    # ------------------------------------------------------------------ 其他

    def _run_thread(self, t) -> None:
        self._threads.add(t)
        t.finished.connect(lambda: self._threads.discard(t))
        t.start()

    def _log(self, text: str) -> None:
        self.log.appendPlainText(text)

    def dragEnterEvent(self, event) -> None:
        if event.mimeData().hasUrls():
            event.acceptProposedAction()

    def dropEvent(self, event) -> None:
        paths = [Path(u.toLocalFile()) for u in event.mimeData().urls() if u.isLocalFile()]
        event.acceptProposedAction()
        self.add_paths(paths)

    def closeEvent(self, event) -> None:
        if not self._closing:
            if self.worker is not None:
                if QMessageBox.question(self, "結束", "還在壓制中，要停止並結束嗎？") != QMessageBox.Yes:
                    event.ignore()
                    return
                self.worker.stop()
            self._save_last()
            self._closing = True
            self._probe_pending.clear()
            if self.preview_dialog is not None:
                self.preview_dialog.close()
        if self._running_threads():
            # 編碼器偵測、ffprobe 等背景執行緒還在跑：QThread 不能在執行中被銷毀，
            # 所以先把視窗藏起來，等它們都結束再離開程式
            event.ignore()
            self.hide()
            for t in self._running_threads():
                t.finished.connect(self._quit_when_idle, Qt.QueuedConnection)
            return
        super().closeEvent(event)

    def _running_threads(self) -> list:
        threads = [*self._threads, self.worker]
        return [t for t in threads if t is not None and t.isRunning() and not t.wait(200)]

    def _quit_when_idle(self) -> None:
        if not self._running_threads():
            QApplication.quit()


def _stack_row(*widgets: QWidget) -> QWidget:
    """同一格放多個元件，依情況只顯示其中一個。"""
    w = QWidget()
    lay = QHBoxLayout(w)
    lay.setContentsMargins(0, 0, 0, 0)
    for x in widgets:
        lay.addWidget(x, 1)
    return w


def _reveal(path: Path) -> None:
    """在檔案總管中顯示檔案。"""
    if os.name == "nt" and path.exists():
        import subprocess
        subprocess.Popen(["explorer", "/select,", str(path)])
    else:
        QDesktopServices.openUrl(QUrl.fromLocalFile(str(path.parent)))
