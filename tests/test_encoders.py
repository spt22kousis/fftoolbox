from pathlib import Path

import pytest

from fftoolbox import encoders as enc
from fftoolbox.paths import COMPAT, MAIN, FFmpegBuild

NVENC_OLD_DRIVER = """\
[h264_nvenc @ 000001d2c3e4f5a0] Driver does not support the required nvenc API version. Required: 13.0 Found: 12.1
[h264_nvenc @ 000001d2c3e4f5a0] The minimum required Nvidia driver for nvenc is 570.0 or newer
[vost#0:0/h264_nvenc @ 000001d2c3e4f600] [enc:h264_nvenc @ 000001d2c3e4f700] Error while opening encoder - maybe incorrect parameters such as bit_rate, rate, width or height.
[vf#0:0 @ 000001d2c3e4f800] Error sending frames to consumers: Function not implemented
Conversion failed!
"""


def test_quality_and_speed_mapping():
    s = enc.VideoSettings(quality=23, speed=5)
    cases = {
        "h264_nvenc": ["-rc", "vbr", "-cq", "23", "-b:v", "0", "-preset", "p5"],
        "hevc_qsv": ["-global_quality", "23", "-preset", "slow"],
        "h264_amf": ["-rc", "cqp", "-qp_i", "23", "-qp_p", "23", "-quality", "balanced"],
        "libx265": ["-crf", "23", "-preset", "slow"],
    }
    for name, expected in cases.items():
        args, warns = enc.video_args(enc.ENCODER_MAP[name], s)
        assert args[:4] == ["-c:v", name, "-pix_fmt", enc.ENCODER_MAP[name].pix_fmt_8]
        assert args[4:] == expected
        assert warns == []


def test_speed_extremes():
    assert enc.speed_value(enc.ENCODER_MAP["hevc_nvenc"], 1) == "p1"
    assert enc.speed_value(enc.ENCODER_MAP["hevc_nvenc"], 99) == "p7"
    assert enc.speed_value(enc.ENCODER_MAP["libx264"], 1) == "veryfast"
    assert enc.speed_value(enc.ENCODER_MAP["libx264"], 7) == "veryslow"
    assert enc.speed_value(enc.ENCODER_MAP["hevc_amf"], 1) == "speed"
    assert enc.speed_value(enc.ENCODER_MAP["hevc_amf"], 7) == "quality"


def test_tenbit():
    s = enc.VideoSettings(tenbit=True)
    args, warns = enc.video_args(enc.ENCODER_MAP["libx265"], s)
    assert args[args.index("-pix_fmt") + 1] == "yuv420p10le"
    args, warns = enc.video_args(enc.ENCODER_MAP["hevc_nvenc"], s)
    assert args[args.index("-pix_fmt") + 1] == "p010le"
    args, warns = enc.video_args(enc.ENCODER_MAP["h264_qsv"], s)
    assert args[args.index("-pix_fmt") + 1] == "nv12"
    assert warns


def test_maxrate_gop_extra():
    s = enc.VideoSettings(maxrate="20M", gop=240, extra='-tune animation -x264-params "aq-mode=3"')
    args, warns = enc.video_args(enc.ENCODER_MAP["libx264"], s)
    assert args[-10:] == ["-maxrate", "20M", "-bufsize", "40M", "-g", "240",
                          "-tune", "animation", "-x264-params", "aq-mode=3"]
    assert warns == []

    args, warns = enc.video_args(enc.ENCODER_MAP["hevc_qsv"], s)
    assert "-maxrate" not in args and warns

    args, warns = enc.video_args(enc.ENCODER_MAP["libx264"], enc.VideoSettings(maxrate="abc"))
    assert "-maxrate" not in args and warns


def test_bitrate_parsing():
    assert enc.parse_bitrate("20M") == 20_000_000
    assert enc.parse_bitrate("8500k") == 8_500_000
    assert enc.parse_bitrate("1.5M") == 1_500_000
    assert enc.parse_bitrate("x") is None
    assert enc.format_bitrate(8_500_000) == "8500k"


def test_summarize_error_picks_encoder_lines():
    msg = enc.summarize_error(NVENC_OLD_DRIVER, "h264_nvenc")
    assert "Driver does not support" in msg
    assert "Conversion failed" not in msg
    assert "@ 0000" not in msg
    assert enc.is_api_version_error(NVENC_OLD_DRIVER)
    assert enc.required_driver(NVENC_OLD_DRIVER) == "570.0"


@pytest.fixture
def fake_builds(tmp_path):
    return [FFmpegBuild(MAIN, tmp_path / "main"), FFmpegBuild(COMPAT, tmp_path / "compat")]


def _all_compiled(builds):
    return {b.name: {e.name for e in enc.ENCODERS} for b in builds}


def test_old_driver_falls_back_to_compat(monkeypatch, fake_builds):
    def fake_test(build, spec, tenbit=False):
        if build.name == MAIN:
            return False, NVENC_OLD_DRIVER
        return True, ""

    monkeypatch.setattr(enc, "test_encoder", fake_test)
    st = enc._detect_one(enc.ENCODER_MAP["hevc_nvenc"], fake_builds, _all_compiled(fake_builds), "551.23")
    assert st.available and st.build == COMPAT
    assert st.required_driver == "570.0"
    assert "551.23" in st.note and "570.0" in st.note
    assert st.tenbit is True


def test_old_driver_both_fail(monkeypatch, fake_builds):
    monkeypatch.setattr(enc, "test_encoder", lambda b, s, tenbit=False: (False, NVENC_OLD_DRIVER))
    st = enc._detect_one(enc.ENCODER_MAP["h264_nvenc"], fake_builds, _all_compiled(fake_builds), "470.10")
    assert not st.available
    assert "新版與相容版都失敗" in st.reason and "570.0" in st.reason


def test_old_driver_without_compat(monkeypatch, fake_builds):
    monkeypatch.setattr(enc, "test_encoder", lambda b, s, tenbit=False: (False, NVENC_OLD_DRIVER))
    st = enc._detect_one(enc.ENCODER_MAP["h264_nvenc"], fake_builds[:1], _all_compiled(fake_builds), None)
    assert not st.available and "未附相容版" in st.reason


def test_non_api_error_does_not_try_compat(monkeypatch, fake_builds):
    calls = []

    def fake_test(build, spec, tenbit=False):
        calls.append(build.name)
        return False, "[h264_nvenc @ 0000] Cannot load nvcuda.dll\nConversion failed!"

    monkeypatch.setattr(enc, "test_encoder", fake_test)
    st = enc._detect_one(enc.ENCODER_MAP["h264_nvenc"], fake_builds, _all_compiled(fake_builds), None)
    assert calls == [MAIN]
    assert st.reason == "[h264_nvenc] Cannot load nvcuda.dll"


def test_not_compiled(fake_builds):
    compiled = {MAIN: set(), COMPAT: set()}
    st = enc._detect_one(enc.ENCODER_MAP["hevc_amf"], fake_builds, compiled, None)
    assert not st.available and "未編譯" in st.reason


def _report(**avail):
    statuses = {}
    for name, (ok, tenbit) in avail.items():
        statuses[name] = enc.EncoderStatus(name, available=ok, build=MAIN, tenbit=tenbit,
                                           reason="" if ok else "boom")
    return enc.DetectionReport("", "", None, {}, statuses)


def test_auto_pick_priority():
    r = _report(h264_nvenc=(True, None), h264_qsv=(True, None), libx264=(True, None))
    assert r.pick(enc.H264).name == "h264_nvenc"
    r = _report(h264_nvenc=(False, None), h264_qsv=(True, None), libx264=(True, None))
    assert r.pick(enc.H264).name == "h264_qsv"
    r = _report(hevc_nvenc=(True, False), hevc_amf=(True, True), libx265=(True, True))
    assert r.pick(enc.HEVC, tenbit=True).name == "hevc_amf"
    assert r.pick(enc.HEVC).name == "hevc_nvenc"


def test_resolve_manual_unavailable():
    r = _report(hevc_nvenc=(False, None), libx265=(True, True))
    spec, err = r.resolve(enc.VideoSettings(codec=enc.HEVC, encoder="hevc_nvenc"))
    assert spec is None and "boom" in err
    spec, err = r.resolve(enc.VideoSettings(codec=enc.HEVC, encoder="auto"))
    assert spec.name == "libx265"


def test_report_roundtrip():
    r = _report(libx264=(True, None), hevc_qsv=(False, None))
    r2 = enc.DetectionReport.from_dict(r.to_dict())
    assert r2.to_dict() == r.to_dict()
