"""تنزيل ملفات باقة كورسيرا **على السيرفر** (NoteWorm 0.9.4).

سبب وجوده (طلب صريح): «التنزيل يكون عبر السيرفر ثم ننتقل لباقي الخطوات» —
لا تنزيل محلي على الهاتف. التطبيق يكتشف روابط الڤيديو وملفات الترجمة الموقّتة
(مع كوكيز جلسته) فيرسلها هنا، والسيرفر:

1) ينزّل الڤيديو بالبثّ (بلا تحميله في الذاكرة) داخل مجلد الباقة.
2) ينزّل الترجمة SRT/VTT ويحوّلها SRT عربيًا.
3) يدمج العربية داخل MP4 بـ ffmpeg (mov_text) إن توفّر، ويحفظ أيضًا `.ar.srt` منفصلًا.
4) يُسجّل الملفات في الباقة بترتيب الوصول (فيديو ثم ترجمته) ⇒ الترقيم `001 - الاسم`.
5) يُنظّف الملفات المؤقتة، ويترك الباقة جاهزة للتغليف (ZIP/مجلد) ثم السحابة.

كل فشل يُسجَّل لكل ملف على حدة ولا يُسقط الدفعة.
"""
from __future__ import annotations

import ipaddress
import os
import re
import shutil
import socket
import subprocess
import threading
from typing import Any
from urllib.parse import urlparse

import httpx

import courses

MAX_MB = int(os.environ.get("COURSE_FETCH_MAX_MB") or 4096)
ALLOW_LOCAL = (os.environ.get("COURSE_FETCH_ALLOW_LOCAL") or "").strip() in ("1", "true", "yes")
TIMEOUT = float(os.environ.get("COURSE_FETCH_TIMEOUT") or 180)
UA_DEFAULT = os.environ.get("COURSE_FETCH_UA") or (
    "Mozilla/5.0 (Linux; Android 13) AppleWebKit/537.36 Chrome/120 Mobile Safari/537.36")


# ───────────────────────── أدوات ─────────────────────────
def _private_host(host: str) -> bool:
    if not host:
        return True
    if host in ("localhost", "127.0.0.1", "::1"):
        return not ALLOW_LOCAL
    try:
        ip = ipaddress.ip_address(host)
        return ip.is_private or ip.is_loopback or ip.is_reserved or ip.is_link_local
    except ValueError:
        pass
    try:
        infos = socket.getaddrinfo(host, None)
    except Exception:
        return False
    for info in infos:
        try:
            ip = ipaddress.ip_address(info[4][0])
        except Exception:
            continue
        if ip.is_private or ip.is_loopback or ip.is_link_local:
            return True
    return False


def _allowed(url: str) -> bool:
    try:
        u = urlparse(url)
    except ValueError:
        return False
    if u.scheme not in ("https", "http"):
        return False
    if u.scheme == "http" and not ALLOW_LOCAL:
        return False
    return not _private_host(u.hostname or "")


def safe_filename(name: str, fallback: str = "file") -> str:
    base = os.path.basename(str(name or "").strip()) or fallback
    base = re.sub(r"[\\/:*?\"<>|\r\n\t]+", "_", base).strip()
    return (base or fallback)[:120]


def vtt_to_srt(text: str) -> str:
    """VTT ⇒ SRT. يحذف الترويسات والأوسمة ويحوّل الفواصل إلى فاصلة عشرية."""
    lines = text.replace("\r\n", "\n").replace("\r", "\n").split("\n")
    out: list[str] = []
    n = 0
    keep = False
    for raw in lines:
        line = raw.rstrip()
        if line.startswith("WEBVTT") or line.startswith("NOTE") or line.startswith("STYLE") \
                or line.startswith("REGION") or line.startswith("X-TIMESTAMP-MAP"):
            continue
        if "-->" in line:
            stamp = re.sub(r"(\d{2}:\d{2}:\d{2})\.(\d{3})", r"\1,\2", line)
            stamp = stamp.split(" align:")[0].split(" position:")[0].strip()
            n += 1
            out.append(str(n))
            out.append(stamp)
            keep = True
            continue
        if not line.strip():
            if keep:
                out.append("")
            keep = False
            continue
        if keep:
            out.append(line)
    body = "\n".join(out).strip()
    return body + "\n" if body else ""


def looks_arabic(text: str) -> bool:
    return bool(re.search(r"[\u0621-\u064a\u066e-\u06d3]", text or ""))


def ffmpeg_bin() -> str:
    """يبحث عن ffmpeg: متغير بيئة ← PATH ← حزمة imageio-ffmpeg."""
    explicit = (os.environ.get("FFMPEG_BIN") or "").strip()
    if explicit and os.path.exists(explicit):
        return explicit
    found = shutil.which("ffmpeg")
    if found:
        return found
    try:
        import imageio_ffmpeg  # نوع ادخال اختياري

        exe = imageio_ffmpeg.get_ffmpeg_exe()
        if exe and os.path.exists(exe):
            return exe
    except Exception:
        pass
    return ""


# ───────────────────────── التنزيل ─────────────────────────
def _download(url: str, dest: str, cookie: str, ua: str,
              on_bytes=None) -> int:
    if not _allowed(url):
        raise RuntimeError("رابط غير مسموح (نطاق خاص أو مخطط غير مدعوم)")
    limit = MAX_MB * 1024 * 1024
    total = 0
    headers = {"User-Agent": ua or UA_DEFAULT, "Accept": "*/*"}
    if cookie:
        headers["Cookie"] = cookie
    with httpx.Client(timeout=TIMEOUT, follow_redirects=True, max_redirects=6) as client:
        with client.stream("GET", url, headers=headers) as r:
            if r.status_code >= 400:
                raise RuntimeError(f"السيرفر البعيد رفض التنزيل (HTTP {r.status_code})")
            with open(dest, "wb") as fh:
                for chunk in r.iter_bytes(1 << 16):
                    if not chunk:
                        continue
                    total += len(chunk)
                    if total > limit:
                        raise RuntimeError(f"الملف أكبر من {MAX_MB} م.ب")
                    fh.write(chunk)
                    if on_bytes:
                        on_bytes(total)
    if total <= 0:
        raise RuntimeError("الملف نزل فارغًا")
    return total


def _mux(video: str, srt: str, out: str) -> bool:
    exe = ffmpeg_bin()
    if not exe:
        return False
    cmd = [exe, "-y", "-nostdin", "-loglevel", "error", "-i", video, "-i", srt,
           "-map", "0:v:0?", "-map", "0:a?", "-map", "1:0",
           "-c", "copy", "-c:s", "mov_text", "-metadata:s:s:0", "language=ara", out]
    try:
        res = subprocess.run(cmd, capture_output=True, text=True, timeout=1800)
    except Exception:
        return False
    return res.returncode == 0 and os.path.exists(out) and os.path.getsize(out) > 1024


# ───────────────────────── العملية الرئيسية ─────────────────────────
def start(course: "courses.Course", items: list[dict], cookie: str = "",
          user_agent: str = "", embed: bool = True) -> None:
    threading.Thread(target=_run, args=(course, items, cookie, user_agent, embed),
                     daemon=True, name="course-fetch").start()


def _run(course: "courses.Course", items: list[dict], cookie: str,
         user_agent: str, embed: bool) -> None:
    course.fetch_state = "fetching"
    course.fetch_done = 0
    course.fetch_total = max(1, len(items))
    course.fetch_current = ""
    course.fetch_errors = []
    course.fetch_mux = "ffmpeg" if ffmpeg_bin() else "off"
    os.makedirs(course.files_dir, exist_ok=True)
    tmp_dir = os.path.join(course.dir, "tmp")
    os.makedirs(tmp_dir, exist_ok=True)
    muxed = 0
    try:
        for i, item in enumerate(items, start=1):
            name = safe_filename(item.get("name") or f"item-{i}", f"item-{i}")
            url = str(item.get("url") or "")
            kind = str(item.get("kind") or "video")
            sub = item.get("subtitle") or {}
            course.fetch_current = name
            course.fetch_percent = int((i - 1) * 100 / course.fetch_total)
            if not url:
                course.fetch_errors.append({"name": name, "error": "لا رابط للتنزيل"})
                course.fetch_done = i
                continue
            video_tmp = os.path.join(tmp_dir, f"{i:03d}-video.bin")
            srt_tmp = os.path.join(tmp_dir, f"{i:03d}.srt")
            try:
                course.note(f"⬇ تنزيل على السيرفر: {name}")
                _download(url, video_tmp, cookie, user_agent)
                srt_path = srt_tmp
                if not (isinstance(sub, dict) and sub.get("url")):
                    srt_path = ""
                if srt_path:
                    raw = os.path.join(tmp_dir, f"{i:03d}-sub.bin")
                    _download(str(sub["url"]), raw, cookie, user_agent)
                    with open(raw, "rb") as fh:
                        text = fh.read(8 * 1024 * 1024).decode("utf-8", "replace")
                    fmt = str(sub.get("format") or "vtt").lower()
                    srt_text = text if fmt.startswith("srt") else vtt_to_srt(text)
                    if not srt_text.strip():
                        srt_text = vtt_to_srt(text)
                    if not srt_text.strip():
                        raise RuntimeError("ملف الترجمة غير مفهوم (لا توقيتات)")
                    with open(srt_path, "w", encoding="utf-8") as fh:
                        fh.write(srt_text)
                    os.remove(raw)

                final_video = os.path.join(tmp_dir, f"{i:03d}-final.mp4")
                use_video = video_tmp
                if srt_path and embed:
                    out = os.path.join(tmp_dir, f"{i:03d}-muxed.mp4")
                    if _mux(video_tmp, srt_path, out):
                        use_video = out
                        muxed += 1
                        course.note(f"🎬 دُمجت الترجمة العربية داخل MP4: {name}")
                    else:
                        course.note("⚠️ دمج الترجمة داخل MP4 غير متاح (ffmpeg غائب) — "
                                    "الترجمة مرفقة كملف .srt")
                stem = os.path.splitext(name)[0] or f"item-{i}"
                if not stem.lower().endswith(".mp4") and kind == "video":
                    name = stem + ".mp4"
                courses.attach(course, use_video, name, kind)
                if srt_path:
                    courses.attach(course, srt_path, stem + ".ar.srt", "srt")
                course.fetch_done = i
            except Exception as e:                       # noqa: BLE001
                course.fetch_errors.append(
                    {"name": name, "error": (str(e) or "فشل التنزيل")[:200]})
                course.note(f"❌ {name}: {str(e)[:120]}")
                course.fetch_done = i
            finally:
                for leftover in (video_tmp, srt_tmp):
                    try:
                        if leftover and os.path.exists(leftover):
                            os.remove(leftover)
                    except Exception:
                        pass
        course.fetch_percent = 100
        course.fetch_state = "done" if len(course.fetch_errors) < len(items) else "error"
        course.fetch_muxed = muxed
        course.note(f"✅ انتهى التنزيل على السيرفر: {len(course.files)} ملف"
                    f" (دُمجت {muxed} ترجمة) — بلا تنزيل على الهاتف")
    except Exception as e:                               # noqa: BLE001
        course.fetch_state = "error"
        course.fetch_errors.append({"name": "-", "error": str(e)[:200]})
        course.note("❌ توقف التنزيل على السيرفر: " + str(e)[:120])
    finally:
        try:
            shutil.rmtree(tmp_dir, ignore_errors=True)
        except Exception:
            pass
