"""config.json 讀寫（存在 %APPDATA%/fftoolbox/config.json）。"""

from __future__ import annotations

import json
import os
import tempfile
import threading
from pathlib import Path
from typing import Any

from .paths import config_dir

# 偵測結果在背景執行緒寫入，介面設定在主執行緒寫入；讀-改-寫要整段鎖住才不會互相蓋掉
_lock = threading.RLock()


def config_path() -> Path:
    return config_dir() / "config.json"


def write_json_atomic(path: Path, data: Any, indent: int | None = 2) -> None:
    """先寫到同資料夾的唯一暫存檔再取代，寫到一半當掉也不會留下壞檔。"""
    fd, tmp = tempfile.mkstemp(prefix=path.stem + "_", suffix=".tmp", dir=path.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=indent)
        os.replace(tmp, path)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


def load() -> dict[str, Any]:
    with _lock:
        try:
            with open(config_path(), encoding="utf-8") as f:
                data = json.load(f)
            return data if isinstance(data, dict) else {}
        except (OSError, ValueError):
            return {}


def save(data: dict[str, Any]) -> None:
    with _lock:
        write_json_atomic(config_path(), data)


def update(**items: Any) -> dict[str, Any]:
    """讀取 → 更新指定鍵 → 寫回（整段加鎖）。"""
    with _lock:
        data = load()
        data.update(items)
        save(data)
        return data
