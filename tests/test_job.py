import subprocess
import threading
from pathlib import Path

import pytest

from fftoolbox import encoders as enc
from fftoolbox import job as jobmod
from fftoolbox.job import AudioSettings, Job, Task
from fftoolbox.paths import MAIN, main_build

DATA = Path(__file__).parent / "data"
HAS_FFMPEG = main_build().exists()
needs_ffmpeg = pytest.mark.skipif(not HAS_FFMPEG, reason="沒有 ffmpeg（設定 FFTOOLBOX_FFMPEG_DIR 或放進 bin/ffmpeg）")


def test_escape_filter_value():
    assert jobmod.escape_filter_value(r"C:\a b\x.ass") == r"C\\:/a b/x.ass"
    assert jobmod.escape_filter_value("a'b") == r"a\\\'b"
    assert jobmod.escape_filter_value("[a],b;c") == r"\[a\]\,b\;c"


def test_subtitles_filter():
    f = jobmod.subtitles_filter(Path("x.ass"), Path("fonts"))
    assert f == "subtitles=filename=x.ass:fontsdir=fonts"


def test_non_utf8_ass_gets_utf8_temp(tmp_path):
    src = tmp_path / "in.mkv"
    src.write_bytes(b"x")
    ass = tmp_path / "big5.ass"
    ass.write_bytes((DATA / "karaoke.ass").read_text(encoding="utf-8").encode("cp950", "replace"))
    j = Job(Task.BURN, src, tmp_path / "o.mp4", ass)
    cmd = jobmod.build_command(j, _report(libx264=1))
    assert len(cmd.temp_files) == 1 and cmd.warnings
    tmp = cmd.temp_files[0]
    assert "微軟正黑體" in tmp.read_text(encoding="utf-8")
    cmd.cleanup()
    assert not tmp.exists()


def test_parse_time():
    assert jobmod.parse_time("83.5") == 83.5
    assert jobmod.parse_time("1:23.5") == 83.5
    assert jobmod.parse_time("1:00:00") == 3600
    with pytest.raises(ValueError):
        jobmod.parse_time("abc")


def test_default_output_and_unique(tmp_path):
    src = tmp_path / "a.mkv"
    assert jobmod.default_output(src, Task.BURN, "mp4") == tmp_path / "a_sub.mp4"
    assert jobmod.default_output(src, Task.AUDIO, "mp4") == tmp_path / "a_aac.mkv"
    (tmp_path / "a_sub.mp4").write_bytes(b"")
    p = jobmod.unique_path(tmp_path / "a_sub.mp4")
    assert p.name == "a_sub (2).mp4"
    assert jobmod.unique_path(tmp_path / "a_sub.mp4", {p}).name == "a_sub (3).mp4"


def test_progress_parser():
    pp = jobmod.ProgressParser(duration=10.0)
    for line in ["frame=10", "fps=24.0", "out_time_us=5000000", "speed=2.0x"]:
        assert pp.feed(line) is None
    p = pp.feed("progress=continue")
    assert p.fraction == pytest.approx(0.5) and p.speed == 2.0 and p.fps == 24.0
    assert p.eta is not None
    pp.feed("out_time_us=N/A")
    p = pp.feed("progress=end")
    assert p.done and p.fraction == 1.0 and p.eta == 0


def _report(**names):
    st = {n: enc.EncoderStatus(n, available=True, build=MAIN, tenbit=True) for n in names}
    return enc.DetectionReport("", "", None, {}, st)


def test_build_burn_command(tmp_path):
    src = tmp_path / "in.mkv"
    src.write_bytes(b"x")
    j = Job(Task.BURN, src, tmp_path / "out.mp4", DATA / "karaoke.ass", tmp_path,
            enc.VideoSettings(codec="hevc", tenbit=True), AudioSettings(bitrate="192k", sample_rate=48000, channels=2))
    cmd = jobmod.build_command(j, _report(hevc_qsv=1, libx265=1))
    a = cmd.args
    assert cmd.encoder.name == "hevc_qsv"
    assert a[a.index("-vf") + 1].startswith("subtitles=filename=")
    assert a[a.index("-c:v") + 1] == "hevc_qsv" and a[a.index("-pix_fmt") + 1] == "p010le"
    assert a[-11:] == ["-c:a", "aac", "-b:a", "192k", "-ar", "48000", "-ac", "2",
                       "-movflags", "+faststart", str(tmp_path / "out.mp4")]


def test_build_audio_command_keeps_subs_for_mkv(tmp_path):
    src = tmp_path / "in.mkv"
    src.write_bytes(b"x")
    a = jobmod.build_command(Job(Task.AUDIO, src, tmp_path / "o.mkv"), None).args
    assert "0:s?" in a and "0:t?" in a and a[a.index("-c:v") + 1] == "copy"
    assert a[a.index("-c:a") + 1] == "aac"
    a = jobmod.build_command(Job(Task.AUDIO, src, tmp_path / "o.mp4"), None).args
    assert "0:s?" not in a and "+faststart" in a


def test_build_errors(tmp_path):
    src = tmp_path / "in.mkv"
    src.write_bytes(b"x")
    with pytest.raises(jobmod.JobError, match="字幕"):
        jobmod.build_command(Job(Task.BURN, src, tmp_path / "o.mp4"), _report(libx264=1))
    with pytest.raises(jobmod.JobError, match="相同"):
        jobmod.build_command(Job(Task.TRANSCODE, src, src), _report(libx264=1))
    with pytest.raises(jobmod.JobError, match="沒有可用"):
        jobmod.build_command(Job(Task.TRANSCODE, src, tmp_path / "o.mp4"), _report())


# ---------------------------------------------------------------------------
# 實際執行 ffmpeg 的整合測試
# ---------------------------------------------------------------------------

@pytest.fixture(scope="module")
def sample(tmp_path_factory):
    d = tmp_path_factory.mktemp("測試 [a],b'c;d")
    src = d / "來源.mkv"
    subprocess.run([str(main_build().ffmpeg), "-hide_banner", "-loglevel", "error", "-y",
                    "-f", "lavfi", "-i", "testsrc2=s=640x360:r=24:d=3",
                    "-f", "lavfi", "-i", "sine=d=3:sample_rate=48000",
                    "-c:v", "libx264", "-preset", "ultrafast", "-c:a", "flac", str(src)], check=True)
    ass = d / "字幕's.ass"
    ass.write_bytes((DATA / "karaoke.ass").read_bytes())
    return d, src, ass


def _sw_report():
    return _report(libx264=1, libx265=1)


@needs_ffmpeg
@pytest.mark.parametrize("task", list(Task))
def test_run_tasks(sample, task):
    d, src, ass = sample
    out = jobmod.default_output(src, task, "mp4")
    j = Job(task, src, out, ass if task == Task.BURN else None, d,
            enc.VideoSettings(speed=1))
    progress = []
    r = jobmod.run_job(j, _sw_report(), on_progress=progress.append)
    assert r.ok, r.error
    assert out.is_file() and progress and progress[-1].done


@needs_ffmpeg
def test_cancel_removes_partial(sample, tmp_path):
    d, src, _ = sample
    big = tmp_path / "long.mkv"
    subprocess.run([str(main_build().ffmpeg), "-hide_banner", "-loglevel", "error", "-y",
                    "-f", "lavfi", "-i", "testsrc2=s=1280x720:r=24:d=120",
                    "-c:v", "libx264", "-preset", "ultrafast", str(big)], check=True)
    out = tmp_path / "out.mp4"
    j = Job(Task.TRANSCODE, big, out, video=enc.VideoSettings(codec="hevc", speed=7))
    holder = {}
    started = threading.Event()

    def on_progress(p):
        started.set()

    def canceller():
        started.wait(30)
        holder["runner"].cancel()

    t = threading.Thread(target=canceller)
    t.start()
    r = jobmod.run_job(j, _sw_report(), on_progress=on_progress,
                       on_runner=lambda rn: holder.__setitem__("runner", rn))
    t.join()
    assert r.cancelled and not r.ok and not out.exists()


@needs_ffmpeg
def test_preview(sample, tmp_path):
    _, src, ass = sample
    png = jobmod.render_preview(src, 2.5, tmp_path / "p.png", ass)
    assert png.stat().st_size > 1000
