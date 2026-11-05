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



# ── اختيار الكوكيز لكل نطاق ────────────────────────────────────────────────
# درس مهم: إرسال كوكيز نطاق .google.com إلى googleusercontent.com يجعل جوجل
# تعيد صفحة CookieMismatch/تسجيل دخول. الكوكيز تُرسل فقط للنطاقات التي
# أُصدرت لها، وهذا ما كان يفسد كل محاولاتنا السابقة.
def cookie_for_host(host: str, cookie_map: dict[str, str]) -> str:
    """أطول نطاق مطابق فقط. مثال: lh3.google.com → كوكيز google.com،
    و lh3.googleusercontent.com → لا شيء (لأن google.com ليست لاحقة لها)."""
    if not host or not cookie_map:
        return ""
    host = host.lower()
    best = ""
    best_len = -1
    for domain, value in cookie_map.items():
        if not value:
            continue
        d = (domain or "").lower().lstrip(".")
        if not d:
            continue
        if (host == d or host.endswith("." + d)) and len(d) > best_len:
            best = value
            best_len = len(d)
    return best


def _map_from_request(cookie: str | None, cookie_map: dict[str, str] | None) -> dict[str, str]:
    out = dict(cookie_map or {})
    if cookie:
        # الكوكي المسطّحة (كما يرسلها التطبيق) تُعامل ككوكيز جوجل فقط،
        # ولا تُرسل أبدًا إلى نطاق googleusercontent.
        out.setdefault("google.com", cookie)
    return out


def _cookie_names(cookie: str) -> str:
    if not cookie:
        return "0"
    return str(len([c for c in cookie.split(";") if "=" in c]))


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
    cookie_map: dict[str, str] | None = None,
    user_agent: str | None = None,
    referer: str | None = None,
    max_bytes: int = 400 * 1024 * 1024,
    timeout: float = 120.0,
    on_bytes: Callable[[int], None] | None = None,
) -> int:
    """ينزّل الملف إلى dest بالبثّ مع كوكيز صحيحة لكل قفزة، ويعيد عدد البايتات."""
    if not host_allowed(url):
        raise MediaError(f"نطاق غير مسموح: {urlparse(url).hostname}")

    r = fetch_traced(
        url, cookie=cookie, cookie_map=cookie_map, user_agent=user_agent,
        referer=referer, dest=dest, max_bytes=max_bytes, timeout=timeout,
    )
    if not r.get("ok"):
        ct = r.get("content_type") or ""
        raise MediaError(
            (r.get("error") or "فشل التنزيل")
            + f" · الحجم={r.get('bytes', 0)} · النوع={ct or '-'}"
            + " · المسار: " + trace_summary(r)
        )
    size = int(r.get("bytes") or 0)
    if on_bytes:
        on_bytes(size)
    return size


def fetch_traced(
    url: str,
    *,
    cookie: str | None = None,
    cookie_map: dict[str, str] | None = None,
    user_agent: str | None = None,
    referer: str | None = None,
    max_hops: int = 6,
    sniff: int = 400,
    dest: str | None = None,
    max_bytes: int = 400 * 1024 * 1024,
    timeout: float = 120.0,
) -> dict:
    """
    يتبع التحويلات يدويًا (بلا follow_redirects) ويختار الكوكيز لكل قفزة حسب
    نطاقها. إن مُرِّر dest يكتب جسم الاستجابة النهائية فيه (تنزيل حقيقي)،
    وإلا يقرأ أول sniff بايت فقط (تشخيص).
    """
    cmap = _map_from_request(cookie, cookie_map)
    hops: list[dict] = []
    current = url
    result: dict = {"ok": False, "hops": hops}

    with httpx.Client(timeout=timeout, follow_redirects=False) as client:
        for index in range(max_hops):
            if not host_allowed(current):
                hops.append({"hop": index + 1, "host": urlparse(current).hostname, "blocked": True})
                result["error"] = "نطاق غير مسموح في سلسلة التحويل"
                return result

            host = urlparse(current).hostname or ""
            ck = cookie_for_host(host, cmap)
            headers = _headers(current, ck or None, user_agent, referer)
            with client.stream("GET", current, headers=headers) as r:
                ctype = (r.headers.get("content-type") or "").lower()
                location = r.headers.get("location") or ""
                hops.append({
                    "hop": index + 1,
                    "host": host,
                    "status": r.status_code,
                    "type": ctype or "-",
                    "cookies": _cookie_names(ck),
                    "location": location[:120],
                })

                if r.status_code in (301, 302, 303, 307, 308) and location:
                    current = str(httpx.URL(current).join(location))
                    continue

                result["status"] = r.status_code
                result["content_type"] = ctype
                result["final_url"] = current
                if r.status_code >= 400:
                    snippet = b""
                    try:
                        for chunk in r.iter_bytes(512):
                            snippet += chunk
                            if len(snippet) >= sniff:
                                break
                    except Exception:  # noqa: BLE001
                        pass
                    result["error"] = (
                        f"HTTP {r.status_code} · "
                        + snippet[:200].decode("latin-1", "replace").replace("\n", " ").strip()
                    )
                    return result

                fh = open(dest, "wb") if dest else None
                written = 0
                head = b""
                try:
                    for chunk in r.iter_bytes(1 << 16):
                        if not head:
                            head = chunk[:sniff]
                        written += len(chunk)
                        if written > max_bytes:
                            result["error"] = "تجاوز الحد الأقصى للحجم"
                            return result
                        if fh:
                            fh.write(chunk)
                        elif written >= sniff:
                            break
                finally:
                    if fh:
                        fh.close()

                result["bytes"] = written
                result["first_bytes"] = head[:120].decode("latin-1", "replace").replace("\n", " ")
                low = head[:64].lower()
                looks_audio = not (low.startswith(b"<!doc") or b"<html" in low)
                result["looks_audio"] = looks_audio
                result["ok"] = looks_audio and written > 0
                if not looks_audio:
                    result["error"] = "الاستجابة صفحة HTML وليست صوتًا"
                return result

    result["final_url"] = current
    result["error"] = "تحويلات كثيرة جدًا (حلقة؟)"
    return result


def trace_summary(result: dict) -> str:
    """سلسلة مختصرة تُطبع في تقرير التشخيص."""
    parts = []
    for h in result.get("hops") or []:
        parts.append(
            f"#{h.get('hop')} {h.get('host', '?')} → {h.get('status', '?')} "
            f"{h.get('type', '-')} cookies={h.get('cookies', '0')}"
            + (f" → {h.get('location')}" if h.get("location") else "")
        )
    return " | ".join(parts) if parts else "لا قفزات"


def probe(
    url: str,
    *,
    cookie: str | None = None,
    cookie_map: dict[str, str] | None = None,
    user_agent: str | None = None,
    referer: str | None = None,
) -> dict:
    """اختبار تفصيلي مع تتبّع كل تحويلة وكل نطاق على حدة."""
    out: dict = {"url": url, "host_allowed": host_allowed(url)}
    if not out["host_allowed"]:
        out["ok"] = False
        out["error"] = "نطاق غير مسموح للسيرفر"
        return out
    try:
        r = fetch_traced(url, cookie=cookie, cookie_map=cookie_map,
                         user_agent=user_agent, referer=referer, sniff=400)
    except Exception as exc:  # noqa: BLE001
        out["ok"] = False
        out["error"] = f"{type(exc).__name__}: {exc}"
        return out

    out["ok"] = bool(r.get("ok"))
    out["status"] = r.get("status")
    out["content_type"] = r.get("content_type")
    out["first_bytes"] = r.get("first_bytes", "")
    out["final_url"] = r.get("final_url") or url
    out["trace"] = trace_summary(r)
    out["hops"] = r.get("hops")
    if r.get("error"):
        out["error"] = r["error"]
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
