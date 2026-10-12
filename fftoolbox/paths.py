"""程式路徑、附帶 ffmpeg 位置、子程序共用設定。

附帶的 ffmpeg 放在：
    <程式資料夾>/bin/ffmpeg/ffmpeg.exe, ffprobe.exe          主要（新版）
    <程式資料夾>/bin/ffmpeg_compat/ffmpeg.exe, ffprobe.exe   相容版（舊 NVENC SDK，可省略）

主要 ffmpeg 的尋找順序：
    1. 環境變數 FFTOOLBOX_FFMPEG_DIR
    2. 程式附帶的 bin/ffmpeg
    3. 系統 PATH（ffmpeg.exe 與 ffprobe.exe 要在同一個資料夾）
相容版只看環境變數 FFTOOLBOX_FFMPEG_COMPAT_DIR 與 bin/ffmpeg_compat。
"""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path

MAIN = "main"
COMPAT = "compat"


def app_dir() -> Path:
    """程式所在資料夾（打包後是 exe 所在資料夾，開發時是專案根目錄）。"""
    if getattr(sys, "frozen", False):
        return Path(sys.executable).resolve().parent
    return Path(__file__).resolve().parent.parent


def config_dir() -> Path:
    """使用者設定資料夾：%APPDATA%/fftoolbox（每台電腦、每個使用者各自一份）。"""
    base = os.environ.get("APPDATA")
    path = Path(base) / "fftoolbox" if base else Path.home() / ".fftoolbox"
    path.mkdir(parents=True, exist_ok=True)
    return path


ENV, BUNDLED, SYSTEM_PATH = "env", "bundled", "path"
SOURCE_LABELS = {ENV: "環境變數指定", BUNDLED: "程式附帶", SYSTEM_PATH: "系統 PATH"}


@dataclass(frozen=True)
class FFmpegBuild:
    name: str  # MAIN 或 COMPAT
    dir: Path
    source: str = BUNDLED  # ENV / BUNDLED / SYSTEM_PATH：從哪裡找到的

    @property
    def ffmpeg(self) -> Path:
        return self.dir / "ffmpeg.exe"

    @property
    def ffprobe(self) -> Path:
        return self.dir / "ffprobe.exe"

    def exists(self) -> bool:
        return self.ffmpeg.is_file()

    def fingerprint(self) -> str:
        """用來判斷偵測快取是否過期（換了 ffmpeg 就要重測）。"""
        if not self.exists():
            return f"{self.name}:missing"
        st = self.ffmpeg.stat()
        return f"{self.name}:{self.ffmpeg}:{st.st_size}:{int(st.st_mtime)}"


def _path_ffmpeg_dir() -> Path | None:
    """系統 PATH 上同時有 ffmpeg.exe 與 ffprobe.exe 的資料夾。
    winget 等安裝方式放在 PATH 上的是捷徑（symlink），優先用實際檔案所在的資料夾（shared 版的 DLL 在那裡）。"""
    found = shutil.which("ffmpeg")
    if not found:
        return None
    link = Path(found)
    candidates = []
    try:
        candidates.append(link.resolve().parent)
    except OSError:
        pass
    candidates.append(link.parent)
    for d in candidates:
        if (d / "ffmpeg.exe").is_file() and (d / "ffprobe.exe").is_file():
            return d
    return None


def main_build() -> FFmpegBuild:
    """主要 ffmpeg：環境變數 FFTOOLBOX_FFMPEG_DIR → 程式附帶的 bin/ffmpeg → 系統 PATH。
    都找不到時回傳附帶的位置（exists() 為 False，錯誤訊息會指向這裡）。"""
    override = os.environ.get("FFTOOLBOX_FFMPEG_DIR")
    if override:
        return FFmpegBuild(MAIN, Path(override), ENV)
    bundled = FFmpegBuild(MAIN, app_dir() / "bin" / "ffmpeg", BUNDLED)
    if bundled.exists():
        return bundled
    path_dir = _path_ffmpeg_dir()
    if path_dir:
        return FFmpegBuild(MAIN, path_dir, SYSTEM_PATH)
    return bundled


def compat_build() -> FFmpegBuild:
    """相容版不去 PATH 找（PATH 上的通常是新版）。"""
    override = os.environ.get("FFTOOLBOX_FFMPEG_COMPAT_DIR")
    if override:
        return FFmpegBuild(COMPAT, Path(override), ENV)
    return FFmpegBuild(COMPAT, app_dir() / "bin" / "ffmpeg_compat", BUNDLED)


def describe_build(build: FFmpegBuild) -> str:
    return f"{build.ffmpeg}（{SOURCE_LABELS.get(build.source, build.source)}）"


def get_build(name: str) -> FFmpegBuild:
    return compat_build() if name == COMPAT else main_build()


# 從 GUI 啟動子程序時不要跳出黑色主控台視窗
CREATE_NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0)


def run(cmd: list[str], timeout: float | None = None) -> subprocess.CompletedProcess:
    """執行指令並收集輸出（UTF-8，無法解碼的位元組以替代字元處理）。"""
    return subprocess.run(
        cmd,
        stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        encoding="utf-8",
        errors="replace",
        timeout=timeout,
        creationflags=CREATE_NO_WINDOW,
    )
