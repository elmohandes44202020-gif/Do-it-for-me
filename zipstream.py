"""
zipstream — كتابة ZIP على شكل بثّ (stored، بلا ضغط) بدون ملف مؤقت على القرص.

الصوت MP3 مضغوط أصلًا، فالضغط يهدر CPU فقط. كل ملف يُحسب له CRC32 مرة واحدة
عند التحويل، ثم يُرسل في مرور واحد عند التحميل.
"""
from __future__ import annotations

import struct
import zlib
from typing import Iterable, NamedTuple

_LOCAL = struct.Struct("<IHHHHHIIIHH")        # 30 بايت + الاسم
_CENTRAL = struct.Struct("<IHHHHHHIIIHHHHHII")  # 46 بايت + الاسم
_END = struct.Struct("<IHHHHIIH")            # 22 بايت


class Entry(NamedTuple):
    name: str   # الاسم داخل الأرشيف (UTF-8)
    path: str   # مساره على القرص
    size: int
    crc: int


def crc_of(path: str, chunk: int = 1 << 20) -> tuple[int, int]:
    """يعيد (crc, size) لملف على القرص."""
    crc = 0
    size = 0
    with open(path, "rb") as fh:
        while True:
            block = fh.read(chunk)
            if not block:
                break
            crc = zlib.crc32(block, crc)
            size += len(block)
    return crc & 0xFFFFFFFF, size


def zip_size(entries: Iterable[Entry]) -> int:
    """الحجم الكلي للأرشيف بالبايت — يُستخدم كـContent-Length لشريط تقدّم دقيق."""
    total = 22
    for e in entries:
        n = len(e.name.encode("utf-8"))
        total += 30 + n + e.size      # local header + data
        total += 46 + n               # central directory record
    return total


def stream_zip(entries: Iterable[Entry], chunk: int = 1 << 16):
    """مولّد يعيد بايتات ZIP صالحة للتنزيل المتدفّق."""
    central: list[bytes] = []
    offset = 0
    count = 0

    for e in entries:
        raw = e.name.encode("utf-8")
        if len(raw) > 0xFFFF:
            raise ValueError("اسم ملف طويل جدًا")

        local_offset = offset
        # flags = 0x0800 → أسماء UTF-8، method = 0 → stored
        head = _LOCAL.pack(
            0x04034B50, 20, 0x0800, 0, 0, 0, e.crc, e.size, e.size, len(raw), 0
        ) + raw
        yield head
        offset += len(head)

        sent = 0
        with open(e.path, "rb") as fh:
            while True:
                block = fh.read(chunk)
                if not block:
                    break
                sent += len(block)
                yield block
        if sent != e.size:  # pragma: no cover
            raise RuntimeError(f"حجم غير متوقع للملف {e.name}")
        offset += sent

        central.append(_CENTRAL.pack(
            0x02014B50, 20, 20, 0x0800, 0, 0, 0,
            e.crc, e.size, e.size, len(raw), 0, 0, 0, 0, 0, local_offset,
        ) + raw)
        count += 1

    cd_start = offset
    for rec in central:
        yield rec
        offset += len(rec)

    yield _END.pack(0x06054B50, 0, 0, count, count, offset - cd_start, cd_start, 0)
