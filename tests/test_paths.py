from pathlib import Path

import pytest

from fftoolbox import paths


def _fake_ffmpeg_dir(d: Path, ffprobe: bool = True) -> Path:
    d.mkdir(parents=True, exist_ok=True)
    (d / "ffmpeg.exe").write_bytes(b"")
    if ffprobe:
        (d / "ffprobe.exe").write_bytes(b"")
    return d


@pytest.fixture
def env(monkeypatch, tmp_path):
    """乾淨的環境：沒有環境變數、程式資料夾在 tmp_path/app、PATH 上什麼都沒有。"""
    monkeypatch.delenv("FFTOOLBOX_FFMPEG_DIR", raising=False)
    monkeypatch.delenv("FFTOOLBOX_FFMPEG_COMPAT_DIR", raising=False)
    monkeypatch.setattr(paths, "app_dir", lambda: tmp_path / "app")
    monkeypatch.setattr(paths.shutil, "which", lambda name: None)
    return monkeypatch, tmp_path


def test_env_var_wins(env):
    mp, tmp = env
    _fake_ffmpeg_dir(tmp / "app" / "bin" / "ffmpeg")
    mp.setenv("FFTOOLBOX_FFMPEG_DIR", str(tmp / "custom"))
    b = paths.main_build()
    assert b.dir == tmp / "custom" and b.source == paths.ENV


def test_bundled_before_path(env):
    mp, tmp = env
    bundled = _fake_ffmpeg_dir(tmp / "app" / "bin" / "ffmpeg")
    on_path = _fake_ffmpeg_dir(tmp / "sys")
    mp.setattr(paths.shutil, "which", lambda name: str(on_path / "ffmpeg.exe"))
    b = paths.main_build()
    assert b.dir == bundled and b.source == paths.BUNDLED


def test_falls_back_to_path(env):
    mp, tmp = env
    on_path = _fake_ffmpeg_dir(tmp / "sys")
    mp.setattr(paths.shutil, "which", lambda name: str(on_path / "ffmpeg.exe"))
    b = paths.main_build()
    assert b.dir == on_path and b.source == paths.SYSTEM_PATH and b.exists()


def test_path_without_ffprobe_is_ignored(env):
    mp, tmp = env
    on_path = _fake_ffmpeg_dir(tmp / "sys", ffprobe=False)
    mp.setattr(paths.shutil, "which", lambda name: str(on_path / "ffmpeg.exe"))
    b = paths.main_build()
    assert b.source == paths.BUNDLED and not b.exists()


def test_nothing_found_points_to_bundled(env):
    _, tmp = env
    b = paths.main_build()
    assert b.dir == tmp / "app" / "bin" / "ffmpeg" and not b.exists()


def test_compat_never_uses_path(env):
    mp, tmp = env
    on_path = _fake_ffmpeg_dir(tmp / "sys")
    mp.setattr(paths.shutil, "which", lambda name: str(on_path / "ffmpeg.exe"))
    b = paths.compat_build()
    assert b.source == paths.BUNDLED and not b.exists()
