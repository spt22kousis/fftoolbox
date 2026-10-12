"""code review 修正項目的測試。"""
import threading
from pathlib import Path

import pytest

from fftoolbox import ass_fonts as af
from fftoolbox import config
from fftoolbox import job as jobmod
from fftoolbox.encoders import VideoSettings
from fftoolbox.job import AudioSettings, Task
from fftoolbox.presets import Preset


@pytest.mark.parametrize("name, expected", [
    ("a.mkv", "a_aac.mkv"),
    ("a.MP4", "a_aac.mp4"),
    ("a.ts", "a_aac.ts"),
    ("a.webm", "a_aac.mkv"),
    ("a.ogv", "a_aac.mkv"),
    ("a.avi", "a_aac.mkv"),
    ("a.flac", "a_aac.m4a"),
    ("a.wav", "a_aac.m4a"),
])
def test_audio_task_output_container_can_hold_aac(name, expected):
    assert jobmod.default_output(Path(name), Task.AUDIO).name == expected


def test_settings_from_dict_coerces_types():
    v = VideoSettings.from_dict({"speed": "5", "quality": None, "tenbit": "true", "gop": "x",
                                 "codec": "hevc", "unknown": 1})
    assert v.speed == 5 and v.quality == VideoSettings.quality and v.tenbit is True
    assert v.gop == 0 and v.codec == "hevc"
    a = AudioSettings.from_dict({"sample_rate": "48000", "bitrate": 192})
    assert a.sample_rate == 48000 and a.bitrate == "192"
    p = Preset.from_dict({"video": "壞掉", "audio": None, "container": ["x"]})
    assert p.video == VideoSettings() and p.audio == AudioSettings() and p.container == "mp4"


def _uue(data: bytes) -> str:
    out = ""
    for i in range(0, len(data), 3):
        chunk = data[i:i + 3]
        v = int.from_bytes(chunk.ljust(3, b"\0"), "big")
        out += "".join(chr(((v >> s) & 63) + 33) for s in (18, 12, 6, 0)[: len(chunk) + 1])
    return out


def test_fonts_section_lines_starting_with_semicolon_or_bracket():
    # 讓編碼後的資料行以 ';'（59）和 '['（91）開頭、']'（93）結尾
    payload = bytes(range(256)) * 3
    enc = _uue(payload)
    lines = [enc[i:i + 80] for i in range(0, len(enc), 80)]
    lines.insert(1, ";" + lines[1][1:])  # 人工製造：資料行開頭是 ';'
    lines.insert(2, "[" + "A" * 10 + "]")  # 人工製造：長得像區段標題的資料行
    ass = "[Fonts]\nfontname: x_0.ttf\n" + "\n".join(lines) + "\n[Events]\nFormat: Text\nDialogue: hi\n"
    info = af.parse_ass(ass)
    assert len(info.embedded) == 1
    # 兩行人工資料都要被收進字型資料（沒被當成註解或區段），而且之後的 [Events] 仍正常解析
    assert info.embedded[0][1] == af._decode_uue(lines)
    assert info.dialogue_count == 1


def test_reset_to_unknown_style_falls_back_to_line_style():
    text = """[V4+ Styles]
Format: Name, Fontname
Style: Default,DefaultFont
Style: Kanji,KanjiFont
[Events]
Format: Layer, Start, End, Style, Name, MarginL, MarginR, MarginV, Effect, Text
Dialogue: 0,0:00:00.00,0:00:01.00,Kanji,,0,0,0,,{\\fnOther}a{\\rTypo}b
"""
    info = af.parse_ass(text)
    assert set(info.fonts) == {"other", "kanjifont"}
    assert not info.unknown_styles


def test_fontsdir_is_not_recursive(tmp_path):
    (tmp_path / "a.ttf").write_bytes(b"")
    sub = tmp_path / "sub"
    sub.mkdir()
    (sub / "b.ttf").write_bytes(b"")
    assert [p.name for p in af.dir_font_files(tmp_path)] == ["a.ttf"]


def test_config_update_is_thread_safe():
    config.save({})
    def worker(i):
        for j in range(20):
            config.update(**{f"k{i}_{j}": j})
    threads = [threading.Thread(target=worker, args=(i,)) for i in range(4)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    data = config.load()
    assert all(f"k{i}_{j}" in data for i in range(4) for j in range(20))
    assert not list(config.config_path().parent.glob("*.tmp"))


def test_queue_worker_skip(monkeypatch):
    from PySide6.QtCore import QCoreApplication

    from fftoolbox.gui import workers

    app = QCoreApplication.instance() or QCoreApplication([])  # noqa: F841
    ran = []
    monkeypatch.setattr(workers, "run_job", lambda job, report, **kw: ran.append(job) or jobmod.JobResult(True))
    w = workers.QueueWorker([(1, "a"), (2, "b"), (3, "c")], None)
    w.skip(2)
    w.run()  # 直接在這個執行緒跑
    assert ran == ["a", "c"]
