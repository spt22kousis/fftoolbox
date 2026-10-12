from pathlib import Path

import pytest

from fftoolbox import encoders as enc
from fftoolbox import job as jobmod
from fftoolbox.job import AUTO, COPY, ENCODE, AudioSettings, Job, Task
from fftoolbox.paths import MAIN
from fftoolbox.probe import MediaInfo


def info(vcodec="h264", pix="yuv420p", audio=("flac",)):
    return MediaInfo(Path("x"), duration=10.0,
                     video=[{"codec_name": vcodec, "pix_fmt": pix, "profile": "High 10"}],
                     audio=[{"codec_name": a} for a in audio])


def plan(task, vmode=AUTO, amode=AUTO, src=None, **audio):
    return jobmod.plan_streams(task, enc.VideoSettings(mode=vmode), AudioSettings(mode=amode, **audio),
                               src or info())


def test_h264_flac_copies_video_encodes_audio():
    p = plan(Task.TRANSCODE, src=info("h264", audio=("flac",)))
    assert p.video_copy and p.audio_copy == [False] and not p.warnings


def test_av1_aac_encodes_video_copies_audio():
    p = plan(Task.TRANSCODE, src=info("av1", audio=("aac",)))
    assert not p.video_copy and p.audio_copy == [True]


def test_hevc_opus():
    p = plan(Task.TRANSCODE, src=info("hevc", "yuv420p10le", audio=("opus",)))
    assert p.video_copy and p.audio_copy == [False] and not p.warnings


def test_hi10p_copies_with_warning():
    p = plan(Task.TRANSCODE, src=info("h264", "yuv420p10le"))
    assert p.video_copy and p.warnings and "重新編碼" in p.warnings[0]
    p = plan(Task.TRANSCODE, src=info("hevc", "yuv422p10le"))
    assert p.video_copy and p.warnings


def test_manual_modes():
    assert not plan(Task.TRANSCODE, vmode=ENCODE, src=info("h264")).video_copy
    assert plan(Task.TRANSCODE, vmode=COPY, src=info("av1")).video_copy
    assert plan(Task.TRANSCODE, amode=COPY, src=info(audio=("flac",))).audio_copy == [True]
    assert plan(Task.TRANSCODE, amode=ENCODE, src=info(audio=("aac",))).audio_copy == [False]


def test_burn_always_encodes_video_but_audio_follows_rule():
    p = plan(Task.BURN, vmode=COPY, src=info("h264", audio=("aac", "flac")))
    assert not p.video_copy and p.warnings
    assert p.audio_copy == [True, False]


def test_audio_task_always_encodes_audio():
    p = plan(Task.AUDIO, amode=COPY, src=info(audio=("aac",)))
    assert p.video_copy and p.audio_copy == [False]


def test_forced_samplerate_disables_aac_copy():
    p = plan(Task.TRANSCODE, src=info(audio=("aac",)), sample_rate=48000)
    assert p.audio_copy == [False]
    p = plan(Task.TRANSCODE, amode=COPY, src=info(audio=("aac",)), channels=2)
    assert p.audio_copy == [True] and p.warnings


def test_mixed_audio_args():
    p = plan(Task.TRANSCODE, src=info(audio=("aac", "flac")), )
    args = jobmod.audio_stream_args(AudioSettings(bitrate="192k"), p)
    assert args == ["-c:a:0", "copy", "-c:a:1", "aac", "-b:a:1", "192k"]
    p = plan(Task.TRANSCODE, src=info(audio=("flac", "opus")), channels=2)
    args = jobmod.audio_stream_args(AudioSettings(channels=2), p)
    assert args == ["-c:a", "aac", "-b:a", "256k", "-ac", "2"]


def test_build_copy_command_needs_no_encoder(tmp_path):
    src = tmp_path / "in.mkv"
    src.write_bytes(b"x")
    j = Job(Task.TRANSCODE, src, tmp_path / "o.mp4")
    cmd = jobmod.build_command(j, None, info("hevc", audio=("aac",)))
    a = cmd.args
    assert a[a.index("-c:v") + 1] == "copy" and a[a.index("-tag:v") + 1] == "hvc1"
    assert a[a.index("-c:a") + 1] == "copy"
    assert cmd.encoder is None and cmd.notes


def test_build_encode_needs_report(tmp_path):
    src = tmp_path / "in.mkv"
    src.write_bytes(b"x")
    j = Job(Task.TRANSCODE, src, tmp_path / "o.mp4")
    with pytest.raises(jobmod.JobError):
        jobmod.build_command(j, None, info("av1"))
    report = enc.DetectionReport("", "", None, {}, {
        "libx264": enc.EncoderStatus("libx264", available=True, build=MAIN)})
    cmd = jobmod.build_command(j, report, info("av1"))
    assert cmd.encoder.name == "libx264"
