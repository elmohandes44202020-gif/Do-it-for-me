"""
media.py — تنزيل الصوت من جوجل وتحويله m4a → mp3 بمكتبة PyAV فقط.

لا يُستخدم أي ffmpeg خارجي: الـwheel الرسمي لـPyAV يحمل libav* وlibmp3lame داخله.
"""
from __future__ import annotations

import os
import re
from typing import Callable, Iterable
from urllib.parse import urlparse

import av
import httpx

# النطاقات المسموح التنزيل منها فقط (نفس قائمة AudioTransfer.java) — يمنع SSRF
_ALLOWED_SUFFIX = (
    "google.com",
    "googleusercontent.com",
    "googleapis.com",
    "googlevideo.com",
    "gvt1.com",
    "gvt2.com",
    "gstatic.com",
)

# تُستخدم في الاختبارات المحلية فقط
_EXTRA = tuple(
    h.strip().lower() for h in os.environ.get("EXTRA_ALLOWED_HOSTS", "").split(",") if h.strip()
)

DEFAULT_UA = (
    "Mozilla/5.0 (Linux; Android 14) AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/125.0.0.0 Mobile Safari/537.36"
)


class MediaError(Exception):
    pass


def host_allowed(url: str) -> bool:
    try:
        u = urlparse(url)
    except ValueError:
        return False
    if u.scheme != "https" and u.hostname not in _EXTRA:
        return False
    h = (u.hostname or "").lower()
    if h in _EXTRA:
        return True
    return any(h == s or h.endswith("." + s) for s in _ALLOWED_SUFFIX)


def _headers(url: str, cookie: str | None, user_agent: str | None, referer: str | None) -> dict:
    h = {
        "User-Agent": user_agent or DEFAULT_UA,
        "Accept": "*/*",
        "Accept-Encoding": "identity",
        "Referer": referer or "https://notebooklm.google.com/",
    }
    if cookie:
        h["Cookie"] = cookie
    return h


def download(
    url: str,
    dest: str,
    *,
    cookie: str | None = None,
    user_agent: str | None = None,
    referer: str | None = None,
    max_bytes: int = 400 * 1024 * 1024,
    timeout: float = 120.0,
    on_bytes: Callable[[int], None] | None = None,
) -> int:
    """ينزّل الملف إلى dest بالبثّ ويعيد عدد البايتات. لا يحتفظ بأي شيء في الذاكرة."""
    if not host_allowed(url):
        raise MediaError(f"نطاق غير مسموح: {urlparse(url).hostname}")

    written = 0
    head = b""
    with httpx.Client(timeout=timeout, follow_redirects=True, http2=False) as client:
        with client.stream("GET", url, headers=_headers(url, cookie, user_agent, referer)) as r:
            if r.status_code in (401, 403):
                raise MediaError(
                    f"HTTP {r.status_code} — جوجل رفضت الطلب بدون جلسة صالحة "
                    "(الرابط يحتاج كوكيز أو انتهت صلاحيته)"
                )
            if r.status_code >= 400:
                raise MediaError(f"HTTP {r.status_code}")
            ctype = (r.headers.get("content-type") or "").lower()
            with open(dest, "wb") as fh:
                for block in r.iter_bytes(1 << 16):
                    if not head:
                        head = block[:64]
                    written += len(block)
                    if written > max_bytes:
                        raise MediaError(f"تجاوز الحد المسموح ({max_bytes} بايت)")
                    fh.write(block)
                    if on_bytes:
                        on_bytes(written)

    if written == 0:
        raise MediaError("ملف فارغ")
    if head[:5].lower() in (b"<!doc", b"<html") or b"<html" in head[:64].lower():
        raise MediaError("الاستجابة صفحة HTML وليست صوتًا (تسجيل دخول مطلوب غالبًا)")
    if ctype.startswith("audio/") or ctype == "application/octet-stream" or ctype == "":
        return written
    raise MediaError(f"نوع محتوى غير متوقع: {ctype}")


def probe(
    url: str,
    *,
    cookie: str | None = None,
    user_agent: str | None = None,
    referer: str | None = None,
) -> dict:
    """اختبار: هل يستطيع السيرفر تنزيل هذا الرابط؟ (بدون كوكيز أولًا)."""
    out = {"url": url, "host_allowed": host_allowed(url)}
    if not out["host_allowed"]:
        out["ok"] = False
        return out
    try:
        with httpx.Client(timeout=30, follow_redirects=True) as client:
            r = client.get(
                url,
                headers={**_headers(url, cookie, user_agent, referer), "Range": "bytes=0-2047"},
            )
        out["status"] = r.status_code
        out["content_type"] = r.headers.get("content-type")
        out["content_length"] = r.headers.get("content-length")
        body = r.content[:64]
        out["looks_audio"] = not (body[:5].lower() == b"<!doc" or b"<html" in body.lower())
        out["ok"] = out["status"] in (200, 206) and out["looks_audio"]
    except Exception as exc:  # noqa: BLE001
        out["ok"] = False
        out["error"] = f"{type(exc).__name__}: {exc}"
    return out


def m4a_to_mp3(
    src: str,
    dst: str,
    *,
    bitrate: int = 96_000,
    mono: bool = True,
    on_progress: Callable[[float], None] | None = None,
) -> dict:
    """تحويل بثّي (frame by frame). الذاكرة المستخدمة ثابتة تقريبًا مهما طال الملف."""
    with av.open(src) as inp, av.open(dst, "w", format="mp3") as out:
        if not inp.streams.audio:
            raise MediaError("الملف لا يحتوي مسارًا صوتيًا")
        istream = inp.streams.audio[0]
        ostream = out.add_stream("libmp3lame", rate=istream.rate or 44100)
        ostream.bit_rate = bitrate
        ostream.layout = "mono" if mono else "stereo"

        res = av.AudioResampler(
            format="s16p", layout=ostream.layout, rate=ostream.rate
        )
        total = float(istream.duration * istream.time_base) if istream.duration else 0.0
        done = 0.0

        def push(frames: Iterable) -> None:
            for f in frames:
                for packet in ostream.encode(f):
                    out.mux(packet)

        for frame in inp.decode(istream):
            push(res.resample(frame))
            done += float(frame.samples) / float(frame.sample_rate or 1)
            if on_progress and total > 0:
                on_progress(min(done / total, 1.0))
        push(res.resample(None))
        for packet in ostream.encode(None):   # إنهاء المُرمِّز
            out.mux(packet)
    with av.open(dst) as check:
        dur = float(check.duration / 1_000_000) if check.duration else 0.0
    if dur <= 0:
        raise MediaError("ناتج MP3 غير صالح")
    return {"duration_sec": round(dur, 1), "bytes": os.path.getsize(dst)}


_FS_UNSAFE = re.compile(r'[\\/:*?"<>|\x00-\x1f]+')
_WS = re.compile(r"\s+")


def safe_name(title: str, index: int, fallback: str = "audio") -> str:
    """اسم ملف آمن للحفظ على أندرويد وويندوز."""
    base = _WS.sub(" ", (title or "").strip())
    base = _FS_UNSAFE.sub("", base).strip(" .")
    if not base:
        base = f"{fallback}-{index:02d}"
    if len(base) > 80:
        base = base[:80].rstrip()
    return base
