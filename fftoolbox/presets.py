"""預設檔：編碼參數存成 JSON，可匯出/匯入給其他社員。

預設檔存在 config.json 的 "presets"；匯出檔格式：
    {"format": "fftoolbox-preset", "version": 1, "name": "...", "video": {...}, "audio": {...}, "container": "mp4"}
"""

from __future__ import annotations

import json
from dataclasses import asdict, dataclass, field
from pathlib import Path

from . import config
from .encoders import VideoSettings
from .job import CONTAINERS, AudioSettings

FORMAT = "fftoolbox-preset"
VERSION = 1


@dataclass
class Preset:
    video: VideoSettings = field(default_factory=VideoSettings)
    audio: AudioSettings = field(default_factory=AudioSettings)
    container: str = "mp4"

    def to_dict(self) -> dict:
        return {"video": asdict(self.video), "audio": asdict(self.audio), "container": self.container}

    @classmethod
    def from_dict(cls, d: dict) -> "Preset":
        if not isinstance(d, dict):
            d = {}
        container = d.get("container", "mp4")
        return cls(
            video=VideoSettings.from_dict(d.get("video")),
            audio=AudioSettings.from_dict(d.get("audio")),
            container=container if container in CONTAINERS else "mp4",
        )


def load_all() -> dict[str, Preset]:
    raw = config.load().get("presets") or {}
    out = {}
    for name, d in raw.items():
        try:
            out[name] = Preset.from_dict(d)
        except (TypeError, ValueError):
            continue
    return out


def save_all(presets: dict[str, Preset]) -> None:
    config.update(presets={k: v.to_dict() for k, v in presets.items()})


def export_preset(path: str | Path, name: str, preset: Preset) -> None:
    data = {"format": FORMAT, "version": VERSION, "name": name, **preset.to_dict()}
    Path(path).write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")


def import_preset(path: str | Path) -> tuple[str, Preset]:
    try:
        data = json.loads(Path(path).read_text(encoding="utf-8-sig"))
    except (OSError, ValueError) as e:
        raise ValueError(f"無法讀取預設檔：{e}")
    if not isinstance(data, dict) or data.get("format") != FORMAT:
        raise ValueError("不是本工具的預設檔")
    try:
        preset = Preset.from_dict(data)
    except (TypeError, ValueError) as e:
        raise ValueError(f"預設檔內容有誤：{e}")
    return str(data.get("name") or Path(path).stem), preset
