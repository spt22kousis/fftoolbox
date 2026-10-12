"""編碼器偵測 + 統一參數對應。

偵測：`ffmpeg -encoders` 只代表有編譯進去，所以對每個候選編碼器實際跑一次短編碼。
參數：UI 只有一套（品質值、速度 1..7、碼率上限、10bit、GOP、額外參數），
這裡把它翻譯成各編碼器自己的參數。之後要加 AV1 只要在 ENCODERS 加項目、
在 _quality_args / _speed_args 補對應即可。

CLI：
    python -m fftoolbox.encoders            顯示偵測結果（有快取就用快取）
    python -m fftoolbox.encoders --force    重新偵測
    python -m fftoolbox.encoders --args     列出各編碼器在預設參數下的指令片段
"""

from __future__ import annotations

import os
import platform
import re
import shlex
import shutil
import subprocess
import threading
from concurrent.futures import ThreadPoolExecutor
from dataclasses import asdict, dataclass, field, fields
from datetime import datetime
from pathlib import Path
from typing import Callable

from . import config
from .paths import COMPAT, MAIN, FFmpegBuild, compat_build, describe_build, main_build, run

# ---------------------------------------------------------------------------
# 編碼器定義
# ---------------------------------------------------------------------------

NVENC, QSV, AMF, SOFTWARE = "nvenc", "qsv", "amf", "software"
VENDOR_ORDER = [NVENC, QSV, AMF, SOFTWARE]  # 自動選擇優先序
VENDOR_LABELS = {NVENC: "NVIDIA NVENC", QSV: "Intel QSV", AMF: "AMD AMF", SOFTWARE: "軟體"}

H264, HEVC = "h264", "hevc"
CODEC_LABELS = {H264: "H.264", HEVC: "H.265 / HEVC"}


@dataclass(frozen=True)
class EncoderSpec:
    name: str  # ffmpeg 編碼器名稱
    codec: str  # H264 / HEVC
    vendor: str
    pix_fmt_8: str  # 8bit 輸出用的像素格式（明確指定，避免 10bit 來源被自動選成 10bit）
    pix_fmt_10: str | None  # None = 不支援 10bit 輸出

    @property
    def label(self) -> str:
        return f"{self.name}（{VENDOR_LABELS[self.vendor]} {CODEC_LABELS[self.codec]}）"


ENCODERS: list[EncoderSpec] = [
    EncoderSpec("h264_nvenc", H264, NVENC, "yuv420p", None),
    EncoderSpec("hevc_nvenc", HEVC, NVENC, "yuv420p", "p010le"),
    EncoderSpec("h264_qsv", H264, QSV, "nv12", None),
    EncoderSpec("hevc_qsv", HEVC, QSV, "nv12", "p010le"),
    EncoderSpec("h264_amf", H264, AMF, "nv12", None),
    EncoderSpec("hevc_amf", HEVC, AMF, "nv12", "p010le"),
    EncoderSpec("libx264", H264, SOFTWARE, "yuv420p", None),
    EncoderSpec("libx265", HEVC, SOFTWARE, "yuv420p", "yuv420p10le"),
]
ENCODER_MAP = {e.name: e for e in ENCODERS}


def encoders_for(codec: str) -> list[EncoderSpec]:
    """指定 codec 的編碼器，依自動選擇優先序排列。"""
    found = [e for e in ENCODERS if e.codec == codec]
    return sorted(found, key=lambda e: VENDOR_ORDER.index(e.vendor))


# ---------------------------------------------------------------------------
# 統一參數 → 各編碼器參數
# ---------------------------------------------------------------------------

SPEED_MIN, SPEED_MAX = 1, 7  # 1 = 最快，7 = 最慢（品質最好）
_SEVEN_PRESETS = ["veryfast", "faster", "fast", "medium", "slow", "slower", "veryslow"]
_AMF_QUALITY = ["speed", "speed", "balanced", "balanced", "balanced", "quality", "quality"]

QUALITY_MIN, QUALITY_MAX = 0, 51


def settings_from_dict(cls, d):
    """從 JSON（config、匯入的預設檔）建立設定物件：忽略未知欄位，
    型別依欄位預設值轉換（"4" → 4），轉不過去的保留預設值。"""
    if not isinstance(d, dict):
        d = {}
    kwargs = {}
    for f in fields(cls):
        if f.name not in d:
            continue
        default, value = f.default, d[f.name]
        try:
            if isinstance(default, bool):
                kwargs[f.name] = value if isinstance(value, bool) else str(value).lower() in ("1", "true", "yes")
            elif isinstance(default, int):
                kwargs[f.name] = int(value)
            elif isinstance(default, str):
                if value is not None:
                    kwargs[f.name] = str(value)
            else:
                kwargs[f.name] = value
        except (TypeError, ValueError):
            continue
    return cls(**kwargs)


@dataclass
class VideoSettings:
    mode: str = "auto"  # 一般轉檔時：auto（H.264/H.265 直接複製）/ encode / copy
    codec: str = H264
    encoder: str = "auto"  # "auto" 或 ENCODERS 裡的名稱
    quality: int = 20  # 越小品質越好（cq / global_quality / qp / crf）
    speed: int = 4  # SPEED_MIN..SPEED_MAX
    tenbit: bool = False
    maxrate: str = ""  # 例："20M"，空白 = 不限
    bufsize: str = ""  # 空白 = maxrate 的兩倍
    gop: int = 0  # 0 = 編碼器預設
    extra: str = ""  # 自訂額外參數，原樣加在最後

    @classmethod
    def from_dict(cls, d: dict) -> "VideoSettings":
        return settings_from_dict(cls, d)


def speed_value(spec: EncoderSpec, level: int) -> str:
    """速度等級在該編碼器上的實際值（給 UI 顯示用）。"""
    i = min(max(level, SPEED_MIN), SPEED_MAX) - 1
    if spec.vendor == NVENC:
        return f"p{i + 1}"
    if spec.vendor == AMF:
        return _AMF_QUALITY[i]
    return _SEVEN_PRESETS[i]


def _speed_args(spec: EncoderSpec, level: int) -> list[str]:
    value = speed_value(spec, level)
    if spec.vendor == AMF:
        return ["-quality", value]
    return ["-preset", value]


def _quality_args(spec: EncoderSpec, q: int) -> list[str]:
    q = str(min(max(int(q), QUALITY_MIN), QUALITY_MAX))
    if spec.vendor == NVENC:
        return ["-rc", "vbr", "-cq", q, "-b:v", "0"]
    if spec.vendor == QSV:
        return ["-global_quality", q]
    if spec.vendor == AMF:
        return ["-rc", "cqp", "-qp_i", q, "-qp_p", q]
    return ["-crf", q]


_BITRATE_RE = re.compile(r"^\s*(\d+(?:\.\d+)?)\s*([kKmMgG]?)\s*(?:b(?:ps)?)?\s*$")
_BITRATE_UNITS = {"": 1, "k": 1_000, "m": 1_000_000, "g": 1_000_000_000}


def parse_bitrate(text: str) -> int | None:
    """'20M' → 20000000；'8500k' → 8500000；無法解析回傳 None。"""
    m = _BITRATE_RE.match(text or "")
    if not m:
        return None
    return int(float(m.group(1)) * _BITRATE_UNITS[m.group(2).lower()])


def format_bitrate(bps: int) -> str:
    if bps % 1_000_000 == 0:
        return f"{bps // 1_000_000}M"
    if bps % 1_000 == 0:
        return f"{bps // 1_000}k"
    return str(bps)


def split_extra(text: str) -> list[str]:
    return shlex.split(text, posix=True) if text.strip() else []


def video_args(spec: EncoderSpec, s: VideoSettings) -> tuple[list[str], list[str]]:
    """回傳 (ffmpeg 視訊參數, 警告訊息)。"""
    warnings: list[str] = []
    args = ["-c:v", spec.name]

    pix_fmt = spec.pix_fmt_8
    if s.tenbit:
        if spec.pix_fmt_10:
            pix_fmt = spec.pix_fmt_10
        else:
            warnings.append(f"{spec.name} 不支援 10bit，改用 8bit 輸出")
    args += ["-pix_fmt", pix_fmt]

    args += _quality_args(spec, s.quality)
    args += _speed_args(spec, s.speed)

    if s.maxrate.strip():
        maxrate = parse_bitrate(s.maxrate)
        if maxrate is None:
            warnings.append(f"碼率上限「{s.maxrate}」格式錯誤（例：20M、8500k），已忽略")
        elif spec.vendor in (QSV, AMF):
            warnings.append(f"{spec.name} 的品質模式不支援碼率上限，已忽略")
        else:
            bufsize = parse_bitrate(s.bufsize) if s.bufsize.strip() else maxrate * 2
            if bufsize is None:
                warnings.append(f"緩衝大小「{s.bufsize}」格式錯誤，改用碼率上限的兩倍")
                bufsize = maxrate * 2
            args += ["-maxrate", format_bitrate(maxrate), "-bufsize", format_bitrate(bufsize)]

    if s.gop and s.gop > 0:
        args += ["-g", str(int(s.gop))]

    try:
        args += split_extra(s.extra)
    except ValueError as e:
        warnings.append(f"額外參數無法解析（{e}），已忽略")
    return args, warnings


# ---------------------------------------------------------------------------
# 偵測
# ---------------------------------------------------------------------------

TEST_INPUT = ["-f", "lavfi", "-i", "color=black:s=640x360:d=0.2"]
TEST_TIMEOUT = 30

_NOISE = (
    "Error while opening encoder",
    "Could not open encoder before EOF",
    "Task finished with error code",
    "Terminating thread with return code",
    "Nothing was written into output file",
    "Error sending frames to consumers",
    "Error while filtering",
    "Error initializing output stream",
    "Error marking filters as finished",
    "Error while processing the decoded data",
    "Conversion failed",
)
_ADDR_RE = re.compile(r" @ (?:0x)?[0-9a-fA-F]+\]")
_REQUIRED_DRIVER_RE = re.compile(r"minimum required Nvidia driver for nvenc is ([\d.]+)", re.I)


@dataclass
class EncoderStatus:
    name: str
    available: bool = False
    build: str | None = None  # MAIN / COMPAT：在哪一份 ffmpeg 上可用
    reason: str = ""  # 不可用原因（ffmpeg 原始錯誤訊息摘要）
    note: str = ""  # 可用但值得提示的事（例如改用相容版）
    tenbit: bool | None = None  # None = 不適用或未測
    tenbit_reason: str = ""
    required_driver: str | None = None  # 新版 ffmpeg 要求的最低 NVIDIA 驅動

    @classmethod
    def from_dict(cls, d: dict) -> "EncoderStatus":
        names = {f.name for f in fields(cls)}
        return cls(**{k: v for k, v in d.items() if k in names})


@dataclass
class DetectionReport:
    detected_at: str
    fingerprint: str
    nvidia_driver: str | None
    builds: dict[str, str]  # MAIN/COMPAT → ffmpeg 版本字串
    statuses: dict[str, EncoderStatus] = field(default_factory=dict)
    build_paths: dict[str, str] = field(default_factory=dict)  # MAIN/COMPAT → 路徑與來源

    def status(self, name: str) -> EncoderStatus:
        return self.statuses.get(name) or EncoderStatus(name, reason="尚未偵測")

    def pick(self, codec: str, tenbit: bool = False) -> EncoderSpec | None:
        """自動選擇：NVENC > QSV > AMF > 軟體；要 10bit 時跳過不支援的。"""
        for spec in encoders_for(codec):
            st = self.status(spec.name)
            if st.available and (not tenbit or st.tenbit):
                return spec
        if tenbit:  # 沒有能輸出 10bit 的，退而求其次
            return self.pick(codec, tenbit=False)
        return None

    def resolve(self, s: VideoSettings) -> tuple[EncoderSpec | None, str]:
        """依設定決定實際要用的編碼器，回傳 (編碼器, 錯誤訊息)。"""
        if s.encoder == "auto":
            spec = self.pick(s.codec, s.tenbit)
            if spec is None:
                return None, f"沒有可用的 {CODEC_LABELS.get(s.codec, s.codec)} 編碼器"
            return spec, ""
        spec = ENCODER_MAP.get(s.encoder)
        if spec is None:
            return None, f"未知的編碼器：{s.encoder}"
        st = self.status(spec.name)
        if not st.available:
            return None, f"{spec.name} 不可用：{st.reason}"
        return spec, ""

    def to_dict(self) -> dict:
        d = asdict(self)
        d["statuses"] = {k: asdict(v) for k, v in self.statuses.items()}
        return d

    @classmethod
    def from_dict(cls, d: dict) -> "DetectionReport":
        return cls(
            detected_at=d.get("detected_at", ""),
            fingerprint=d.get("fingerprint", ""),
            nvidia_driver=d.get("nvidia_driver"),
            builds=dict(d.get("builds", {})),
            statuses={k: EncoderStatus.from_dict(v) for k, v in d.get("statuses", {}).items()},
            build_paths=dict(d.get("build_paths", {})),
        )


def ffmpeg_version(build: FFmpegBuild) -> str:
    try:
        r = run([str(build.ffmpeg), "-hide_banner", "-version"], timeout=15)
        return r.stdout.splitlines()[0].strip() if r.stdout else ""
    except (OSError, subprocess.TimeoutExpired):
        return ""


def compiled_encoders(build: FFmpegBuild) -> set[str]:
    """`ffmpeg -encoders` 列出的編碼器名稱（只代表有編譯進去）。"""
    try:
        r = run([str(build.ffmpeg), "-hide_banner", "-encoders"], timeout=15)
    except (OSError, subprocess.TimeoutExpired):
        return set()
    names = set()
    for line in r.stdout.splitlines():
        parts = line.split()
        if len(parts) >= 2 and len(parts[0]) == 6 and parts[0][0] in "VAS.":
            names.add(parts[1])
    return names


def summarize_error(stderr: str, encoder: str, max_lines: int = 5) -> str:
    """從 ffmpeg stderr 挑出真正的錯誤原因（最後幾行通常只有 Conversion failed）。"""
    lines = [_ADDR_RE.sub("]", ln.strip()) for ln in stderr.splitlines() if ln.strip()]
    useful = [ln for ln in lines if not any(n in ln for n in _NOISE)]
    tagged = [ln for ln in useful if f"[{encoder}]" in ln or f"enc:{encoder}]" in ln]
    picked = tagged or useful or lines
    return "\n".join(picked[-max_lines:]) or "未知錯誤"


def is_api_version_error(stderr: str) -> bool:
    return "nvenc api version" in stderr.lower()


def required_driver(stderr: str) -> str | None:
    m = _REQUIRED_DRIVER_RE.search(stderr)
    return m.group(1) if m else None


def nvidia_driver_version() -> str | None:
    """用 nvidia-smi 讀目前 NVIDIA 驅動版本；沒有 NVIDIA 顯示卡回傳 None。"""
    candidates = [shutil.which("nvidia-smi")]
    windir = Path(os.environ.get("WINDIR", r"C:\Windows"))
    candidates += [
        str(windir / "System32" / "nvidia-smi.exe"),
        r"C:\Program Files\NVIDIA Corporation\NVSMI\nvidia-smi.exe",
    ]
    for exe in candidates:
        if not exe or not Path(exe).is_file():
            continue
        try:
            r = run([exe, "--query-gpu=driver_version", "--format=csv,noheader"], timeout=15)
        except (OSError, subprocess.TimeoutExpired):
            continue
        if r.returncode == 0 and r.stdout.strip():
            return r.stdout.strip().splitlines()[0].strip()
    return None


def test_encoder(build: FFmpegBuild, spec: EncoderSpec, tenbit: bool = False) -> tuple[bool, str]:
    """實測一次短編碼，回傳 (成功, stderr)。"""
    pix_fmt = spec.pix_fmt_10 if tenbit else spec.pix_fmt_8
    cmd = [str(build.ffmpeg), "-hide_banner", "-nostdin", "-loglevel", "warning",
           *TEST_INPUT, "-c:v", spec.name, "-pix_fmt", pix_fmt, "-f", "null", "-"]
    try:
        r = run(cmd, timeout=TEST_TIMEOUT)
    except subprocess.TimeoutExpired:
        return False, f"測試逾時（超過 {TEST_TIMEOUT} 秒）"
    except OSError as e:
        return False, f"無法執行 ffmpeg：{e}"
    ok = r.returncode == 0
    # 編碼器不支援指定格式時 ffmpeg 會自動換格式而且照樣成功，要另外判斷
    if ok and "Incompatible pixel format" in r.stderr:
        ok = False
    return ok, r.stderr


def driver_hint(required: str | None, current: str | None) -> str:
    if not required:
        return ""
    cur = f"目前驅動 {current}，" if current else ""
    return f"{cur}更新 NVIDIA 驅動至 {required} 以上可使用新版 ffmpeg"


def _detect_one(spec: EncoderSpec, builds: list[FFmpegBuild], compiled: dict[str, set[str]],
                driver: str | None) -> EncoderStatus:
    st = EncoderStatus(spec.name)
    main = builds[0]
    if spec.name not in compiled[main.name]:
        st.reason = "此 ffmpeg 未編譯此編碼器"
        return st

    ok, err = test_encoder(main, spec)
    used = main
    if not ok and spec.vendor == NVENC and is_api_version_error(err):
        st.required_driver = required_driver(err)
        hint = driver_hint(st.required_driver, driver)
        compat = builds[1] if len(builds) > 1 else None
        if compat is not None and spec.name in compiled[compat.name]:
            ok2, err2 = test_encoder(compat, spec)
            if ok2:
                ok, used = True, compat
                st.note = f"驅動較舊，改用相容版 ffmpeg。{hint}"
            else:
                err = err2
                st.reason = f"{summarize_error(err2, spec.name)}\n（新版與相容版都失敗）{hint}"
        else:
            st.reason = f"{summarize_error(err, spec.name)}\n{hint}（未附相容版 ffmpeg）"
    if not ok:
        st.reason = st.reason or summarize_error(err, spec.name)
        return st

    st.available, st.build = True, used.name
    if spec.pix_fmt_10:
        ok10, err10 = test_encoder(used, spec, tenbit=True)
        st.tenbit = ok10
        if not ok10:
            st.tenbit_reason = summarize_error(err10, spec.name)
    return st


def machine_fingerprint(driver: str | None) -> str:
    parts = [platform.node(), main_build().fingerprint(), compat_build().fingerprint(),
             f"nvidia:{driver or '-'}"]
    return "|".join(parts)


def detect(progress: Callable[[str], None] | None = None) -> DetectionReport:
    """實測全部候選編碼器。不同廠商平行測，同一廠商依序測（避免硬體 session 數限制）。"""
    main = main_build()
    if not main.exists():
        raise FileNotFoundError(f"找不到 ffmpeg：{main.ffmpeg}")
    builds = [main]
    compat = compat_build()
    if compat.exists():
        builds.append(compat)

    driver = nvidia_driver_version()
    compiled = {b.name: compiled_encoders(b) for b in builds}
    report = DetectionReport(
        detected_at=datetime.now().isoformat(timespec="seconds"),
        fingerprint=machine_fingerprint(driver),
        nvidia_driver=driver,
        builds={b.name: ffmpeg_version(b) for b in builds},
        build_paths={b.name: describe_build(b) for b in builds},
    )

    lock = threading.Lock()

    def run_vendor(vendor: str) -> None:
        for spec in (e for e in ENCODERS if e.vendor == vendor):
            if progress:
                progress(f"測試 {spec.name}…")
            st = _detect_one(spec, builds, compiled, driver)
            with lock:
                report.statuses[spec.name] = st

    with ThreadPoolExecutor(max_workers=len(VENDOR_ORDER)) as pool:
        list(pool.map(run_vendor, VENDOR_ORDER))

    report.statuses = {e.name: report.statuses[e.name] for e in ENCODERS}
    return report


CONFIG_KEY = "encoder_detection"


def load_cached() -> DetectionReport | None:
    d = config.load().get(CONFIG_KEY)
    return DetectionReport.from_dict(d) if isinstance(d, dict) else None


def save_cache(report: DetectionReport) -> None:
    config.update(**{CONFIG_KEY: report.to_dict()})


def get_report(force: bool = False, progress: Callable[[str], None] | None = None) -> DetectionReport:
    """有效的快取就直接用；ffmpeg、電腦或 NVIDIA 驅動變了就重測。"""
    if not force:
        cached = load_cached()
        if cached and cached.fingerprint == machine_fingerprint(nvidia_driver_version()):
            return cached
    report = detect(progress)
    save_cache(report)
    return report


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def format_report(report: DetectionReport) -> str:
    out = [f"偵測時間：{report.detected_at}"]
    for name, ver in report.builds.items():
        out.append(f"ffmpeg[{name}]：{ver}")
        if name in report.build_paths:
            out.append(f"    {report.build_paths[name]}")
    out.append(f"NVIDIA 驅動：{report.nvidia_driver or '（無）'}")
    out.append("")
    for spec in ENCODERS:
        st = report.status(spec.name)
        if st.available:
            tb = {None: "", True: "  10bit ✓", False: "  10bit ✗"}[st.tenbit]
            build = "" if st.build == MAIN else f"  [{st.build}]"
            out.append(f"  ✓ {spec.name:<11}{tb}{build}")
            if st.note:
                out.append(f"      {st.note}")
            if st.tenbit is False:
                out.append("      10bit：" + st.tenbit_reason.replace("\n", "\n             "))
        else:
            out.append(f"  ✗ {spec.name:<11}")
            out.append("      " + st.reason.replace("\n", "\n      "))
    out.append("")
    for codec in (H264, HEVC):
        spec = report.pick(codec)
        out.append(f"自動選擇 {CODEC_LABELS[codec]}：{spec.name if spec else '（無）'}")
    return "\n".join(out)


def main(argv: list[str] | None = None) -> int:
    import argparse
    import sys

    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, "reconfigure"):
            stream.reconfigure(encoding="utf-8")

    p = argparse.ArgumentParser(prog="python -m fftoolbox.encoders", description="編碼器偵測")
    p.add_argument("--force", action="store_true", help="忽略快取重新偵測")
    p.add_argument("--args", action="store_true", help="列出各編碼器在預設參數下的指令片段")
    a = p.parse_args(argv)

    if a.args:
        s = VideoSettings()
        for spec in ENCODERS:
            args, warns = video_args(spec, s)
            print(f"{spec.name:<11} {' '.join(args)}" + (f"  ⚠ {warns}" if warns else ""))
        return 0

    try:
        report = get_report(force=a.force, progress=lambda m: print(m, file=sys.stderr))
    except FileNotFoundError as e:
        print(e, file=sys.stderr)
        return 1
    print(format_report(report))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
