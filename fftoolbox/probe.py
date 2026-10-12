"""ffprobe 來源資訊。"""

from __future__ import annotations

import json
import subprocess
from dataclasses import dataclass, field
from pathlib import Path

from .paths import FFmpegBuild, main_build, run


@dataclass
class MediaInfo:
    path: Path
    duration: float | None = None
    format_name: str = ""
    bit_rate: int | None = None
    video: list[dict] = field(default_factory=list)
    audio: list[dict] = field(default_factory=list)
    subtitles: list[dict] = field(default_factory=list)
    attachments: list[dict] = field(default_factory=list)

    @property
    def main_video(self) -> dict | None:
        real = [v for v in self.video if not v.get("disposition", {}).get("attached_pic")]
        return real[0] if real else None

    def summary(self) -> str:
        lines = [f"格式：{self.format_name}　長度：{format_duration(self.duration)}"
                 + (f"　總碼率：{self.bit_rate // 1000} kb/s" if self.bit_rate else "")]
        for v in self.video:
            if v.get("disposition", {}).get("attached_pic"):
                continue
            fps = frame_rate(v)
            bits = _bit_depth(v)
            lines.append(
                f"視訊：{v.get('codec_name', '?')}"
                + (f" ({v['profile']})" if v.get("profile") else "")
                + f"　{v.get('width', '?')}x{v.get('height', '?')}"
                + (f"　{fps:.3f} fps" if fps else "")
                + f"　{v.get('pix_fmt', '?')}" + (f"（{bits}bit）" if bits else "")
            )
        for a in self.audio:
            br = a.get("bit_rate")
            lang = a.get("tags", {}).get("language")
            lines.append(
                f"音訊：{a.get('codec_name', '?')}　{a.get('sample_rate', '?')} Hz"
                + f"　{a.get('channels', '?')} 聲道"
                + (f"（{a['channel_layout']}）" if a.get("channel_layout") else "")
                + (f"　{int(br) // 1000} kb/s" if br and str(br).isdigit() else "")
                + (f"　[{lang}]" if lang else "")
            )
        if self.subtitles:
            lines.append("字幕軌：" + "、".join(s.get("codec_name", "?") for s in self.subtitles))
        if self.attachments:
            lines.append(f"附件：{len(self.attachments)} 個（多半是字型）")
        return "\n".join(lines)


def frame_rate(stream: dict) -> float | None:
    for key in ("avg_frame_rate", "r_frame_rate"):
        num, _, den = str(stream.get(key, "")).partition("/")
        try:
            n, d = float(num), float(den or 1)
        except ValueError:
            continue
        if n > 0 and d > 0:
            return n / d
    return None


def _bit_depth(stream: dict) -> int | None:
    raw = stream.get("bits_per_raw_sample")
    if raw and str(raw).isdigit():
        return int(raw)
    pf = stream.get("pix_fmt", "")
    for depth in (16, 12, 10):
        if str(depth) in pf:
            return depth
    return 8 if pf else None


def format_duration(seconds: float | None) -> str:
    if seconds is None:
        return "?"
    s = int(seconds)
    return f"{s // 3600:d}:{s % 3600 // 60:02d}:{s % 60:02d}.{int((seconds - s) * 100):02d}"


def probe(path: str | Path, build: FFmpegBuild | None = None) -> MediaInfo:
    build = build or main_build()
    cmd = [str(build.ffprobe), "-v", "error", "-print_format", "json",
           "-show_format", "-show_streams", str(path)]
    try:
        r = run(cmd, timeout=60)
    except subprocess.TimeoutExpired:
        raise RuntimeError("ffprobe 逾時")
    if r.returncode != 0:
        raise RuntimeError(r.stderr.strip() or f"ffprobe 失敗（{r.returncode}）")
    data = json.loads(r.stdout or "{}")
    fmt = data.get("format", {})
    info = MediaInfo(Path(path), format_name=fmt.get("format_long_name") or fmt.get("format_name", ""))
    try:
        info.duration = float(fmt["duration"])
    except (KeyError, ValueError):
        pass
    try:
        info.bit_rate = int(fmt["bit_rate"])
    except (KeyError, ValueError):
        pass
    for s in data.get("streams", []):
        kind = s.get("codec_type")
        {"video": info.video, "audio": info.audio, "subtitle": info.subtitles,
         "attachment": info.attachments}.get(kind, []).append(s)
    if info.duration is None:
        for s in info.video + info.audio:
            try:
                info.duration = float(s["duration"])
                break
            except (KeyError, ValueError):
                continue
    return info
