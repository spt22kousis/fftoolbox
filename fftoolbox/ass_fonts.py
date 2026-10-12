"""ASS 字型檢查。

1. 讀 ASS（自動判斷編碼：UTF-8 / UTF-16 / Big5 / GBK / Shift-JIS…）
2. 找出實際用到的字型：每個 Dialogue 依樣式字型開始，逐段套用行內 \\fn、\\r，
   只有真的有文字（不是 \\p 繪圖）的段落才算用到。Comment 行（karaoke template）不算。
3. 建立字型索引：系統字型（含使用者安裝的字型）+ fontsdir + ASS 內嵌 [Fonts]，
   讀字型檔的 name 表（家族名、全名、PostScript 名，含中文等在地化名稱）。
4. 比對，列出缺少的字型。

CLI：python -m fftoolbox.ass_fonts 字幕.ass [--fontsdir 資料夾]
"""

from __future__ import annotations

import io
import json
import os
import re
import struct
import tempfile
from dataclasses import dataclass, field
from pathlib import Path
from typing import BinaryIO, Iterable

from .config import write_json_atomic
from .paths import config_dir

FONT_EXTS = {".ttf", ".otf", ".ttc", ".otc"}

# ---------------------------------------------------------------------------
# 讀檔與編碼
# ---------------------------------------------------------------------------

_FALLBACK_ENCODINGS = ["utf-8", "cp950", "gb18030", "cp932"]


def detect_encoding(data: bytes) -> str:
    """回傳 Python 編碼名稱。"""
    if data.startswith(b"\xef\xbb\xbf"):
        return "utf-8-sig"
    if data.startswith((b"\xff\xfe", b"\xfe\xff")):
        return "utf-16"
    sample = data[:4096]
    if sample.count(b"\x00") > len(sample) // 4:  # 沒有 BOM 的 UTF-16
        return "utf-16-le" if sample[1:2] == b"\x00" else "utf-16-be"
    for enc in _FALLBACK_ENCODINGS:
        try:
            data.decode(enc)
            return enc
        except UnicodeDecodeError:
            continue
    return "latin-1"


def is_utf8(encoding: str) -> bool:
    return encoding.replace("_", "-").lower() in ("utf-8", "utf-8-sig", "utf8")


def write_utf8_temp(text: str, stem: str) -> Path:
    """把非 UTF-8 的 ASS 存成 UTF-8 暫存檔給 ffmpeg 用。
    （subtitles 濾鏡的 charenc 只轉換對話，不轉換樣式區段，樣式名稱與字型名稱會變亂碼）"""
    tmp_dir = Path(tempfile.gettempdir()) / "fftoolbox"
    tmp_dir.mkdir(parents=True, exist_ok=True)
    fd, name = tempfile.mkstemp(suffix=".ass", prefix=f"{stem[:40]}_", dir=tmp_dir)
    with os.fdopen(fd, "w", encoding="utf-8", newline="") as f:
        f.write(text)
    return Path(name)


def read_ass(path: str | Path) -> tuple[str, str]:
    """回傳 (文字內容, 編碼)。"""
    data = Path(path).read_bytes()
    enc = detect_encoding(data)
    return data.decode(enc, errors="replace"), enc


# ---------------------------------------------------------------------------
# ASS 解析
# ---------------------------------------------------------------------------

INLINE = "（行內 \\fn）"


@dataclass
class FontUsage:
    name: str  # ASS 裡寫的名稱（已去掉直排用的 @）
    sources: set[str] = field(default_factory=set)  # 樣式名稱，或 INLINE
    lines: int = 0  # 用到這個字型的 Dialogue 行數


@dataclass
class AssInfo:
    styles: dict[str, str]  # 樣式名稱 → 字型
    fonts: dict[str, FontUsage]  # 小寫名稱 → 用法
    embedded: list[tuple[str, bytes]]  # [Fonts] 內嵌字型（檔名, 內容）
    unknown_styles: set[str]
    dialogue_count: int


_SECTION_RE = re.compile(r"^\s*\[(.+?)\]\s*$")
_BLOCK_RE = re.compile(r"\{([^}]*)\}")
_TAG_RE = re.compile(r"\\(fn|r|p(?=-?\d))([^\\]*)")


def _split_fields(value: str, fmt: list[str]) -> dict[str, str] | None:
    parts = value.split(",", len(fmt) - 1)
    if len(parts) != len(fmt):
        return None
    return {k: v for k, v in zip(fmt, parts)}


def _clean_font(name: str) -> str:
    return name.strip().lstrip("@").strip()


def _is_uue_data(line: str) -> bool:
    """uuencode 資料只會用到 ASCII 33..96（沒有小寫與空白），區段標題與 fontname: 一定有小寫。"""
    return all(33 <= ord(c) <= 96 for c in line)


def _decode_uue(lines: list[str]) -> bytes:
    """ASS [Fonts] 用的 uuencode 變體：每字元減 33 得 6 bits。"""
    data = "".join(ln.strip() for ln in lines)
    out = bytearray()
    for i in range(0, len(data), 4):
        chunk = [ord(c) - 33 for c in data[i:i + 4]]
        n = len(chunk)
        if n < 2:
            break
        chunk += [0] * (4 - n)
        v = (chunk[0] << 18) | (chunk[1] << 12) | (chunk[2] << 6) | chunk[3]
        out += bytes([(v >> 16) & 0xFF, (v >> 8) & 0xFF, v & 0xFF])[: n - 1]
    return bytes(out)


def parse_ass(text: str) -> AssInfo:
    section = ""
    style_fmt: list[str] = []
    event_fmt: list[str] = []
    styles: dict[str, str] = {}
    dialogues: list[tuple[str, str]] = []  # (樣式, 文字)
    embedded: list[tuple[str, list[str]]] = []

    for raw in text.splitlines():
        line = raw.strip()
        if not line:
            continue
        if section in ("fonts", "graphics") and _is_uue_data(line):
            if section == "fonts" and embedded:
                embedded[-1][1].append(line)
            continue
        if line.startswith(";"):
            continue
        m = _SECTION_RE.match(line)
        if m:
            section = m.group(1).strip().lower()
            continue
        key, sep, value = line.partition(":")
        if not sep:
            continue
        key = key.strip().lower()
        value = value.lstrip()

        if section in ("v4+ styles", "v4 styles", "v4 styles+"):
            if key == "format":
                style_fmt = [f.strip().lower() for f in value.split(",")]
            elif key == "style" and style_fmt:
                d = _split_fields(value, style_fmt)
                if d and "name" in d and "fontname" in d:
                    styles[d["name"].strip()] = _clean_font(d["fontname"])
        elif section == "events":
            if key == "format":
                event_fmt = [f.strip().lower() for f in value.split(",")]
            elif key == "dialogue" and event_fmt:
                d = _split_fields(value, event_fmt)
                if d and "text" in d:
                    dialogues.append((d.get("style", "Default").strip(), d["text"]))
        elif section == "fonts" and key == "fontname":
            embedded.append((value.strip(), []))

    fonts: dict[str, FontUsage] = {}
    unknown: set[str] = set()

    def style_font(name: str) -> tuple[str, str]:
        name = name.lstrip("*")
        if name in styles:
            return styles[name], name
        unknown.add(name)
        if "Default" in styles:
            return styles["Default"], "Default"
        return "Arial", name  # libass 內建 Default 樣式的字型

    for style, text_ in dialogues:
        font, source = style_font(style)
        used_here: dict[str, str] = {}
        drawing = False
        pos = 0
        for m in [*_BLOCK_RE.finditer(text_), None]:
            end = m.start() if m else len(text_)
            segment = text_[pos:end].replace("\\N", "").replace("\\n", "").replace("\\h", " ")
            if segment.strip() and not drawing and font:
                used_here.setdefault(font.lower(), font)
                fonts.setdefault(font.lower(), FontUsage(font)).sources.add(source)
            if m is None:
                break
            for tag, arg in _TAG_RE.findall(m.group(1)):
                arg = arg.strip()
                if tag == "fn":
                    if arg:
                        font, source = _clean_font(arg), INLINE
                    else:
                        font, source = style_font(style)
                elif tag == "r":
                    # libass：\r 指定的樣式不存在時，回到該行本身的樣式
                    name = arg.lstrip("*")
                    font, source = (styles[name], name) if name in styles else style_font(style)
                else:  # \pN
                    drawing = arg.lstrip("-").isdigit() and int(arg) != 0
            pos = m.end()
        for key in used_here:
            fonts[key].lines += 1

    return AssInfo(
        styles=styles,
        fonts=fonts,
        embedded=[(name, _decode_uue(lines)) for name, lines in embedded],
        unknown_styles=unknown,
        dialogue_count=len(dialogues),
    )


# ---------------------------------------------------------------------------
# 字型檔 name 表
# ---------------------------------------------------------------------------

_NAME_IDS = {1, 4, 6, 16, 21}  # 家族、全名、PostScript、typographic 家族、WWS 家族
_MAC_ENCODINGS = {0: "mac_roman", 1: "shift_jis", 2: "big5", 3: "euc_kr", 25: "gb2312"}


def _read(f: BinaryIO, offset: int, size: int) -> bytes:
    f.seek(offset)
    data = f.read(size)
    if len(data) != size:
        raise ValueError("檔案過短")
    return data


def _sfnt_names(f: BinaryIO, offset: int) -> set[str]:
    num_tables = struct.unpack(">H", _read(f, offset + 4, 2))[0]
    directory = _read(f, offset + 12, 16 * num_tables)
    for i in range(num_tables):
        tag, _, table_off, length = struct.unpack(">4sIII", directory[16 * i:16 * i + 16])
        if tag == b"name":
            break
    else:
        return set()
    table = _read(f, table_off, length)
    _, count, string_off = struct.unpack(">HHH", table[:6])
    names: set[str] = set()
    for i in range(count):
        pid, eid, _, nid, slen, soff = struct.unpack(">HHHHHH", table[6 + 12 * i:18 + 12 * i])
        if nid not in _NAME_IDS:
            continue
        raw = table[string_off + soff:string_off + soff + slen]
        if pid in (0, 3):
            enc = "utf-16-be"
        elif pid == 1 and eid in _MAC_ENCODINGS:
            enc = _MAC_ENCODINGS[eid]
        else:
            continue
        name = raw.decode(enc, errors="ignore").replace("\x00", "").strip()
        if name:
            names.add(name)
    return names


def font_names(f: BinaryIO) -> set[str]:
    """讀 TTF / OTF / TTC 的所有名稱。"""
    head = _read(f, 0, 12)
    if head[:4] == b"ttcf":
        num = struct.unpack(">I", head[8:12])[0]
        offsets = struct.unpack(f">{num}I", _read(f, 12, 4 * num))
        names: set[str] = set()
        for off in offsets:
            names |= _sfnt_names(f, off)
        return names
    if head[:4] in (b"\x00\x01\x00\x00", b"OTTO", b"true"):
        return _sfnt_names(f, 0)
    return set()


def font_file_names(path: str | Path) -> set[str]:
    try:
        with open(path, "rb") as f:
            return font_names(f)
    except (OSError, ValueError, struct.error):
        return set()


# ---------------------------------------------------------------------------
# 字型索引
# ---------------------------------------------------------------------------

SYSTEM, USER, FONTSDIR, EMBEDDED = "系統", "使用者安裝", "fontsdir", "ASS 內嵌"


def _windows_font_dirs() -> list[tuple[Path, str]]:
    dirs = [(Path(os.environ.get("WINDIR", r"C:\Windows")) / "Fonts", SYSTEM)]
    local = os.environ.get("LOCALAPPDATA")
    if local:
        dirs.append((Path(local) / "Microsoft" / "Windows" / "Fonts", USER))
    return dirs


def _registry_font_files() -> list[tuple[Path, str]]:
    """登錄檔裡註冊的字型（有些安裝在 Fonts 資料夾以外）。"""
    try:
        import winreg
    except ImportError:
        return []
    fonts_dir = Path(os.environ.get("WINDIR", r"C:\Windows")) / "Fonts"
    out = []
    key_path = r"SOFTWARE\Microsoft\Windows NT\CurrentVersion\Fonts"
    for hive, source in ((winreg.HKEY_LOCAL_MACHINE, SYSTEM), (winreg.HKEY_CURRENT_USER, USER)):
        try:
            with winreg.OpenKey(hive, key_path) as key:
                i = 0
                while True:
                    try:
                        _, value, _ = winreg.EnumValue(key, i)
                    except OSError:
                        break
                    i += 1
                    if isinstance(value, str) and value:
                        p = Path(value)
                        out.append((p if p.is_absolute() else fonts_dir / p, source))
        except OSError:
            continue
    return out


def system_font_files() -> list[tuple[Path, str]]:
    seen: dict[str, tuple[Path, str]] = {}
    for d, source in _windows_font_dirs():
        if d.is_dir():
            for p in d.iterdir():
                if p.suffix.lower() in FONT_EXTS:
                    seen.setdefault(str(p).lower(), (p, source))
    for p, source in _registry_font_files():
        if p.suffix.lower() in FONT_EXTS and p.is_file():
            seen.setdefault(str(p).lower(), (p, source))
    return list(seen.values())


def dir_font_files(d: str | Path) -> list[Path]:
    """fontsdir 裡的字型檔。libass 只讀這一層、不會進子資料夾，這裡也一樣。"""
    d = Path(d)
    if not d.is_dir():
        return []
    return [p for p in d.iterdir() if p.suffix.lower() in FONT_EXTS and p.is_file()]


class _NameCache:
    """字型名稱快取（路徑+大小+修改時間），避免每次都重讀幾百個系統字型。"""

    def __init__(self) -> None:
        self.path = config_dir() / "font_cache.json"
        try:
            self.data: dict = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            self.data = {}
        self.dirty = False

    def names(self, p: Path) -> set[str]:
        try:
            st = p.stat()
        except OSError:
            return set()
        key = str(p)
        stamp = f"{st.st_size}:{int(st.st_mtime)}"
        entry = self.data.get(key)
        if entry and entry.get("stamp") == stamp:
            return set(entry["names"])
        names = font_file_names(p)
        self.data[key] = {"stamp": stamp, "names": sorted(names)}
        self.dirty = True
        return names

    def save(self) -> None:
        if self.dirty:
            try:
                write_json_atomic(self.path, self.data, indent=None)
            except OSError:
                pass


@dataclass
class FontLocation:
    path: str
    source: str  # SYSTEM / USER / FONTSDIR / EMBEDDED


class FontIndex:
    def __init__(self) -> None:
        self.by_name: dict[str, list[FontLocation]] = {}

    def add(self, names: Iterable[str], loc: FontLocation) -> None:
        for n in names:
            self.by_name.setdefault(n.lower(), []).append(loc)

    def find(self, name: str) -> list[FontLocation]:
        return self.by_name.get(name.lower(), [])

    @classmethod
    def build(cls, fonts_dir: str | Path | None = None, include_system: bool = True,
              embedded: list[tuple[str, bytes]] | None = None) -> "FontIndex":
        idx = cls()
        cache = _NameCache()
        if include_system:
            for p, source in system_font_files():
                idx.add(cache.names(p), FontLocation(str(p), source))
        if fonts_dir:
            for p in dir_font_files(fonts_dir):
                idx.add(cache.names(p), FontLocation(str(p), FONTSDIR))
        cache.save()
        for fname, data in embedded or []:
            try:
                idx.add(font_names(io.BytesIO(data)), FontLocation(fname, EMBEDDED))
            except (ValueError, struct.error):
                pass
        return idx


# ---------------------------------------------------------------------------
# 檢查
# ---------------------------------------------------------------------------

@dataclass
class FontStatus:
    usage: FontUsage
    locations: list[FontLocation]

    @property
    def found(self) -> bool:
        return bool(self.locations)


@dataclass
class FontCheckResult:
    encoding: str
    fonts: list[FontStatus]
    unknown_styles: set[str]
    dialogue_count: int

    @property
    def missing(self) -> list[FontStatus]:
        return [f for f in self.fonts if not f.found]

    def format(self) -> str:
        out = [f"編碼：{self.encoding}　對話行數：{self.dialogue_count}　用到 {len(self.fonts)} 個字型"]
        for f in self.fonts:
            src = "、".join(sorted(f.usage.sources))
            if f.found:
                where = "、".join(sorted({loc.source for loc in f.locations}))
                out.append(f"  ✓ {f.usage.name}　[{where}]　{f.usage.lines} 行（{src}）")
            else:
                out.append(f"  ✗ {f.usage.name}　缺少！　{f.usage.lines} 行（{src}）")
        if self.unknown_styles:
            out.append("  ⚠ 找不到定義的樣式（會改用 Default）：" + "、".join(sorted(self.unknown_styles)))
        return "\n".join(out)


def check_fonts(ass_path: str | Path, fonts_dir: str | Path | None = None,
                index: FontIndex | None = None) -> FontCheckResult:
    """index 可傳入事先建好的系統字型索引（不含 fontsdir），批次時省時間。"""
    text, enc = read_ass(ass_path)
    info = parse_ass(text)
    if index is None:
        index = FontIndex.build(fonts_dir, embedded=info.embedded)
    else:
        extra = FontIndex.build(fonts_dir, include_system=False, embedded=info.embedded)
        merged = FontIndex()
        for idx in (index, extra):
            for k, v in idx.by_name.items():
                merged.by_name.setdefault(k, []).extend(v)
        index = merged
    statuses = [FontStatus(u, index.find(u.name))
                for u in sorted(info.fonts.values(), key=lambda u: -u.lines)]
    return FontCheckResult(enc, statuses, info.unknown_styles, info.dialogue_count)


def main(argv: list[str] | None = None) -> int:
    import argparse
    import sys

    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")
    p = argparse.ArgumentParser(prog="python -m fftoolbox.ass_fonts", description="ASS 字型檢查")
    p.add_argument("ass")
    p.add_argument("--fontsdir")
    a = p.parse_args(argv)
    result = check_fonts(a.ass, a.fontsdir)
    print(result.format())
    return 1 if result.missing else 0


if __name__ == "__main__":
    raise SystemExit(main())
