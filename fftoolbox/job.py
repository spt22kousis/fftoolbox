"""組 ffmpeg 指令、執行、進度解析。不依賴 Qt，CLI 與 GUI 共用。

CLI 範例：
    python -m fftoolbox.job burn  影片.mkv --ass 字幕.ass --fontsdir fonts -o out.mp4
    python -m fftoolbox.job audio 影片.mkv
    python -m fftoolbox.job transcode 影片.mov --codec hevc --quality 24
    python -m fftoolbox.job preview 影片.mkv --ass 字幕.ass -t 1:23.5 -o shot.png
"""

from __future__ import annotations

import re
import subprocess
import threading
import time
from collections import deque
from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path
from typing import Callable

from . import ass_fonts
from .encoders import (DetectionReport, EncoderSpec, VideoSettings, settings_from_dict, split_extra,
                       video_args)
from .paths import CREATE_NO_WINDOW, FFmpegBuild, get_build, main_build, run
from .probe import MediaInfo, probe


class Task(str, Enum):
    BURN = "burn"
    AUDIO = "audio"
    TRANSCODE = "transcode"


TASK_LABELS = {Task.BURN: "燒錄字幕", Task.AUDIO: "只轉音訊", Task.TRANSCODE: "一般轉檔"}
TASK_SUFFIX = {Task.BURN: "_sub", Task.AUDIO: "_aac", Task.TRANSCODE: "_enc"}
CONTAINERS = ["mp4", "mkv"]
MP4_LIKE = {".mp4", ".m4v", ".mov", ".m4a"}
# 只轉音訊時可以沿用的來源容器（放得下 AAC）；其他的改用 mkv（有視訊）或 m4a（純音訊）
AAC_CONTAINERS = {".mp4", ".m4v", ".mov", ".m4a", ".mkv", ".mka", ".ts", ".m2ts", ".mts", ".flv", ".3gp"}
AUDIO_ONLY_EXTS = {".flac", ".wav", ".mp3", ".ogg", ".oga", ".opus", ".wma", ".aif", ".aiff",
                   ".ape", ".wv", ".tta", ".ac3", ".dts", ".aac", ".m4a", ".mka"}


class JobError(Exception):
    pass


# 串流處理方式
AUTO, ENCODE, COPY = "auto", "encode", "copy"
VIDEO_MODES = {AUTO: "自動（H.264 / H.265 直接複製）", ENCODE: "重新編碼", COPY: "直接複製"}
AUDIO_MODES = {AUTO: "自動（AAC 直接複製）", ENCODE: "轉成 AAC", COPY: "直接複製"}
COPYABLE_VIDEO = {"h264", "hevc"}
# 直接複製時相容性沒問題的像素格式；其他（Hi10P、4:2:2、4:4:4…）照樣複製但提醒
SAFE_PIX_FMTS = {"h264": {"yuv420p", "yuvj420p"},
                 "hevc": {"yuv420p", "yuvj420p", "yuv420p10le"}}


@dataclass
class AudioSettings:
    mode: str = AUTO  # auto（AAC 直接複製）/ encode / copy；只轉音訊任務一律轉檔
    bitrate: str = "256k"
    sample_rate: int = 0  # 0 = 保持原始
    channels: int = 0  # 0 = 保持原始

    @classmethod
    def from_dict(cls, d: dict) -> "AudioSettings":
        return settings_from_dict(cls, d)


def audio_args(a: AudioSettings, index: int | None = None) -> list[str]:
    """AAC 編碼參數；index 指定時只套用到第 index 條輸出音軌。"""
    sfx = "" if index is None else f":{index}"
    args = [f"-c:a{sfx}", "aac", f"-b:a{sfx}", a.bitrate or "256k"]
    if a.sample_rate:
        args += [f"-ar{':a' + sfx if sfx else ''}", str(a.sample_rate)]
    if a.channels:
        args += [f"-ac{':a' + sfx if sfx else ''}", str(a.channels)]
    return args


@dataclass
class StreamPlan:
    """每條串流要複製還是重新編碼。"""
    video_copy: bool
    audio_copy: list[bool]  # 依來源音軌順序；來源資訊不明時為空
    audio_copy_all: bool  # 來源資訊不明時用
    notes: list[str]  # 給人看的說明
    warnings: list[str]


def plan_streams(task: "Task", video: VideoSettings, audio: AudioSettings,
                 info: MediaInfo | None) -> StreamPlan:
    notes: list[str] = []
    warnings: list[str] = []
    v = info.main_video if info else None
    vcodec = (v or {}).get("codec_name", "")

    # 視訊
    if task == Task.AUDIO:
        video_copy = True
    elif task == Task.BURN:
        video_copy = False
        if video.mode == COPY:
            warnings.append("燒錄字幕必須重新編碼視訊，已忽略「直接複製」")
    elif video.mode == COPY:
        video_copy = True
    elif video.mode == ENCODE:
        video_copy = False
    else:
        video_copy = vcodec in COPYABLE_VIDEO
    src = f"（來源 {vcodec}）" if vcodec else ""
    notes.append(f"視訊：{'直接複製' if video_copy else '重新編碼'}{src}")
    if video_copy and task == Task.TRANSCODE and v is not None:
        pix = v.get("pix_fmt", "")
        safe = SAFE_PIX_FMTS.get(vcodec)
        if safe is not None and pix and pix not in safe:
            profile = v.get("profile") or ""
            warnings.append(f"來源視訊是 {vcodec} {profile} {pix}，直接複製後部分手機/電視/網站可能無法播放；"
                            f"需要的話把視訊處理改成「重新編碼」")

    # 音訊
    mode = ENCODE if task == Task.AUDIO else audio.mode
    forced = bool(audio.sample_rate or audio.channels)
    if mode == COPY and forced:
        warnings.append("音訊設為直接複製，取樣率／聲道設定不會生效")
    copies: list[bool] = []
    for i, a in enumerate(info.audio if info else []):
        acodec = a.get("codec_name", "?")
        if mode == COPY:
            c = True
        elif mode == ENCODE:
            c = False
        else:
            c = acodec == "aac" and not forced
        copies.append(c)
        label = f"音訊 {i + 1}" if len(info.audio) > 1 else "音訊"
        notes.append(f"{label}：{'直接複製' if c else '轉成 AAC'}（來源 {acodec}）")
    if info is not None and not info.audio:
        notes.append("音訊：（來源沒有音軌）")
    return StreamPlan(video_copy, copies, mode == COPY, notes, warnings)


def audio_stream_args(a: AudioSettings, plan: StreamPlan) -> list[str]:
    copies = plan.audio_copy
    if not copies:
        return ["-c:a", "copy"] if plan.audio_copy_all else audio_args(a)
    if all(copies):
        return ["-c:a", "copy"]
    if not any(copies):
        return audio_args(a)
    args: list[str] = []
    for i, c in enumerate(copies):
        args += [f"-c:a:{i}", "copy"] if c else audio_args(a, i)
    return args


@dataclass
class Job:
    task: Task
    input: Path
    output: Path
    subtitle: Path | None = None
    fonts_dir: Path | None = None
    video: VideoSettings = field(default_factory=VideoSettings)
    audio: AudioSettings = field(default_factory=AudioSettings)


# ---------------------------------------------------------------------------
# 路徑與跳脫
# ---------------------------------------------------------------------------

def escape_filter_value(value: str) -> str:
    """濾鏡選項值的兩層跳脫：先是選項層（\\ ' :），再是 filtergraph 層（\\ ' [ ] , ;）。
    Windows 路徑先轉成正斜線，減少反斜線數量。"""
    v = value.replace("\\", "/")
    v = re.sub(r"([\\':])", r"\\\1", v)
    return re.sub(r"([\\'\[\],;])", r"\\\1", v)


def subtitles_filter(ass: Path, fonts_dir: Path | None = None) -> str:
    opts = [f"filename={escape_filter_value(str(ass))}"]
    if fonts_dir:
        opts.append(f"fontsdir={escape_filter_value(str(fonts_dir))}")
    return "subtitles=" + ":".join(opts)


def subtitle_for_ffmpeg(ass: Path) -> tuple[Path, Path | None, str]:
    """回傳 (給 ffmpeg 的路徑, 暫存檔或 None, 原始編碼)。非 UTF-8 的 ASS 會轉存成 UTF-8 暫存檔。"""
    text, enc = ass_fonts.read_ass(ass)
    if ass_fonts.is_utf8(enc):
        return ass, None, enc
    tmp = ass_fonts.write_utf8_temp(text, ass.stem)
    return tmp, tmp, enc


def default_output(input: Path, task: Task, container: str = "mp4", out_dir: Path | None = None) -> Path:
    if task == Task.AUDIO:
        ext = input.suffix.lower()  # 視訊直接複製，盡量沿用原本的容器
        if ext not in AAC_CONTAINERS:  # webm、ogv、avi、flac、wav… 放不了 AAC
            ext = ".m4a" if ext in AUDIO_ONLY_EXTS else ".mkv"
    else:
        ext = "." + container
    return (out_dir or input.parent) / f"{input.stem}{TASK_SUFFIX[task]}{ext}"


def unique_path(p: Path, taken: set[Path] | None = None) -> Path:
    """檔案已存在（或已被佇列裡其他工作佔用）就加上 (2)、(3)…"""
    taken = taken or set()
    candidate, n = p, 2
    while candidate.exists() or candidate in taken:
        candidate = p.with_name(f"{p.stem} ({n}){p.suffix}")
        n += 1
    return candidate


def command_text(args: list[str]) -> str:
    """可貼到 cmd / PowerShell 執行的完整指令。"""
    return subprocess.list2cmdline(args)


# ---------------------------------------------------------------------------
# 組指令
# ---------------------------------------------------------------------------

@dataclass
class BuiltCommand:
    args: list[str]
    build: FFmpegBuild
    encoder: EncoderSpec | None
    warnings: list[str]
    temp_files: list[Path] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)  # 各串流處理方式說明

    @property
    def text(self) -> str:
        return command_text(self.args)

    def cleanup(self) -> None:
        for p in self.temp_files:
            try:
                p.unlink(missing_ok=True)
            except OSError:
                pass


def build_command(job: Job, report: DetectionReport | None,
                  info: MediaInfo | None = None) -> BuiltCommand:
    """info 是來源的 ffprobe 結果；沒給的話這裡會自己讀（決定串流要複製還是編碼）。"""
    if not job.input.is_file():
        raise JobError(f"找不到輸入檔：{job.input}")
    if job.output.resolve() == job.input.resolve():
        raise JobError("輸出檔不能跟輸入檔相同")
    if info is None:
        try:
            info = probe(job.input)
        except (RuntimeError, OSError, ValueError):
            info = None

    plan = plan_streams(job.task, job.video, job.audio, info)
    warnings: list[str] = list(plan.warnings)
    temps: list[Path] = []
    out_ext = job.output.suffix.lower()
    common = ["-hide_banner", "-nostdin", "-y", "-loglevel", "warning",
              "-nostats", "-progress", "pipe:1"]

    encoder = None
    build = main_build()
    if not plan.video_copy:
        if report is None:
            raise JobError("尚未偵測編碼器")
        encoder, err = report.resolve(job.video)
        if encoder is None:
            raise JobError(err)
        build = get_build(report.status(encoder.name).build or "main")
    if not build.exists():
        raise JobError(f"找不到 ffmpeg：{build.ffmpeg}")

    args = [str(build.ffmpeg), *common, "-i", str(job.input)]
    if job.task == Task.AUDIO:
        args += ["-map", "0:V?", "-map", "0:a"]
        if out_ext == ".mkv":  # mp4 放不了 ASS 字幕與字型附件
            args += ["-map", "0:s?", "-map", "0:t?"]
        args += ["-c:v", "copy", "-c:s", "copy", "-c:t", "copy"]
    else:
        args += ["-map", "0:V:0", "-map", "0:a?"]

    if plan.video_copy:
        if job.task != Task.AUDIO:
            args += ["-c:v", "copy"]
        vcodec = ((info.main_video if info else None) or {}).get("codec_name")
        if vcodec == "hevc" and out_ext in MP4_LIKE:
            args += ["-tag:v", "hvc1"]  # Apple 裝置 / QuickTime 需要 hvc1
    else:
        vargs, vwarn = video_args(encoder, job.video)
        warnings += vwarn

        if job.task == Task.BURN:
            if not job.subtitle:
                raise JobError("沒有指定字幕檔")
            if not job.subtitle.is_file():
                raise JobError(f"找不到字幕檔：{job.subtitle}")
            if job.fonts_dir and not job.fonts_dir.is_dir():
                raise JobError(f"找不到字型資料夾：{job.fonts_dir}")
            sub, tmp, enc = subtitle_for_ffmpeg(job.subtitle)
            if tmp:
                temps.append(tmp)
                warnings.append(f"字幕檔是 {enc} 編碼，已轉成 UTF-8 暫存檔再交給 ffmpeg")
            try:
                extra = split_extra(job.video.extra)
            except ValueError:
                extra = []
            if any(a in ("-vf", "-filter:v", "-filter_complex") for a in extra):
                warnings.append("額外參數裡的 -vf / -filter_complex 會蓋掉字幕濾鏡")
            args += ["-vf", subtitles_filter(sub, job.fonts_dir)]
        args += vargs

    args += audio_stream_args(job.audio, plan)
    if out_ext in MP4_LIKE:
        args += ["-movflags", "+faststart"]
    args.append(str(job.output))
    return BuiltCommand(args, build, encoder, warnings, temps, plan.notes)


# ---------------------------------------------------------------------------
# 執行與進度
# ---------------------------------------------------------------------------

@dataclass
class Progress:
    fraction: float | None  # 0..1；長度未知時為 None
    out_time: float  # 已處理的影片秒數
    speed: float | None  # 倍速
    fps: float | None
    elapsed: float  # 實際經過秒數
    eta: float | None  # 預估剩餘秒數
    done: bool = False


class ProgressParser:
    """解析 `-progress pipe:1` 的 key=value 輸出，每收到一組 progress= 產生一次 Progress。"""

    def __init__(self, duration: float | None) -> None:
        self.duration = duration if duration and duration > 0 else None
        self.values: dict[str, str] = {}
        self.start = time.monotonic()

    def feed(self, line: str) -> Progress | None:
        key, sep, value = line.strip().partition("=")
        if not sep:
            return None
        self.values[key] = value.strip()
        if key != "progress":
            return None
        return self._snapshot(done=value.strip() == "end")

    def _snapshot(self, done: bool) -> Progress:
        v = self.values
        out_time = 0.0
        for key in ("out_time_us", "out_time_ms"):  # 兩者單位其實都是微秒
            try:
                out_time = max(0.0, int(v[key]) / 1_000_000)
                break
            except (KeyError, ValueError):
                continue
        speed = _float(v.get("speed", "").rstrip("x"))
        fps = _float(v.get("fps", ""))
        elapsed = time.monotonic() - self.start
        fraction = None
        eta = None
        if self.duration:
            fraction = 1.0 if done else min(out_time / self.duration, 1.0)
            if done:
                eta = 0.0
            elif fraction > 0.01:
                eta = elapsed * (1 - fraction) / fraction
            elif speed:
                eta = (self.duration - out_time) / speed
        return Progress(fraction, out_time, speed, fps, elapsed, eta, done)


def _float(s: str) -> float | None:
    try:
        return float(s)
    except ValueError:
        return None


class FFmpegRunner:
    """執行 ffmpeg，stdout 讀進度、stderr 讀日誌（各自一條執行緒，避免管線塞住）。"""

    def __init__(self, args: list[str], duration: float | None = None,
                 on_progress: Callable[[Progress], None] | None = None,
                 on_log: Callable[[str], None] | None = None) -> None:
        self.args = args
        self.parser = ProgressParser(duration)
        self.on_progress = on_progress
        self.on_log = on_log
        self.log_tail: deque[str] = deque(maxlen=300)
        self.cancelled = False
        self._proc: subprocess.Popen | None = None
        self._lock = threading.Lock()

    def run(self) -> int:
        with self._lock:
            if self.cancelled:
                return -1
            self._proc = subprocess.Popen(
                self.args, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                encoding="utf-8", errors="replace", creationflags=CREATE_NO_WINDOW)
        proc = self._proc
        err_thread = threading.Thread(target=self._read_stderr, args=(proc,), daemon=True)
        err_thread.start()
        for line in proc.stdout:
            p = self.parser.feed(line)
            if p and self.on_progress:
                self.on_progress(p)
        rc = proc.wait()
        err_thread.join(timeout=5)
        return rc

    def _read_stderr(self, proc: subprocess.Popen) -> None:
        for line in proc.stderr:
            line = line.rstrip()
            if not line:
                continue
            self.log_tail.append(line)
            if self.on_log:
                self.on_log(line)

    def cancel(self) -> None:
        with self._lock:
            self.cancelled = True
            if self._proc and self._proc.poll() is None:
                self._proc.kill()


@dataclass
class JobResult:
    ok: bool
    cancelled: bool = False
    returncode: int | None = None
    error: str = ""
    command: str = ""
    warnings: list[str] = field(default_factory=list)
    elapsed: float = 0.0


def error_summary(log_lines: list[str], max_lines: int = 8) -> str:
    noise = ("Conversion failed", "Terminating thread", "Task finished with error")
    useful = [ln for ln in log_lines if not any(n in ln for n in noise)]
    return "\n".join((useful or log_lines)[-max_lines:]) or "ffmpeg 失敗（沒有錯誤訊息）"


def run_job(job: Job, report: DetectionReport | None,
            on_progress: Callable[[Progress], None] | None = None,
            on_log: Callable[[str], None] | None = None,
            on_command: Callable[[BuiltCommand], None] | None = None,
            on_runner: Callable[[FFmpegRunner], None] | None = None) -> JobResult:
    """執行一個工作（阻塞）。on_runner 會拿到 runner，用來取消。"""
    start = time.monotonic()
    try:
        info = probe(job.input)
    except (RuntimeError, OSError, ValueError):
        info = None
    try:
        cmd = build_command(job, report, info)
    except (JobError, ValueError, OSError) as e:
        return JobResult(False, error=str(e))
    if on_command:
        on_command(cmd)
    duration = info.duration if info else None

    runner = FFmpegRunner(cmd.args, duration, on_progress, on_log)
    if on_runner:
        on_runner(runner)
    try:
        job.output.parent.mkdir(parents=True, exist_ok=True)
        rc = runner.run()
    except OSError as e:
        return JobResult(False, error=f"無法執行 ffmpeg：{e}", command=cmd.text, warnings=cmd.warnings)
    finally:
        cmd.cleanup()

    result = JobResult(rc == 0 and not runner.cancelled, runner.cancelled, rc,
                       command=cmd.text, warnings=cmd.warnings, elapsed=time.monotonic() - start)
    if not result.ok:
        result.error = "已取消" if runner.cancelled else error_summary(list(runner.log_tail))
        try:
            job.output.unlink(missing_ok=True)  # 半成品刪掉
        except OSError:
            pass
    return result


# ---------------------------------------------------------------------------
# 字幕預覽截圖
# ---------------------------------------------------------------------------

_TIME_RE = re.compile(r"^\s*(?:(\d+):)?(?:(\d+):)?(\d+(?:\.\d+)?)\s*$")


def parse_time(text: str) -> float:
    """'83.5'、'1:23.5'、'0:01:23.5' → 秒數。"""
    m = _TIME_RE.match(text or "")
    if not m:
        raise ValueError(f"時間格式錯誤：{text}（例：1:23.5）")
    a, b, sec = m.groups()
    if b is not None:
        h, mnt = int(a), int(b)
    else:
        h, mnt = 0, int(a or 0)
    return h * 3600 + mnt * 60 + float(sec)


def preview_command(video: Path, t: float, out_png: Path, ass: Path | None = None,
                    fonts_dir: Path | None = None) -> tuple[list[str], Path | None]:
    """在 t 秒輸出一張截圖，回傳 (指令, 暫存字幕檔或 None)。
    -ss 放在 -i 前面（快速且精確），但時間戳會從 0 開始，
    所以先用 setpts 把時間加回去，字幕才會對到正確的時間點。"""
    build = main_build()
    vf = [f"setpts=PTS+{t:.3f}/TB"]
    tmp = None
    if ass:
        sub, tmp, _ = subtitle_for_ffmpeg(ass)
        vf.append(subtitles_filter(sub, fonts_dir))
    cmd = [str(build.ffmpeg), "-hide_banner", "-nostdin", "-y", "-loglevel", "error",
           "-ss", f"{t:.3f}", "-i", str(video), "-map", "0:V:0", "-vf", ",".join(vf),
           "-frames:v", "1", "-update", "1", str(out_png)]
    return cmd, tmp


def render_preview(video: Path, t: float, out_png: Path, ass: Path | None = None,
                   fonts_dir: Path | None = None) -> Path:
    cmd, tmp = preview_command(video, t, out_png, ass, fonts_dir)
    out_png.unlink(missing_ok=True)
    try:
        r = run(cmd, timeout=120)
    except subprocess.TimeoutExpired:
        raise JobError("預覽逾時")
    finally:
        if tmp:
            tmp.unlink(missing_ok=True)
    if r.returncode != 0 or not out_png.is_file():
        raise JobError(error_summary(r.stderr.splitlines()) if r.stderr.strip()
                       else "截圖失敗（時間點可能超過影片長度）")
    return out_png


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def main(argv: list[str] | None = None) -> int:
    import argparse
    import sys

    from .encoders import get_report

    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, "reconfigure"):
            stream.reconfigure(encoding="utf-8")

    p = argparse.ArgumentParser(prog="python -m fftoolbox.job")
    p.add_argument("task", choices=[t.value for t in Task] + ["preview"])
    p.add_argument("input", type=Path)
    p.add_argument("-o", "--output", type=Path)
    p.add_argument("--ass", type=Path)
    p.add_argument("--fontsdir", type=Path)
    p.add_argument("--vmode", default=AUTO, choices=list(VIDEO_MODES), help="一般轉檔的視訊處理")
    p.add_argument("--amode", default=AUTO, choices=list(AUDIO_MODES), help="音訊處理")
    p.add_argument("--codec", default="h264", choices=["h264", "hevc"])
    p.add_argument("--encoder", default="auto")
    p.add_argument("--quality", type=int, default=VideoSettings.quality)
    p.add_argument("--speed", type=int, default=VideoSettings.speed)
    p.add_argument("--tenbit", action="store_true")
    p.add_argument("--maxrate", default="")
    p.add_argument("--gop", type=int, default=0)
    p.add_argument("--extra", default="")
    p.add_argument("--abitrate", default="256k")
    p.add_argument("--container", default="mp4", choices=CONTAINERS)
    p.add_argument("-t", "--time", default="0", help="preview 的時間點")
    p.add_argument("--dry-run", action="store_true", help="只印出指令")
    a = p.parse_args(argv)

    if a.task == "preview":
        out = a.output or a.input.with_suffix(".preview.png")
        try:
            render_preview(a.input, parse_time(a.time), out, a.ass, a.fontsdir)
        except (JobError, ValueError) as e:
            print(e, file=sys.stderr)
            return 1
        print(out)
        return 0

    task = Task(a.task)
    video = VideoSettings(mode=a.vmode, codec=a.codec, encoder=a.encoder, quality=a.quality, speed=a.speed,
                          tenbit=a.tenbit, maxrate=a.maxrate, gop=a.gop, extra=a.extra)
    job = Job(task, a.input, a.output or unique_path(default_output(a.input, task, a.container)),
              a.ass, a.fontsdir, video, AudioSettings(mode=a.amode, bitrate=a.abitrate))
    report = get_report() if task != Task.AUDIO else None

    if a.dry_run:
        try:
            cmd = build_command(job, report)
        except JobError as e:
            print(e, file=sys.stderr)
            return 1
        for n in cmd.notes:
            print("・", n, file=sys.stderr)
        for w in cmd.warnings:
            print("⚠", w, file=sys.stderr)
        print(cmd.text)
        cmd.cleanup()
        return 0

    def show(pr: Progress) -> None:
        pct = f"{pr.fraction * 100:5.1f}%" if pr.fraction is not None else "  ?  "
        eta = f"剩 {pr.eta:5.0f}s" if pr.eta is not None else ""
        spd = f"{pr.speed:.2f}x" if pr.speed else ""
        print(f"\r{pct} {spd:>7} {eta}   ", end="", file=sys.stderr, flush=True)

    result = run_job(job, report, on_progress=show,
                     on_log=lambda ln: print(f"\n[ffmpeg] {ln}", file=sys.stderr),
                     on_command=lambda c: print(c.text, file=sys.stderr))
    print(file=sys.stderr)
    for w in result.warnings:
        print("⚠", w, file=sys.stderr)
    if not result.ok:
        print("失敗：", result.error, file=sys.stderr)
        return 1
    print(f"完成（{result.elapsed:.1f}s）：{job.output}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
