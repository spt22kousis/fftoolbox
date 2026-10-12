import io
import struct
from pathlib import Path

from fftoolbox import ass_fonts as af

DATA = Path(__file__).parent / "data"


def test_used_fonts_from_sample():
    text, enc = af.read_ass(DATA / "karaoke.ass")
    info = af.parse_ass(text)
    names = {u.name for u in info.fonts.values()}
    # 整段被 \fn 覆蓋的樣式字型、Comment 行、沒用到的樣式都不算
    assert names == {"Arial", "微軟正黑體", "MS Gothic", "Yu Gothic UI", "Missing Font ABC"}
    assert info.unknown_styles == {"NoSuchStyle"}
    assert info.fonts["arial"].lines == 3
    assert info.styles["Kanji"] == "MS Gothic"


def test_drawing_does_not_use_font():
    text = """[V4+ Styles]
Format: Name, Fontname, Fontsize
Style: Default,DrawFont,20
Style: Text,TextFont,20
[Events]
Format: Layer, Start, End, Style, Name, MarginL, MarginR, MarginV, Effect, Text
Dialogue: 0,0:00:00.00,0:00:01.00,Default,,0,0,0,,{\\p1}m 0 0 l 1 1{\\p0}
Dialogue: 0,0:00:00.00,0:00:01.00,Text,,0,0,0,,{\\pos(1,2)\\pbo0}hi, with comma
"""
    info = af.parse_ass(text)
    assert set(info.fonts) == {"textfont"}


def test_encoding_detection():
    assert af.detect_encoding("中文".encode("utf-8")) == "utf-8"
    assert af.detect_encoding(b"\xef\xbb\xbfabc") == "utf-8-sig"
    assert af.detect_encoding("[Script Info] 中文字幕".encode("utf-16")) == "utf-16"
    assert af.detect_encoding("繁體中文字幕測試".encode("cp950")) == "cp950"
    assert af.is_utf8("utf-8-sig") and not af.is_utf8("cp950")


def _make_font(names: dict[int, str]) -> bytes:
    """產生只有 name 表的最小 sfnt。"""
    strings = b""
    records = b""
    for nid, s in names.items():
        raw = s.encode("utf-16-be")
        records += struct.pack(">HHHHHH", 3, 1, 0x409, nid, len(raw), len(strings))
        strings += raw
    name_table = struct.pack(">HHH", 0, len(names), 6 + len(records)) + records + strings
    header = struct.pack(">4sHHHH", b"\x00\x01\x00\x00", 1, 16, 0, 0)
    entry = struct.pack(">4sIII", b"name", 0, 12 + 16, len(name_table))
    return header + entry + name_table


def test_font_names_and_uue_roundtrip():
    font = _make_font({1: "測試字型", 4: "測試字型 Bold", 6: "TestFont-Bold", 2: "Bold"})
    assert af.font_names(io.BytesIO(font)) == {"測試字型", "測試字型 Bold", "TestFont-Bold"}

    # 依 ASS 規格 uuencode，再用 parser 解回來
    enc = ""
    for i in range(0, len(font), 3):
        chunk = font[i:i + 3]
        v = int.from_bytes(chunk.ljust(3, b"\0"), "big")
        chars = [(v >> s) & 63 for s in (18, 12, 6, 0)][: len(chunk) + 1]
        enc += "".join(chr(c + 33) for c in chars)
    lines = [enc[i:i + 80] for i in range(0, len(enc), 80)]
    ass = "[Fonts]\nfontname: test_0.ttf\n" + "\n".join(lines) + "\n"
    info = af.parse_ass(ass)
    assert info.embedded[0][0] == "test_0.ttf"
    assert info.embedded[0][1] == font


def test_check_with_fontsdir(tmp_path):
    (tmp_path / "x.ttf").write_bytes(_make_font({1: "Missing Font ABC"}))
    result = af.check_fonts(DATA / "karaoke.ass", tmp_path)
    found = {f.usage.name: f for f in result.fonts}
    assert found["Missing Font ABC"].found
    assert found["Missing Font ABC"].locations[0].source == af.FONTSDIR
    assert found["Arial"].found
