"""
main.py — خدمة تحويل حلقات NotebookLM الصوتية إلى MP3 وتقديمها كرابط تحميل مباشر.

مصمّمة للخطة المجانية على Faable (bi.xs: 0.6 vCPU / 512 MB، تطبيق واحد، ينام عند الخمول):

  • تنزيل واحد في نفس الوقت، وتحويل تسلسلي — الذاكرة تبقى منخفضة.
  • لا ffmpeg خارجي: التحويل عبر PyAV.
  • الملفات في /tmp فقط، وتُحذف بعد انتهاء صلاحيتها (TTL) — لا تخزين دائم.
  • الـZIP يُرسل بثًّا أثناء البناء (zipstream) لتوفير مساحة القرص.
  • الكوكيز تُمرَّر لكل مهمة، تُستخدم في الذاكرة، ثم تُمحى فورًا ولا تُسجَّل أبدًا.
"""
from __future__ import annotations

import hashlib
import hmac
import os
import queue
import platform
import shutil
import sys
import threading
import time
import uuid
from dataclasses import dataclass, field
from typing import Any
from urllib.parse import urlparse

from fastapi import (
    Depends,
    FastAPI,
    File,
    Form,
    Header,
    HTTPException,
    Request,
    UploadFile,
)
from fastapi.responses import JSONResponse, StreamingResponse
from pydantic import BaseModel, Field

import av as _av
import mega_up
import tg_up
import notecol as colls
import links
import media
import zipstream

# ─────────────────────────────── التطبيق ───────────────────────────────
app = FastAPI(title="NotebookLM → MP3", version="0.1.0")

# ─────────────────────────────── الإعدادات ───────────────────────────────
WORK_DIR = os.environ.get("WORK_DIR", "/tmp/nlm")
TTL_MINUTES = int(os.environ.get("TTL_MINUTES", "240"))
MP3_BITRATE = int(os.environ.get("MP3_BITRATE", "96000"))
MP3_MONO = os.environ.get("MP3_MONO", "1") not in ("0", "false", "False", "")
MAX_FILES = int(os.environ.get("MAX_FILES", "20"))
MAX_FILE_MB = int(os.environ.get("MAX_FILE_MB", "400"))
MAX_JOB_MB = int(os.environ.get("MAX_JOB_MB", "250"))
DEBUG = os.environ.get("NLM_DEBUG", "0") == "1"


LINK_TTL_SEC = int(os.environ.get("LINK_TTL_MINUTES", "240")) * 60


def _link_secret() -> str:
    env = os.environ.get("LINK_SECRET")
    if env:
        return env
    keys = _load_keys()
    return (next(iter(keys.values()), "") or "dev-secret") + "|links"


def sign_link(kind: str, ident: str, ttl: int = LINK_TTL_SEC) -> str:
    """توقيع مؤقت يسمح بتحميل الملف من المتصفح بدون ترويسة المفتاح."""
    exp = int(time.time()) + ttl
    mac = hmac.new(_link_secret().encode(), f"{kind}:{ident}:{exp}".encode(),
                   hashlib.sha256).hexdigest()[:40]
    return f"{exp}.{mac}"


def check_link(kind: str, ident: str, token: str | None) -> bool:
    if not token:
        return False
    try:
        exp_s, mac = token.split(".", 1)
        exp = int(exp_s)
    except Exception:
        return False
    if time.time() > exp:
        return False
    good = hmac.new(_link_secret().encode(), f"{kind}:{ident}:{exp}".encode(),
                    hashlib.sha256).hexdigest()[:40]
    return hmac.compare_digest(good, mac)


def _load_keys() -> dict[str, str]:
    """API_KEYS="ahmed:K1,sara:K2"  →  {hash(K1): "ahmed", ...}"""
    raw = os.environ.get("API_KEYS") or os.environ.get("API_KEY") or ""
    keys: dict[str, str] = {}
    for i, part in enumerate(p.strip() for p in raw.split(",") if p.strip()):
        name, _, key = part.partition(":")
        if not key:
            name, key = f"user{i+1}", name
        keys[hashlib.sha256(key.encode()).hexdigest()[:16]] = name
    return keys


KEYS = _load_keys()


def owner_of(token: str | None) -> str:
    if not token:
        raise HTTPException(status_code=401, detail="مفتاح API مطلوب")
    digest = hashlib.sha256(token.encode()).hexdigest()[:16]
    for known, name in KEYS.items():
        if hmac.compare_digest(known, digest):
            return name
    raise HTTPException(status_code=403, detail="مفتاح API غير صالح")


def auth(x_api_key: str | None = Header(default=None)) -> str:
    return owner_of(x_api_key)


# ─────────────────────────────── نماذج الطلب ───────────────────────────────
class FileSpec(BaseModel):
    title: str = Field(default="", max_length=200)
    url: str
    cookie: str | None = None          # كوكيز مسطّحة: تُعامل كنطاق google.com فقط
    cookie_map: dict[str, str] | None = None   # الأفضل: كوكيز لكل نطاق على حدة
    referer: str | None = None
    bitrate: int | None = None
    mono: bool | None = None


class JobRequest(BaseModel):
    files: list[FileSpec] = Field(min_length=1)
    cookie: str | None = None          # كوكي مشترك (يُعامل كنطاق google.com)
    cookie_map: dict[str, str] | None = None   # كوكيز لكل نطاق — الأدق
    user_agent: str | None = None      # يُمرَّر من التطبيق = نفس UA الجهاز
    referer: str | None = None         # صفحة الدفتر — جوجل تتحقق منها أحيانًا
    zip_name: str = "notebooklm-audio"


class CookieVariant(BaseModel):
    label: str = ""
    cookie: str | None = None


class DiagnoseRequest(BaseModel):
    url: str
    cookies: list[CookieVariant] = Field(default_factory=list)
    cookie_map: dict[str, str] | None = None
    user_agent: str | None = None
    referer: str | None = None


class ProbeRequest(BaseModel):
    url: str
    cookie: str | None = None
    user_agent: str | None = None
    referer: str | None = None


# ─────────────────────────────── حالة المهام ───────────────────────────────
@dataclass
class JobFile:
    title: str
    url: str | None
    cookie: str | None
    referer: str | None
    bitrate: int
    mono: bool
    state: str = "queued"          # queued | downloading | converting | done | error
    bytes_in: int = 0
    bytes_out: int = 0
    duration_sec: float = 0.0
    error: str | None = None
    mp3_path: str | None = None
    zip_name: str = ""
    crc: int = 0
    src_path: str | None = None   # ملف مرفوع من الهاتف (المسار البديل)
    cookie_map: dict[str, str] | None = None   # كوكيز لكل نطاق (مسار التنزيل من السيرفر)


@dataclass
class Job:
    id: str
    owner: str
    user_agent: str | None
    zip_name: str
    files: list[JobFile]
    state: str = "queued"          # queued | running | done | error
    error: str | None = None
    shared_cookie: str | None = None
    created_at: float = field(default_factory=time.time)
    finished_at: float | None = None
    expires_at: float | None = None

    def public(self, base: str) -> dict[str, Any]:
        total = len(self.files)
        finished = sum(1 for f in self.files if f.state == "done")
        pct = int(round(100 * finished / total)) if total else 0
        running = next((f for f in self.files if f.state in ("downloading", "converting")), None)
        return {
            "job_id": self.id,
            "state": self.state,
            "error": self.error,
            "progress_percent": pct,
            "active_title": running.title if running else None,
            "files": [
                {
                    "index": i,
                    "download_url": (
                        f"{base}/jobs/{self.id}/files/{i}" if f.state == "done" else None
                    ),
                    "name": f.zip_name or None,
                    "title": f.title,
                    "state": f.state,
                    "bytes_in": f.bytes_in,
                    "bytes_out": f.bytes_out,
                    "duration_sec": f.duration_sec,
                    "error": f.error,
                }
                for i, f in enumerate(self.files, start=1)
            ],
            "total_bytes_out": sum(f.bytes_out for f in self.files),
            "zip_url": f"{base}/jobs/{self.id}/zip" if self.state == "done" else None,
            "expires_in_sec": (
                max(0, int(self.expires_at - time.time())) if self.expires_at else None
            ),
        }


STARTED_AT = time.time()
JOBS: dict[str, Job] = {}
LOCK = threading.Lock()
QUEUE: "queue.Queue[str]" = queue.Queue()


# ─────────────────────────────── العامل ───────────────────────────────
def _work(job_id: str) -> None:
    job = JOBS[job_id]
    job.state = "running"
    job_dir = os.path.join(WORK_DIR, job_id)
    os.makedirs(job_dir, exist_ok=True)
    budget = MAX_JOB_MB * 1024 * 1024
    used = 0   # بايتات ملفات MP3 المحفوظة (المصدر يُحذف بعد كل تحويل)

    # أسماء الملفات = عناوين الحلقات الأصلية كما هي، بلا ترقيم ولا بادئات.
    taken: set[str] = set()
    for i, f in enumerate(job.files, start=1):
        f.zip_name = media.episode_filename(f.title, i, taken)

    try:
        for index, f in enumerate(job.files, start=1):
            src = os.path.join(job_dir, f"src-{index}.m4a")
            dst = os.path.join(job_dir, f"out-{index:02d}.mp3")
            try:
                if f.src_path:
                    # الملف وصل جاهزًا من الهاتف — لا حاجة لأي تنزيل من جوجل
                    f.state = "converting"
                    f.bytes_in = os.path.getsize(f.src_path)
                else:
                    allowed = budget - used
                    budget_limited = allowed < MAX_FILE_MB * 1024 * 1024
                    f.state = "downloading"
                    try:
                        media.download(
                            f.url,
                            src,
                            cookie=f.cookie or job.shared_cookie,
                        cookie_map=f.cookie_map,
                        user_agent=job.user_agent,
                        referer=f.referer,
                            max_bytes=min(MAX_FILE_MB * 1024 * 1024, max(1, allowed)),
                            on_bytes=lambda n, _f=f: setattr(_f, "bytes_in", n),
                        )
                    except media.MediaError as exc:
                        if budget_limited and "تجاوز الحد" in str(exc):
                            raise media.MediaError(
                                "لا توجد مساحة كافية في هذه المهمة (المتبقي {} م.ب من {} م.ب). "
                                "أرسل بقية الحلقات في دفعة جديدة.".format(
                                    max(0, allowed) // (1024 * 1024), MAX_JOB_MB)) from None
                        raise
                    f.state = "converting"
                info = media.m4a_to_mp3(src, dst, bitrate=f.bitrate, mono=f.mono)
                f.duration_sec = info["duration_sec"]
                f.bytes_out = info["bytes"]
                f.mp3_path = dst
                f.crc, size = zipstream.crc_of(dst)
                f.bytes_out = size
                used += size          # ما يُحفظ فعلًا على القرص هو الناتج فقط
                f.state = "done"
            except Exception as exc:  # noqa: BLE001
                f.state = "error"
                msg = f"{type(exc).__name__}: {exc}" if DEBUG else str(exc)
                if used >= budget * 0.95:
                    msg += " — انتهت مساحة المهمة ({} م.ب). قلّل عدد الحلقات في الدفعة.".format(
                        MAX_JOB_MB)
                f.error = msg
                if os.path.exists(dst):
                    os.remove(dst)
            finally:
                if os.path.exists(src):
                    os.remove(src)   # لا نحتفظ بالـm4a الأصلي

        done = [f for f in job.files if f.state == "done"]
        job.finished_at = time.time()
        job.expires_at = job.finished_at + TTL_MINUTES * 60
        job.state = "done" if done else "error"
        if not done:
            job.error = "كل الملفات فشلت"
    except Exception as exc:  # noqa: BLE001
        job.state = "error"
        job.error = str(exc)
        job.expires_at = time.time() + TTL_MINUTES * 60
    finally:
        # محو الكوكيز من الذاكرة فورًا
        for f in job.files:
            f.cookie = None
        job.shared_cookie = None


def _worker_loop() -> None:
    while True:
        job_id = QUEUE.get()
        try:
            _work(job_id)
        except Exception:  # noqa: BLE001
            pass
        finally:
            QUEUE.task_done()


def _janitor_loop() -> None:
    while True:
        time.sleep(60)
        now = time.time()
        with LOCK:
            stale = [
                j for j in JOBS.values()
                if j.expires_at and j.expires_at < now
            ]
            for j in stale:
                JOBS.pop(j.id, None)
                shutil.rmtree(os.path.join(WORK_DIR, j.id), ignore_errors=True)


@app.on_event("startup")
def _startup() -> None:
    shutil.rmtree(WORK_DIR, ignore_errors=True)   # لا بقايا من تشغيل سابق
    os.makedirs(WORK_DIR, exist_ok=True)
    colls.ON_READY = _auto_mega          # الرفع التلقائي إلى MEGA
    threading.Thread(target=_worker_loop, daemon=True, name="nlm-worker").start()
    threading.Thread(target=_mega_engine, daemon=True, name="nlm-mega-engine").start()
    threading.Thread(target=_janitor_loop, daemon=True, name="nlm-janitor").start()




@app.get("/diag")
def diag(_owner: str = Depends(auth)) -> dict:
    """حالة السيرفر الحقيقية: ذاكرة، قرص، خيوط، ونشاط كل مجموعة، وحالة MEGA."""
    def _read(path: str) -> str | None:
        try:
            with open(path) as fh:
                return fh.read().strip()
        except Exception:
            return None

    rss_mb = None
    status = _read("/proc/self/status") or ""
    for line in status.splitlines():
        if line.startswith("VmRSS:"):
            try:
                rss_mb = round(int(line.split()[1]) / 1024, 1)
            except Exception:
                rss_mb = None
            break
    cg_max, cg_cur = _read("/sys/fs/cgroup/memory.max"), _read("/sys/fs/cgroup/memory.current")

    def _mb(v):
        try:
            return round(int(v) / (1024 * 1024), 1)
        except Exception:
            return None

    try:
        du = shutil.disk_usage(WORK_DIR)
        disk = {"free_mb": round(du.free / 1048576, 1),
                "total_mb": round(du.total / 1048576, 1),
                "used_mb": round(du.used / 1048576, 1)}
    except Exception:
        disk = {}

    now = time.time()
    cols = []
    for c in colls.COLS.values():
        cur = next((i for i in c.items
                    if i.state in ("downloading", "converting")), None)
        cols.append({
            "id": c.id, "name": c.name, "state": c.state,
            "done": c.counts()["done"], "failed": c.counts()["error"],
            "pending": c.counts()["pending"],
            "current": None if cur is None else {
                "title": cur.title, "state": cur.state,
                "attempts": cur.attempts, "bytes_in": cur.bytes_in,
                "since_sec": round(now - c.last_tick, 1),
            },
            "idle_sec": round(now - c.last_tick, 1),
        })

    return {
        "ok": True,
        "version": "0.9.1",
        "uptime_sec": round(now - STARTED_AT, 1),
        "mega": mega_up.info(),
        "memory": {"rss_mb": rss_mb,
                   "cgroup_max_mb": _mb(cg_max) if cg_max not in (None, "max") else None,
                   "cgroup_used_mb": _mb(cg_cur)},
        "disk": disk,
        "threads": threading.active_count(),
        "workers": colls.WORKERS,
        "retries": colls.RETRIES,
        "collections": cols,
        "jobs": len(JOBS),
        "max_collection_mb": colls.MAX_COLLECTION_MB,
    }


@app.get("/healthz")
def healthz() -> dict:
    return {
        "ok": True,
        "service": "notebooklm-mp3",
        "version": "0.9.1",
        "python": sys.version.split()[0],
        "pyav": getattr(_av, "__version__", "?"),
        "jobs": len(JOBS),
        "queued": QUEUE.qsize(),
        "auth_configured": bool(KEYS),
        "settings": {
            "ttl_minutes": TTL_MINUTES,
            "mp3_bitrate": MP3_BITRATE,
            "mp3_mono": MP3_MONO,
            "max_files": MAX_FILES,
        },
    }


@app.post("/probe")
def probe(req: ProbeRequest, _owner: str = Depends(auth)) -> dict:
    """هل الرابط قابل للتنزيل من السيرفر؟ جرّب بدون كوكيز أولًا، ثم معها إن مُرّرت."""
    out: dict[str, Any] = {"without_cookie": media.probe(
        req.url, user_agent=req.user_agent, referer=req.referer)}
    if not out["without_cookie"].get("ok") and req.cookie:
        out["with_cookie"] = media.probe(
            req.url, cookie=req.cookie, user_agent=req.user_agent, referer=req.referer)
    out["cookie_needed"] = not out["without_cookie"].get("ok")
    return out


def _verdict(attempts: list[dict]) -> dict:
    """قراءة النتائج وترجمتها إلى توصية واحدة واضحة."""
    ok_variants = [a for a in attempts if a.get("ok")]
    statuses = [a.get("status") for a in attempts]
    any_cookie_ok = any(a.get("ok") for a in ok_variants if a.get("cookie_len", 0) > 0)
    no_cookie_ok = any(a.get("ok") for a in ok_variants if a.get("cookie_len", 0) == 0)

    if no_cookie_ok:
        return {
            "code": "link-public",
            "arabic": "الرابط يُنزَّل من السيرفر بدون أي كوكيز. لو المهمة فشلت رغم ذلك فالمشكلة في التحويل أو في الرابط نفسه لا في الصلاحيات.",
            "english": "The URL is downloadable server-side without cookies; a job failure is then about the link or transcoding, not auth.",
        }
    if any_cookie_ok:
        return {
            "code": "cookie-required",
            "arabic": "الرابط يحتاج كوكيز. أعد إرسال المهمة مع تعليم مربّع الكوكيز، وتأكد أن الكوكي المُرسل هو كوكي حساب جوجل (نطاق .google.com) لا كوكي نطاق الصوت.",
            "english": "Cookie required. Resubmit with cookies enabled — and send the .google.com account cookies, not the media-host ones.",
        }
    if all(s == 403 for s in statuses if s):
        return {
            "code": "google-blocks-server-ip",
            "arabic": "جوجل ترد 403 على كل المحاولات حتى مع الكوكيز: الرابط مربوط بجلسة/جهاز طالب مختلف، أو الجلسة انتهت. الحل العملي: نزّل على الهاتف ثم حوّل على السيرفر، أو أعد الاكتشاف لتحصل على رابط جديد وأرسله فورًا.",
            "english": "Google answers 403 for every attempt: the URL is bound to a different session/device or expired. Re-discover for a fresh URL, or download on-device and convert server-side.",
        }
    if all(s == 404 for s in statuses if s):
        return {
            "code": "expired",
            "arabic": "الرابط منتهي الصلاحية (404). أعد الاكتشاف في NotebookLM لتحصل على رابط جديد.",
            "english": "Expired link (404). Re-run discovery to get a fresh URL.",
        }
    if any(a.get("content_type", "") and "html" in str(a.get("content_type")).lower() for a in attempts):
        return {
            "code": "login-page",
            "arabic": "جوجل تعيد صفحة HTML (تسجيل دخول) بدل الصوت: الجلسة غير صالحة لهذا الرابط. أعد تسجيل الدخول في التطبيق ثم أعد الاكتشاف.",
            "english": "Google returns an HTML login page instead of audio: session invalid for this URL. Sign in again and re-discover.",
        }
    return {
        "code": "unknown",
        "arabic": "النتائج غير حاسمة. أرسل التقرير كما هو.",
        "english": "Inconclusive. Send the report as is.",
    }


@app.post("/diagnose")
def diagnose(req: DiagnoseRequest, owner: str = Depends(auth)) -> dict:
    """تشخيص شامل للرابط: عدة محاولات بكوكيز مختلفة + توصية نهائية."""
    variants = req.cookies or [CookieVariant(label="no-cookie", cookie=None)]
    attempts: list[dict] = []
    for v in variants:
        r = media.probe(
            req.url,
            cookie=v.cookie,
            cookie_map=req.cookie_map if not v.cookie else None,
            user_agent=req.user_agent,
            referer=req.referer,
        )
        r["label"] = v.label or ("cookie" if v.cookie else "no-cookie")
        r["cookie_len"] = len(v.cookie or "")
        attempts.append(r)
    host = ""
    try:
        host = urlparse(req.url).hostname or ""
    except ValueError:
        pass
    return {
        "url_len": len(req.url),
        "host": host,
        "host_allowed": media.host_allowed(req.url),
        "has_query": "?" in req.url,
        "user_agent_sent": bool(req.user_agent),
        "referer_sent": bool(req.referer),
        "server": {
            "service": "notebooklm-mp3",
            "version": "0.9.1",
            "python": sys.version.split()[0],
            "platform": platform.platform(),
            "pyav": getattr(_av, "__version__", "?"),
        },
        "attempts": attempts,
        "verdict": _verdict(attempts),
    }


@app.post("/jobs", status_code=202)
def create_job(req: JobRequest, owner: str = Depends(auth)) -> dict:
    if not KEYS:
        raise HTTPException(500, "API_KEYS غير مضبوط على السيرفر")
    if len(req.files) > MAX_FILES:
        raise HTTPException(400, f"الحد الأقصى {MAX_FILES} ملفًا في المهمة")

    files: list[JobFile] = []
    for spec in req.files:
        if not media.host_allowed(spec.url):
            raise HTTPException(
                400, f"رابط غير مسموح: {spec.url[:80]} (النطاقات المسموحة هي نطاقات جوجل)")
        files.append(JobFile(
            title=spec.title or "",
            url=spec.url,
            cookie=spec.cookie,
            referer=spec.referer or req.referer,
            cookie_map=spec.cookie_map or req.cookie_map,
            bitrate=spec.bitrate or MP3_BITRATE,
            mono=MP3_MONO if spec.mono is None else spec.mono,
        ))

    job = Job(
        id=uuid.uuid4().hex[:12],
        owner=owner,
        user_agent=req.user_agent,
        zip_name=media.safe_name(req.zip_name, 0, "notebooklm-audio"),
        files=files,
        shared_cookie=req.cookie,   # كوكي مشترك (اختياري)
    )

    with LOCK:
        JOBS[job.id] = job
    QUEUE.put(job.id)
    return {"job_id": job.id, "state": job.state, "files": len(files)}



# ─────────────────── ملف الروابط النصي: «انسخ الرابط وسلّمه للسيرفر» ───────────────────
class ResolveRequest(BaseModel):
    url: str
    cookie: str | None = None
    cookie_map: dict[str, str] | None = None
    user_agent: str | None = None
    referer: str | None = None


@app.post("/resolve")
def resolve_link(req: ResolveRequest, owner: str = Depends(auth)) -> dict:
    """
    يفتح الرابط على السيرفر ويتتبّع كل تحويلة، ثم يرجّع **الرابط النهائي الكامل**.
    الهدف: أن ينسخ التطبيق/المستخدم هذا الرابط النهائي في ملف نصي، فيصبح رابطًا
    جاهزًا للسيرفر بدل الرابط الأصلي الذي تعيد جوجل توجيهه لصفحة دخول.
    لا يُنزّل الجسم كاملًا — أول 400 بايت فقط للتحقق.
    """
    if not media.host_allowed(req.url):
        raise HTTPException(400, "رابط غير مسموح: النطاقات المسموحة هي نطاقات جوجل")
    r = media.fetch_traced(
        req.url,
        cookie=req.cookie,
        cookie_map=req.cookie_map,
        user_agent=req.user_agent,
        referer=req.referer,
        sniff=400,
    )
    final_url = r.get("final_url") or req.url
    return {
        "ok": bool(r.get("ok")),
        "status": r.get("status"),
        "content_type": r.get("content_type"),
        "looks_audio": bool(r.get("looks_audio")),
        "final_url": final_url,
        "final_host": urlparse(final_url).hostname,
        "trace": media.trace_summary(r),
        "hops": r.get("hops"),
        "error": r.get("error"),
        "next_step": (
            "الرابط النهائي يعمل من السيرفر: احفظه في ملف نصي وأرسله إلى /links أو /jobs."
            if r.get("ok") else
            "الرابط النهائي لم يُنزَّل: جرّب /resolve مرة أخرى مع cookie_map لنطاقات جوجل، "
            "أو استخدم /convert كمسار احتياطي."
        ),
    }


class LinksRequest(BaseModel):
    text: str = Field(min_length=1, max_length=400_000)
    cookie: str | None = None
    cookie_map: dict[str, str] | None = None
    user_agent: str | None = None
    referer: str | None = None
    bitrate: int | None = None
    mono: bool | None = None
    zip_name: str | None = None


def _job_from_text(
    text: str,
    *,
    owner: str,
    cookie: str | None,
    cookie_map: dict[str, str] | None,
    user_agent: str | None,
    referer: str | None,
    bitrate: int | None,
    mono: bool | None,
    zip_name: str | None,
    fallback_title: str = "notebooklm",
) -> dict:
    entries = links.parse(text, default_title="")
    if not entries:
        raise HTTPException(400, "لم أجد أي رابط في النص المرسل")

    seen: set[str] = set()
    specs: list[JobFile] = []
    rejected: list[dict] = []
    for entry in entries:
        if entry.url in seen:
            continue
        seen.add(entry.url)
        if not media.host_allowed(entry.url):
            rejected.append({"url": entry.url[:120], "why": "نطاق غير مسموح"})
            continue
        specs.append(JobFile(
            title=entry.title or f"{fallback_title}-{len(specs) + 1}",
            url=entry.url,
            cookie=None,
            referer=referer,
            bitrate=int(bitrate or MP3_BITRATE),
            mono=MP3_MONO if mono is None else bool(mono),
            cookie_map=cookie_map,
        ))

    if not specs:
        raise HTTPException(400, "كل الروابط مرفوضة: " + (
            rejected[0]["why"] if rejected else "صيغة غير مفهومة"))
    if len(specs) > MAX_FILES:
        raise HTTPException(400, f"الحد الأقصى {MAX_FILES} ملفًا في المهمة")

    job = Job(
        id=uuid.uuid4().hex[:12],
        owner=owner,
        user_agent=user_agent,
        zip_name=media.safe_name(zip_name or "notebooklm-audio", 0, "notebooklm-audio"),
        files=specs,
        shared_cookie=cookie,
    )
    with LOCK:
        JOBS[job.id] = job
    QUEUE.put(job.id)
    return {
        "job_id": job.id,
        "state": job.state,
        "files": len(specs),
        "mode": "links",
        "titles": [f.title for f in specs],
        "rejected": rejected,
        "status_url": f"/jobs/{job.id}",
    }


@app.post("/links", status_code=202)
def create_from_links(req: LinksRequest, owner: str = Depends(auth)) -> dict:
    """يقبل نصًا فيه روابط (أو أزواج عنوان|رابط) ويبدأ التنزيل والتحويل مباشرة."""
    if not req.cookie and not req.cookie_map:
        pass  # الكوكيز اختيارية تمامًا: الرابط النهائي الموقّع غالبًا لا يحتاجها
    return _job_from_text(
        req.text,
        owner=owner,
        cookie=req.cookie,
        cookie_map=req.cookie_map,
        user_agent=req.user_agent,
        referer=req.referer,
        bitrate=req.bitrate,
        mono=req.mono,
        zip_name=req.zip_name,
    )


@app.post("/links/file", status_code=202)
def create_from_link_file(
    file: UploadFile = File(...),
    meta: str = Form("{}"),
    owner: str = Depends(auth),
) -> dict:
    """نفس /links لكن الملف النصي نفسه يُرفع (links.txt) — كما طلب المستخدم."""
    import json as _json

    try:
        cfg = _json.loads(meta or "{}")
    except Exception:  # noqa: BLE001
        raise HTTPException(400, "حقل meta ليس JSON صالحًا") from None

    raw = file.file.read(400_000)
    try:
        text = raw.decode("utf-8")
    except UnicodeDecodeError:
        text = raw.decode("utf-8", "replace")
    name = (file.filename or "links").rsplit(".", 1)[0][:80] or "links"
    return _job_from_text(
        text,
        owner=owner,
        cookie=cfg.get("cookie"),
        cookie_map=cfg.get("cookie_map"),
        user_agent=cfg.get("user_agent"),
        referer=cfg.get("referer"),
        bitrate=cfg.get("bitrate"),
        mono=cfg.get("mono"),
        zip_name=cfg.get("zip_name") or name,
        fallback_title=name,
    )


@app.post("/convert", status_code=202)
def convert_uploaded(
    meta: str = Form("{}"),
    files: list[UploadFile] = File(...),
    owner: str = Depends(auth),
) -> dict:
    """
    المسار المضمون: الهاتف ينزّل الملف بجلسته (التي تعمل) ويرفعه هنا،
    فنتجاوز رفض جوجل لتنزيل السيرفر المباشر. لا كوكيز تُرسل في هذه الحالة.
    """
    import json as _json

    try:
        cfg = _json.loads(meta or "{}")
    except Exception:  # noqa: BLE001
        raise HTTPException(400, "حقل meta ليس JSON صالحًا") from None

    if not files:
        raise HTTPException(400, "لا يوجد أي ملف في الطلب")
    if len(files) > MAX_FILES:
        raise HTTPException(400, f"الحد الأقصى {MAX_FILES} ملفًا في المهمة")

    titles = cfg.get("titles") or []
    bitrate = int(cfg.get("bitrate") or MP3_BITRATE)
    mono = MP3_MONO if cfg.get("mono") is None else bool(cfg.get("mono"))
    zip_name = media.safe_name(str(cfg.get("zip_name") or "notebooklm-audio"), 0)

    job = Job(
        id=uuid.uuid4().hex[:12],
        owner=owner,
        user_agent=None,
        zip_name=zip_name,
        files=[],
    )
    job_dir = os.path.join(WORK_DIR, job.id)
    os.makedirs(job_dir, exist_ok=True)

    total = 0
    try:
        for index, (upload, title) in enumerate(zip(files, list(titles) + [""] * len(files)), start=1):
            dest = os.path.join(job_dir, f"src-{index}.m4a")
            size = 0
            with open(dest, "wb") as out:
                while True:
                    chunk = upload.file.read(1 << 20)
                    if not chunk:
                        break
                    size += len(chunk)
                    total += len(chunk)
                    if size > MAX_FILE_MB * 1024 * 1024:
                        raise HTTPException(413, f"الملف {index} أكبر من الحد المسموح")
                    if total > MAX_JOB_MB * 1024 * 1024:
                        raise HTTPException(413, "مجموع الملفات أكبر من حد المهمة")
                    out.write(chunk)
            if size == 0:
                raise HTTPException(400, f"الملف {index} فارغ")
            job.files.append(JobFile(
                title=str(title or f"episode-{index}"),
                url=None,
                cookie=None,
                referer=None,
                bitrate=bitrate,
                mono=mono,
                src_path=dest,
                bytes_in=size,
            ))
    except HTTPException:
        shutil.rmtree(job_dir, ignore_errors=True)
        raise
    finally:
        for upload in files:
            try:
                upload.file.close()
            except Exception:  # noqa: BLE001
                pass

    with LOCK:
        JOBS[job.id] = job
    QUEUE.put(job.id)
    return {"job_id": job.id, "files": len(job.files), "bytes": total, "mode": "upload"}


def _get_job(job_id: str, owner: str) -> Job:
    job = JOBS.get(job_id)
    if not job or job.owner != owner:
        raise HTTPException(404, "المهمة غير موجودة أو انتهت صلاحيتها")
    return job


@app.get("/jobs")
def list_jobs(request: Request, owner: str = Depends(auth)) -> dict:
    """لوحة إدارة: كل مهام هذا المستخدم وحالتها (بدون تحميل أي ملف)."""
    base = _public_base(request)
    with LOCK:
        mine = [j for j in JOBS.values() if j.owner == owner]
    mine.sort(key=lambda j: j.created_at, reverse=True)

    rows = []
    for j in mine:
        done = sum(1 for f in j.files if f.state == "done")
        failed = sum(1 for f in j.files if f.state == "error")
        rows.append({
            "job_id": j.id,
            "state": j.state,
            "name": (j.zip_name or "notebooklm-audio"),
            "files_total": len(j.files),
            "files_done": done,
            "files_failed": failed,
            "bytes_out": sum(f.bytes_out for f in j.files),
            "created_at": int(j.created_at),
            "expires_in_sec": (max(0, int(j.expires_at - time.time())) if j.expires_at else None),
            "zip_url": f"{base}/jobs/{j.id}/zip" if j.state == "done" else None,
            "error": j.error,
            "titles": [f.title for f in j.files if f.state == "done"][:30],
            "failures": [
                {"title": f.title, "error": (f.error or "")[:300]}
                for f in j.files if f.state == "error"
            ][:30],
        })
    return {
        "count": len(rows),
        "queued": QUEUE.qsize(),
        "limits": {"max_files": MAX_FILES, "max_job_mb": MAX_JOB_MB, "ttl_minutes": TTL_MINUTES},
        "jobs": rows,
    }


@app.get("/jobs/{job_id}")
def job_status(job_id: str, request: Request, owner: str = Depends(auth)) -> dict:
    job = _get_job(job_id, owner)
    return job.public(str(request.base_url).rstrip("/"))


@app.get("/jobs/{job_id}/files/{index}")
def job_file(job_id: str, index: int, owner: str = Depends(auth)):
    job = _get_job(job_id, owner)
    done = [f for f in job.files if f.state == "done" and f.mp3_path]
    if index < 1 or index > len(done):
        raise HTTPException(404, "لا يوجد ملف بهذا الرقم")
    f = done[index - 1]
    return StreamingResponse(
        _file_iter(f.mp3_path),
        media_type="audio/mpeg",
        headers={"Content-Disposition": cd_header(f.zip_name)},
    )


@app.get("/jobs/{job_id}/zip")
def job_zip(job_id: str, t: str | None = None,
            x_api_key: str | None = Header(default=None)):
    if t and check_link("j", job_id, t):
        with LOCK:
            job = JOBS.get(job_id)
        if not job:
            raise HTTPException(404, "المهمة غير موجودة")
    else:
        job = _get_job(job_id, auth(x_api_key))
    if job.state not in ("done", "error"):
        raise HTTPException(409, "المهمة لم تنتهِ بعد")
    done = [f for f in job.files if f.state == "done" and f.mp3_path]
    if not done:
        raise HTTPException(404, "لا ملفات ناجحة في هذه المهمة")

    entries = [
        zipstream.Entry(name=f.zip_name, path=f.mp3_path, size=f.bytes_out, crc=f.crc)
        for f in done
    ]
    total = zipstream.zip_size(entries)
    headers = {
        "Content-Disposition": cd_header(f"{job.zip_name}.zip"),
        "Content-Length": str(total),
        "X-Total-Bytes": str(total),
        "X-Files": str(len(entries)),
        "Cache-Control": "no-store",
    }
    return StreamingResponse(
        zipstream.stream_zip(entries), media_type="application/zip", headers=headers
    )


@app.delete("/jobs/{job_id}")
def delete_job(job_id: str, owner: str = Depends(auth)) -> dict:
    job = _get_job(job_id, owner)
    JOBS.pop(job_id, None)
    shutil.rmtree(os.path.join(WORK_DIR, job_id), ignore_errors=True)
    return {"deleted": job_id}


def cd_header(filename: str) -> str:
    """ترويسة تنزيل تدعم الأسماء العربية (RFC 5987) مع بديل ASCII نظيف.

    المتصفحات الحديثة تستخدم filename* (الاسم العربي كما هو)؛ والعملاء القديمة
    تستخدم البديل ASCII، فنجعله بلا شرطات سفلية قدر الإمكان.
    """
    import re as _re
    from urllib.parse import quote

    stem, _, ext = filename.rpartition(".")
    if not stem:
        stem, ext = filename, ""
    ascii_stem = "".join(c for c in stem if 32 <= ord(c) < 127 and c not in '"\\')
    ascii_stem = _re.sub(r"\s+", " ", ascii_stem).strip(" .-")
    ascii_ext = "".join(c for c in ext if c.isalnum())[:5]
    if not ascii_stem:
        ascii_stem = "audio"
    ascii_fallback = f"{ascii_stem}.{ascii_ext}" if ascii_ext else ascii_stem
    return (f'attachment; filename="{ascii_fallback}"; '
            f"filename*=UTF-8''{quote(filename)}")


# ═══════════════════════ مجموعات: دفتر ملاحظات كامل ═══════════════════════
# كل حلقات الدفتر (أي عدد) في طلب واحد → التنزيل والتحويل بالتسلسل →
# الترتيب من ملف نصي مرجعي ونقل كل ملف إلى «تم الترتيب/» بفاصل 150ms →
# ZIP واحد باسم دفتر الملاحظات. لا مجلد لكل دفعة: مجلد واحد للكل.


class CollectionItem(BaseModel):
    title: str
    url: str
    cookie: str | None = None
    cookie_map: dict[str, str] | None = None
    user_agent: str | None = None
    referer: str | None = None
    bitrate: int | None = None
    mono: bool | None = None


class CollectionRequest(BaseModel):
    name: str = "notebooklm-audio"
    order_text: str = ""
    match: float | None = None
    delivery: str = "zip"          # zip | folder
    files: list[CollectionItem] = Field(default_factory=list)
    auto_mega: bool = False        # رفع تلقائي بعد انتهاء الترتيب
    mega_session: str = ""         # جلسة ميجا (تُفتح من التطبيق مرة واحدة)
    cloud: str = "mega"            # mega | telegram (0.9.1)
    tg_session: str = ""           # جلسة تيليجرام (توكن مشفّر من الهاتف)


class OrderRequest(BaseModel):
    text: str = ""
    match: float | None = None


def _own_collection(col_id: str, owner: str) -> "colls.Collection":
    col = colls.get(col_id)
    if not col or col.owner != owner:
        raise HTTPException(404, "المجموعة غير موجودة")
    return col



def _public_base(request: Request) -> str:
    """أصل الرابط كما يراه المستخدم (https دائمًا خلف الوكيل)."""
    host = request.headers.get("host") or request.url.netloc
    proto = (request.headers.get("x-forwarded-proto") or "https").split(",")[0].strip()
    return f"{proto}://{host}"


def _signed(d: dict, base: str) -> dict:
    if d.get("zip_url") and base:
        d["zip_url_signed"] = f"{base}/collections/{d['id']}/zip?t={sign_link('c', d['id'])}"
        d["link_expires_in_sec"] = LINK_TTL_SEC
    if d.get("delivery") == "folder" and base and d.get("files"):
        for f in d["files"]:
            f["url"] = (f"{base}/collections/{d['id']}/file/{f['idx']}"
                        f"?t={sign_link('f', str(d['id']) + ':' + str(f['idx']))}")
            f["url_signed"] = f["url"]
        d["link_expires_in_sec"] = LINK_TTL_SEC
    return d


@app.post("/collections", status_code=202)
def create_collection(req: CollectionRequest, owner: str = Depends(auth)) -> dict:
    if not req.files:
        raise HTTPException(400, "لا حلقات في الطلب")
    if len(req.files) > colls.MAX_ITEMS:
        raise HTTPException(400, f"الحد الأقصى {colls.MAX_ITEMS} حلقة في المجموعة")
    missing = [f.title for f in req.files if not (f.url or "").startswith(("http://", "https://"))]
    if missing:
        raise HTTPException(400, "روابط غير صالحة: " + "، ".join(missing[:5]))
    col = colls.create(
        owner, req.name, [f.model_dump() for f in req.files],
        order_text=req.order_text, match=req.match, delivery=req.delivery,
        auto_mega=req.auto_mega, mega_sid=req.mega_session,
        cloud=req.cloud, tg_sid=req.tg_session,
    )
    return {
        "collection_id": col.id,
        "count": len(col.items),
        "state": col.state,
        "match": col.match,
        "delivery": col.delivery,
    }


@app.get("/collections")
def list_collections(request: Request, owner: str = Depends(auth)) -> dict:
    base = _public_base(request)
    mine = [c for c in colls.COLS.values() if c.owner == owner]
    mine.sort(key=lambda c: c.created_at, reverse=True)
    return {
        "count": len(mine),
        "limits": {
            "max_items": colls.MAX_ITEMS,
            "max_collection_mb": colls.MAX_COLLECTION_MB,
            "ttl_minutes": colls.TTL_MINUTES,
            "move_delay_ms": int(colls.MOVE_DELAY * 1000),
            "sorted_dirname": colls.SORTED,
        },
        "collections": [_signed(c.to_dict(base=base, detail=False), base) for c in mine],
    }


@app.get("/collections/{col_id}")
def get_collection(col_id: str, request: Request, owner: str = Depends(auth)) -> dict:
    col = _own_collection(col_id, owner)
    base = _public_base(request)
    return _signed(col.to_dict(base=base, detail=True), base)


class MegaSessionReq(BaseModel):
    sealed: str = Field(min_length=0, default="")


class MegaUploadReq(BaseModel):
    session_id: str = ""
    wipe_after: bool = False
    auto: bool = False        # الجلسة تبقى للاستخدام التالي (لا نطلب كلمة السر كل مرة)


@app.get("/mega/key")
def mega_public_key(owner: str = Depends(auth)) -> dict:
    """المفتاح العام المؤقت (يُعاد توليده مع كل تشغيل ولا يُخزَّن)."""
    if not mega_up.rsa_available():
        raise HTTPException(status_code=503, detail="وحدة التشفير غير متاحة على السيرفر")
    return {"alg": "RSA-OAEP-SHA1", "pubkey": mega_up.public_pem(),
            "env_creds": mega_up.enabled()}


@app.post("/mega/session")
def mega_open_session(req: MegaSessionReq, owner: str = Depends(auth)) -> dict:
    """يفك ظرف الهاتف، يسجّل الدخول لميجا، ثم يمسح النص الصريح ويحتفظ بالجلسة فقط."""
    if not req.sealed:
        raise HTTPException(status_code=400, detail="لا يوجد ظرف مشفّر")
    if not mega_up.rsa_available():
        raise HTTPException(status_code=503, detail="وحدة التشفير غير متاحة على السيرفر")
    try:
        email, password = mega_up.open_sealed(req.sealed)
    except Exception as e:                    # noqa: BLE001
        raise HTTPException(status_code=400,
                            detail="تعذّر فك التشفير: " + type(e).__name__)
    if not email or not password:
        raise HTTPException(status_code=400, detail="البريد أو كلمة المرور ناقص")
    try:
        sid = mega_up.create_session(email, password)
    except Exception as e:                    # noqa: BLE001
        msg = f"{type(e).__name__}: {e}"
        low = msg.lower()
        if ("jsondecode" in low or "connection" in low or "timeout" in low
                or "timed out" in low or "getaddrinfo" in low):
            msg = "تعذّر الوصول إلى ميجا من السيرفر (شبكة/حجب) — جرّب لاحقًا"
        elif "password" in low or "credential" in low or "login" in low or "auth" in low:
            msg = "بيانات دخول ميجا غير صحيحة"
        del password
        raise HTTPException(status_code=401, detail=msg[:200])
    del password                              # لا نُبقي كلمة المرور في أي مرجع
    return {"session_id": sid, "email": mega_up.mask(email),
            "expires_in_sec": mega_up.SESS_TTL,
            "note": "البيانات لم تُخزَّن على السيرفر — الجلسة فقط"}


@app.delete("/mega/session/{sid}")
def mega_close_session(sid: str, owner: str = Depends(auth)) -> dict:
    return {"closed": mega_up.drop_session(sid)}


# ─────────────── طابور الرفع إلى MEGA: متوالٍ + تلقائي (0.9.0) ───────────────
MEGA_Q: list[dict[str, Any]] = []
MEGA_Q_LOCK = threading.Lock()
MEGA_WAKE = threading.Event()
MEGA_STAT: dict[str, Any] = {"current": "", "current_name": "", "processed": 0,
                             "errors": 0, "last_error": "", "started": time.time()}


def _mega_pos_locked(col_id: str) -> int:
    for i, it in enumerate(MEGA_Q, start=1):
        if it["col"] == col_id:
            return i
    return 0


def _mega_enqueue(col: "colls.Collection", sid: str, wipe: bool = False,
                  auto: bool = False, backend: str = "mega") -> dict:
    """يضع المجموعة في طابور الرفع. الترتيب = ترتيب الدخول ⇒ تتابع صارم."""
    with MEGA_Q_LOCK:
        if any(it["col"] == col.id for it in MEGA_Q):
            return {"state": "queued", "position": _mega_pos_locked(col.id)}
        if col.mega_state in ("uploading", "link", "done"):
            return {"state": col.mega_state, "link": col.mega_link,
                    "error": col.mega_error}
        if backend == "telegram":
            col.tg_state = "queued"
            col.tg_error = ""
            col.tg_percent = 0
        else:
            col.mega_state = "queued"
            col.mega_error = ""
            col.mega_percent = 0
        col.mega_files_done = 0
        col.mega_files_total = len(col.order)
        col.note(("☁️ رفع تلقائي إلى MEGA — في الطابور…" if auto
                  else "☁️ في طابور الرفع إلى MEGA…"))
        if backend != "telegram":
            col.mega_state = "queued"
        MEGA_Q.append({"col": col.id, "sid": sid, "wipe": wipe, "auto": auto,
                       "backend": backend, "at": time.time()})
        pos = len(MEGA_Q)
    MEGA_WAKE.set()
    return {"state": "queued", "position": pos}


def _mega_run(col: "colls.Collection", sid: str, wipe: bool = False) -> None:
    """ينفّذ الرفع فعليًا (كان داخل خيط مستقل — الآن داخل المحرّك المتوالٍ)."""
    sess = mega_up.session(sid) if sid else None
    zip_path = os.path.join(col.dir, "_mega_upload.zip")
    remote = (col.name or "notebooklm-audio") + ".zip"
    col.mega_state = "uploading"
    col.mega_percent = 0
    col.mega_files_done = 0
    col.mega_files_total = len(col.order)
    col.mega_current = ""
    col.mega_skipped = 0
    col.mega_folder = col.name or ""
    col.mega_sub = ""
    col.mega_error = ""
    col.mega_link = ""
    col.note("☁️ بدء الرفع إلى MEGA…")

    def on_state(**kw: Any) -> None:
        st = kw.get("state")
        if st:
            col.mega_state = st
        if kw.get("percent") is not None:
            col.mega_percent = int(kw.get("percent") or 0)
        if kw.get("files_done") is not None:
            col.mega_files_done = int(kw.get("files_done") or 0)
        if kw.get("files_total") is not None:
            col.mega_files_total = int(kw.get("files_total") or 0)
        if kw.get("current") is not None:
            col.mega_current = str(kw.get("current") or "")
        if kw.get("skipped") is not None:
            col.mega_skipped = int(kw.get("skipped") or 0)
        if kw.get("folder"):
            col.mega_folder = str(kw["folder"])
        if kw.get("subfolder"):
            col.mega_sub = str(kw["subfolder"])
        if kw.get("link"):
            col.mega_link = str(kw["link"])
        if kw.get("folder_link"):
            col.mega_folder_link = str(kw["folder_link"])
        if kw.get("error"):
            col.mega_error = str(kw["error"])
        if kw.get("seconds"):
            col.mega_seconds = float(kw["seconds"])
        if st == "done":
            col.mega_percent = 100
            col.mega_current = ""
            col.note(f"☁️ تم الرفع إلى MEGA: /{col.mega_folder}/{col.mega_sub or ''}"
                     f" — {col.mega_files_done} ملف"
                     + (f" (تُخطّي {col.mega_skipped} موجود)" if col.mega_skipped else ""))
        elif st == "error":
            col.note("❌ فشل الرفع إلى MEGA: " + col.mega_error[:120])
        if st in ("done", "error"):
            try:
                if os.path.exists(zip_path):
                    os.remove(zip_path)
            except Exception:
                pass

    try:
        if col.delivery == "folder":
            payload = [{"path": e["path"], "name": e["name"]}
                       for e in col.order if os.path.exists(e.get("path", ""))]
            col.mega_bytes = sum(int(e.get("size") or 0) for e in col.order)
            col.note(f"☁️ رفع مجلد «{col.name}» ({len(payload)} ملف) إلى MEGA…")
            cli = sess["client"] if sess is not None else None
            mega_up.upload_folder(cli, payload, col.name, on_state)
            if sess is not None and wipe:
                mega_up.drop_session(sid)
                col.note("🔒 مُسحت بيانات الجلسة من ذاكرة السيرفر")
            return
        files, size = mega_up.build_zip(zip_path, col.order)
        col.mega_bytes = size
        if files == 0:
            raise RuntimeError("لا ملفات لبنائها في ZIP الرفع")
        col.note(f"☁️ نسخة الرفع جاهزة ({size/1048576:.1f} م.ب) — جارٍ التشفير والرفع")
        if sess is not None:
            mega_up.upload_with(sess["client"], zip_path, remote, on_state)
            sess["uploads"] = int(sess.get("uploads", 0)) + 1
            if wipe and sess.get("uploads", 0) >= 1:
                mega_up.drop_session(sid)
                col.note("🔒 مُسحت بيانات الجلسة من ذاكرة السيرفر")
        else:
            mega_up.upload(zip_path, remote, on_state)
    except Exception as e:                    # noqa: BLE001
        col.mega_state = "error"
        col.mega_error = f"{type(e).__name__}: {e}"[:300]
        col.note("❌ فشل الرفع إلى MEGA: " + col.mega_error[:120])
        try:
            if os.path.exists(zip_path):
                os.remove(zip_path)
        except Exception:
            pass


def _tg_run(col: "colls.Collection", sid: str) -> None:
    """يرسل ملفات المجموعة المرتّبة إلى محادثة تيليجرام (بالترتيب)."""
    sess = tg_up.session(sid) if sid else None
    token = (sess or {}).get("token") or tg_up.TOKEN_ENV
    chat = (sess or {}).get("chat") or tg_up.CHAT_ENV
    if not token or not chat:
        col.tg_state = "error"
        col.tg_error = "لا جلسة تيليجرام صالحة — اربط البوت من التطبيق أولًا"
        col.note("❌ " + col.tg_error)
        return
    col.tg_state = "uploading"
    col.tg_percent = 0
    col.tg_error = ""
    col.tg_messages = []
    col.tg_bytes = 0
    col.tg_files_done = 0
    col.tg_files_total = len(col.order)
    col.tg_bot = (sess or {}).get("bot") or ""
    t0 = time.time()
    zip_path = os.path.join(col.dir, "_tg_upload.zip")
    try:
        if col.delivery == "folder":
            payload = [(e["path"], e["name"]) for e in col.order
                       if e.get("path") and os.path.exists(e["path"])]
        else:
            files, size = mega_up.build_zip(zip_path, col.order)
            if files == 0:
                raise RuntimeError("لا ملفات لإرسالها")
            if size > tg_up.MAX_MB * 1048576:
                raise RuntimeError(
                    f"نسخة ZIP حجمها {size/1048576:.1f} م.ب وأقصى حد لتيليجرام "
                    f"{tg_up.MAX_MB} م.ب — بدّل طريقة التسليم إلى «مجلد» ليُرسل ملفًا ملفًا.")
            payload = [(zip_path, (col.name or "notebooklm-audio") + ".zip")]
        if not payload:
            raise RuntimeError("لا ملفات موجودة على القرص لإرسالها")
        total = max(1, len(payload))
        col.note(f"📨 إرسال {len(payload)} ملف إلى تيليجرام…"
                 + (f" ({col.tg_bot})" if col.tg_bot else ""))
        for i, (path, name) in enumerate(payload, start=1):
            col.tg_current = name
            r = tg_up.send_document(token, chat, path, name,
                                    caption=f"{col.name} · {i}/{len(payload)}")
            col.tg_messages.append({"name": name, "file_id": r["file_id"],
                                    "message_id": r["message_id"],
                                    "size": r["size"], "order": i})
            col.tg_bytes += int(r["size"] or 0)
            col.tg_files_done = i
            col.tg_percent = int(i * 100 / total)
            if sess:
                sess["messages"] = int(sess.get("messages", 0)) + 1
        col.tg_state = "done"
        col.tg_percent = 100
        col.tg_current = ""
        col.tg_seconds = round(time.time() - t0, 1)
        col.note(f"📨 تم إرسال {len(payload)} ملف إلى تيليجرام"
                 + (f" (البوت @{col.tg_bot})" if col.tg_bot else ""))
    except Exception as e:                    # noqa: BLE001
        col.tg_state = "error"
        col.tg_error = f"{type(e).__name__}: {e}"[:300]
        col.note("❌ فشل الإرسال إلى تيليجرام: " + col.tg_error[:140])
    finally:
        try:
            if os.path.exists(zip_path):
                os.remove(zip_path)
        except Exception:
            pass


def _mega_engine() -> None:
    """خيط واحد: مجموعة واحدة ترفع في أي لحظة ⇒ لا تعارض ولا تزامن."""
    while True:
        MEGA_WAKE.wait(1.0)
        MEGA_WAKE.clear()
        while True:
            with MEGA_Q_LOCK:
                if not MEGA_Q:
                    MEGA_STAT["current"] = ""
                    MEGA_STAT["current_name"] = ""
                    break
                item = MEGA_Q[0]
                col = colls.get(item["col"])
                if col is None:
                    MEGA_Q.pop(0)
                    continue
                MEGA_STAT["current"] = col.id
                MEGA_STAT["current_name"] = col.name
            try:
                if (item.get("backend") or "mega") == "telegram":
                    _tg_run(col, item.get("sid") or "")
                else:
                    _mega_run(col, item.get("sid") or "", bool(item.get("wipe")))
                MEGA_STAT["processed"] = int(MEGA_STAT["processed"]) + 1
            except Exception as exc:               # noqa: BLE001
                MEGA_STAT["errors"] = int(MEGA_STAT["errors"]) + 1
                MEGA_STAT["last_error"] = f"{type(exc).__name__}: {exc}"[:200]
            finally:
                with MEGA_Q_LOCK:
                    if MEGA_Q and MEGA_Q[0] is item:
                        MEGA_Q.pop(0)
                    elif item in MEGA_Q:
                        MEGA_Q.remove(item)
                MEGA_STAT["current"] = ""
                MEGA_STAT["current_name"] = ""


def _auto_mega(col: "colls.Collection") -> None:
    """يُنادى تلقائيًا عند جاهزية المجموعة — يرفع إلى السحابة المختارة بلا تطبيق."""
    if not getattr(col, "auto_mega", False):
        return
    if not col.order:
        return
    if getattr(col, "cloud", "mega") == "telegram":
        sid_tg = getattr(col, "tg_sid", "") or ""
        if tg_up.session(sid_tg) is None and not tg_up.enabled():
            col.note("⚠️ الرفع التلقائي متوقف: لا جلسة تيليجرام — اربط البوت من التطبيق.")
            return
        _mega_enqueue(col, sid_tg, False, auto=True, backend="telegram")
        return
    sid = getattr(col, "mega_sid", "") or ""
    if not sid and not mega_up.enabled() and not mega_up.FAKE:
        col.note("⚠️ الرفع التلقائي متوقف: لا جلسة MEGA — افتح الرفع من التطبيق مرة واحدة.")
        return
    if sid and mega_up.session(sid) is None and not mega_up.enabled() and not mega_up.FAKE:
        col.note("⚠️ الرفع التلقائي متوقف: جلسة MEGA انتهت — أعد تسجيل الدخول من التطبيق.")
        return
    _mega_enqueue(col, sid, False, auto=True)


@app.post("/collections/{col_id}/mega", status_code=202)
def mega_upload_collection(col_id: str, owner: str = Depends(auth),
                           req: MegaUploadReq | None = None) -> dict:
    """يضع المجموعة في طابور الرفع إلى حساب MEGA (متوالٍ بالترتيب)."""
    col = _own_collection(col_id, owner)
    sid = (req.session_id if req else "") or getattr(col, "mega_sid", "") or ""
    sess = mega_up.session(sid) if sid else None
    if sess is None and not mega_up.enabled() and not mega_up.FAKE:
        raise HTTPException(status_code=400, detail=(
            "لا توجد بيانات ميجا: افتح رفع ميجا من التطبيق وأدخل بريدك مرة واحدة "
            "(تُرسَل مشفّرة ولا تُخزَّن)، أو اضبط MEGA_EMAIL/MEGA_PASSWORD."))
    if col.state not in ("done", "partial") or not col.order:
        raise HTTPException(status_code=409, detail=(
            "المجموعة لم تنتهِ بعد — سيبدأ رفعها تلقائيًا بمجرد انتهاء الترتيب."))
    total = sum(int(e.get("size") or 0) for e in col.order) or 1
    free = colls._disk_free_mb()
    if free < (total / 1048576.0) * 1.25 + 40:
        raise HTTPException(status_code=507, detail=(
            f"مساحة القرص لا تكفي لبناء نسخة الرفع ({free:.0f}MB متاح). "
            "نزّل الـZIP مباشرة بدل الرفع."))
    if sid:
        col.mega_sid = sid
    return _mega_enqueue(col, sid, bool(req.wipe_after) if req else False)


@app.post("/collections/{col_id}/mega/auto")
def mega_auto(col_id: str, owner: str = Depends(auth),
              req: MegaUploadReq | None = None) -> dict:
    """تشغيل/إيقاف الرفع التلقائي لمجموعة (يُستخدم قبل انتهاء التنزيل)."""
    col = _own_collection(col_id, owner)
    sid = (req.session_id if req else "") or ""
    if sid:
        col.mega_sid = sid
    return {"auto_mega": bool(getattr(col, "auto_mega", False)),
            "armed": bool(col.mega_sid or mega_up.enabled() or mega_up.FAKE)}


# ─────────────── Telegram Bot كسحابة ثانية (0.9.1) ───────────────
@app.get("/tg/key")
def tg_public_key(owner: str = Depends(auth)) -> dict:
    if not tg_up.rsa_available():
        raise HTTPException(503, "وحدة التشفير غير متاحة على السيرفر")
    return {"alg": "RSA-OAEP-SHA1", "pubkey": tg_up.public_pem(),
            "max_mb": tg_up.MAX_MB, "env_bot": tg_up.enabled()}


@app.post("/tg/session")
def tg_open_session(req: MegaSessionReq, owner: str = Depends(auth)) -> dict:
    """يفك ظرف الهاتف: «token\nchat_id» ويختبرهما فعليًا ثم يفتح جلسة في الذاكرة."""
    try:
        token, chat = tg_up.open_sealed(req.sealed)
    except Exception:
        raise HTTPException(400, "تعذّر فك الظرف المشفّر — أعد المحاولة من التطبيق")
    try:
        sid = tg_up.create_session(token, chat)
    except Exception as e:                    # noqa: BLE001
        raise HTTPException(400, f"{e}")
    s0 = tg_up.session(sid) or {}
    return {"session_id": sid, "bot": s0.get("bot", ""), "chat": chat,
            "chat_type": (s0.get("chat_info") or {}).get("type", ""),
            "max_mb": tg_up.MAX_MB,
            "expires_in_sec": tg_up.SESS_TTL}


@app.get("/tg/session/{sid}")
def tg_session_state(sid: str, owner: str = Depends(auth)) -> dict:
    return {**tg_up.session_info(sid), "max_mb": tg_up.MAX_MB, "env": tg_up.env_info()}


@app.delete("/tg/session/{sid}")
def tg_close_session(sid: str, owner: str = Depends(auth)) -> dict:
    return {"closed": tg_up.drop_session(sid)}


@app.post("/collections/{col_id}/tg", status_code=202)
def tg_upload_collection(col_id: str, owner: str = Depends(auth),
                         req: MegaUploadReq | None = None) -> dict:
    """يضع المجموعة في طابور الإرسال إلى تيليجرام (نفس الطابور المتوالٍ)."""
    col = _own_collection(col_id, owner)
    sid = (req.session_id if req else "") or getattr(col, "tg_sid", "") or ""
    if tg_up.session(sid) is None and not tg_up.enabled():
        raise HTTPException(400, detail=(
            "لا جلسة تيليجرام: اربط البوت من التطبيق (توكن BotFather + معرّف المحادثة) "
            "أو اضبط TELEGRAM_BOT_TOKEN/TELEGRAM_CHAT_ID على السيرفر."))
    if col.state not in ("done", "partial") or not col.order:
        raise HTTPException(409, "المجموعة لم تنتهِ بعد — سيُرسل تلقائيًا بعد انتهاء الترتيب.")
    if sid:
        col.tg_sid = sid
    return _mega_enqueue(col, sid, False, backend="telegram")


class CloudRequest(BaseModel):
    cloud: str = "mega"          # mega | telegram
    session_id: str = ""         # جلسة السحابة المختارة


@app.post("/collections/{col_id}/cloud")
def set_cloud(col_id: str, req: CloudRequest, owner: str = Depends(auth)) -> dict:
    """يغيّر سحابة المجموعة بعد إنشائها (المستخدم يختار في خطوة السحابة)."""
    col = _own_collection(col_id, owner)
    col.cloud = "telegram" if str(req.cloud).lower() in ("telegram", "tg", "bot") else "mega"
    if req.session_id:
        if col.cloud == "telegram":
            col.tg_sid = req.session_id
        else:
            col.mega_sid = req.session_id
    if (col.state in ("done", "partial") and col.order
            and getattr(col, "auto_mega", False)):
        with MEGA_Q_LOCK:
            queued = any(it["col"] == col.id for it in MEGA_Q)
        if not queued:
            if col.cloud == "telegram" and col.tg_state not in ("done", "uploading"):
                _mega_enqueue(col, col.tg_sid, False, auto=True, backend="telegram")
            elif col.cloud == "mega" and col.mega_state not in ("done", "uploading"):
                _mega_enqueue(col, col.mega_sid, False, auto=True)
    return {"cloud": col.cloud, "tg_session": bool(col.tg_sid),
            "mega_session": bool(col.mega_sid)}


@app.get("/collections/{col_id}/tg")
def tg_state(col_id: str, request: Request, owner: str = Depends(auth)) -> dict:
    col = _own_collection(col_id, owner)
    base = _public_base(request)
    links = []
    for m in col.tg_messages:
        idx = int(m.get("order") or 0)
        links.append({
            "name": m.get("name"),
            "size": m.get("size"),
            "url": f"{base}/tg/file/{col.id}/{idx}?t={sign_link('g', col.id + ':' + str(idx))}",
        })
    return {"state": col.tg_state, "percent": col.tg_percent,
            "files_done": col.tg_files_done, "files_total": col.tg_files_total,
            "current": col.tg_current, "error": col.tg_error,
            "bytes": col.tg_bytes, "seconds": col.tg_seconds,
            "bot": col.tg_bot, "links": links,
            "max_mb": tg_up.MAX_MB,
            "enabled": tg_up.enabled(),
            "session_alive": tg_up.live_sessions() > 0}


@app.get("/tg/file/{col_id}/{idx}")
def tg_file(col_id: str, idx: int, t: str | None = None,
            x_api_key: str | None = Header(default=None)):
    """ينزّل ملفًا أُرسل إلى تيليجرام عبر السيرفر (رابط موقّع أو مفتاح API)."""
    if t and check_link("g", col_id + ":" + str(idx), t):
        col = colls.get(col_id)
        if not col:
            raise HTTPException(404, "المجموعة غير موجودة")
    else:
        col = _own_collection(col_id, auth(x_api_key))
    msg = next((m for m in col.tg_messages if int(m.get("order") or 0) == idx), None)
    if not msg or not msg.get("file_id"):
        raise HTTPException(404, "الملف غير موجود في تيليجرام")
    sess = tg_up.session(col.tg_sid) if col.tg_sid else None
    token = (sess or {}).get("token") or tg_up.TOKEN_ENV
    if not token:
        raise HTTPException(409, "جلسة تيليجرام انتهت — أعد ربط البوت من التطبيق")
    try:
        stream, size = tg_up.download_stream(token, msg["file_id"])
    except Exception as e:                    # noqa: BLE001
        raise HTTPException(502, f"تعذّر التنزيل من تيليجرام: {e}")
    name = msg.get("name") or "file"
    headers = {"Content-Disposition": cd_header(name), "Cache-Control": "no-store"}
    if size:
        headers["Content-Length"] = str(size)
    return StreamingResponse(stream, media_type="application/octet-stream", headers=headers)


@app.get("/queue")
def global_queue(owner: str = Depends(auth)) -> dict:
    """حالة الطابور العامة: ما يُعالَج الآن، وما يليه — لكل المساحات."""
    with MEGA_Q_LOCK:
        up_order = [it["col"] for it in MEGA_Q]
        current = MEGA_STAT["current"]
    cols = [c for c in colls.COLS.values() if c.owner == owner]
    cols.sort(key=lambda c: c.created_at)
    items = []
    for c in cols:
        phase = "queued"
        if c.state in ("pending", "running", "waiting_order"):
            phase = "processing"
        elif c.state == "sorting":
            phase = "sorting"
        elif c.state in ("done", "partial"):
            st_cloud = c.tg_state if getattr(c, "cloud", "mega") == "telegram" else c.mega_state
            if st_cloud in ("queued", "uploading", "link", "done", "error"):
                phase = ("uploading" if st_cloud in ("uploading", "link")
                         else ("uploaded" if st_cloud == "done"
                               else ("upload_error" if st_cloud == "error" else "upload_queued")))
            else:
                phase = "ready"
        pos = up_order.index(c.id) + 1 if c.id in up_order else 0
        items.append({
            "id": c.id, "name": c.name, "phase": phase, "state": c.state,
            "total": len(c.items),
            "done": sum(1 for i in c.items if i.state == "done"),
            "errors": sum(1 for i in c.items if i.state == "error"),
            "queue_position": pos,
            "is_current": c.id == current,
            "auto_mega": bool(getattr(c, "auto_mega", False)),
            "cloud": getattr(c, "cloud", "mega"),
            "tg_state": c.tg_state, "tg_percent": c.tg_percent,
            "tg_files_done": c.tg_files_done, "tg_files_total": c.tg_files_total,
            "mega_state": c.mega_state, "mega_percent": c.mega_percent,
            "mega_files_done": c.mega_files_done,
            "mega_files_total": c.mega_files_total,
            "mega_current": c.mega_current, "mega_error": c.mega_error,
            "link": c.mega_link, "folder_link": c.mega_folder_link,
            "created": c.created_at,
        })
    busy = sum(1 for i in items if i["phase"] in ("processing", "sorting", "uploading"))
    return {
        "engine": {
            "current": current,
            "current_name": MEGA_STAT["current_name"],
            "waiting": max(0, len(up_order) - (1 if current else 0)),
            "processed": MEGA_STAT["processed"],
            "errors": MEGA_STAT["errors"],
            "last_error": MEGA_STAT["last_error"],
            "busy": busy,
            "sequential": True,
        },
        "items": items,
        "total": len(items),
    }


@app.get("/collections/{col_id}/mega")
def mega_state(col_id: str, owner: str = Depends(auth)) -> dict:
    col = _own_collection(col_id, owner)
    if not col.mega_files_total:
        col.mega_files_total = len(col.order)
    if not col.mega_folder:
        col.mega_folder = col.name or ""
    if not col.mega_sub:
        col.mega_sub = mega_up.SORTED_NAME if col.delivery == "folder" else ""
    return {"enabled": mega_up.enabled(), "state": col.mega_state,
            "link": col.mega_link, "folder_link": col.mega_folder_link,
            "error": col.mega_error, "percent": col.mega_percent,
            "files_done": col.mega_files_done, "files_total": col.mega_files_total,
            "current": col.mega_current, "skipped": col.mega_skipped,
            "folder": col.mega_folder, "subfolder": col.mega_sub,
            "bytes": col.mega_bytes, "seconds": col.mega_seconds,
            "session_alive": mega_up.live_sessions() > 0}


@app.post("/collections/{col_id}/order")
def set_order(col_id: str, req: OrderRequest, owner: str = Depends(auth)) -> dict:
    col = _own_collection(col_id, owner)
    col.order_text = req.text or ""
    if req.match is not None:
        col.match = min(0.80, max(0.50, float(req.match)))
    col.note(f"📄 ملف ترتيب: {len(colls.order_lines(col.order_text))} سطر · "
             f"مطابقة {int(col.match * 100)}%")
    if col.counts()["pending"] == 0 and col.counts()["downloading"] == 0 \
            and col.counts()["converting"] == 0:
        colls.start_finalize(col)
        col.finished_at = None
    return col.to_dict()


@app.post("/collections/{col_id}/finalize")
def finalize_collection(col_id: str, owner: str = Depends(auth)) -> dict:
    col = _own_collection(col_id, owner)
    if col.state == "running":
        raise HTTPException(409, "المجموعة لا تزال قيد التنزيل/التحويل")
    colls.start_finalize(col)
    return {"started": True, "state": col.state}


@app.post("/collections/{col_id}/retry")
def retry_collection(col_id: str, owner: str = Depends(auth)) -> dict:
    col = _own_collection(col_id, owner)
    n = colls.retry(col)
    return {"retried": n, "state": col.state}


@app.get("/collections/{col_id}/probe")
def probe_collection_item(col_id: str, idx: int = 0, owner: str = Depends(auth)) -> dict:
    """تشخيص حلقة واحدة: ماذا يرد جوجل بالضبط؟ (كود الحالة، الكوكيز، التحويلات)"""
    col = _own_collection(col_id, owner)
    if idx < 0 or idx >= len(col.items):
        raise HTTPException(404, "رقم الحلقة غير موجود")
    it = col.items[idx]
    r = media.probe(
        it.url, cookie=it.cookie, cookie_map=it.cookie_map,
        user_agent=it.user_agent, referer=it.referer,
    )
    r.update({
        "title": it.title,
        "state": it.state,
        "attempts": it.attempts,
        "saved_error": it.error,
        "has_cookie": bool(it.cookie or it.cookie_map),
        "cookie_domains": sorted((it.cookie_map or {}).keys()),
    })
    return r


@app.get("/collections/{col_id}/zip")
def collection_zip(col_id: str, t: str | None = None,
                   range_header: str | None = Header(default=None, alias="Range"),
                   x_api_key: str | None = Header(default=None)):
    if t and check_link("c", col_id, t):
        col = colls.get(col_id)
        if not col:
            raise HTTPException(404, "المجموعة غير موجودة")
    else:
        col = _own_collection(col_id, auth(x_api_key))
    if col.state not in ("done", "partial"):
        raise HTTPException(409, "المجموعة لم تنتهِ بعد")
    entries, total = colls.zip_entries(col)
    if not entries:
        raise HTTPException(404, "لا ملفات في هذه المجموعة")
    headers = {
        "Content-Disposition": cd_header(f"{col.name}.zip"),
        "Content-Length": str(total),
        "X-Total-Bytes": str(total),
        "X-Files": str(len(entries)),
        "Cache-Control": "no-store",
        "Accept-Ranges": "bytes",
    }
    # دعم الاستئناف: Range: bytes=START-  → 206 مع تخطي START بايت من البث
    start = 0
    if range_header:
        try:
            part = range_header.strip().lower().replace("bytes=", "")
            first = part.split(",")[0].split("-")[0].strip()
            if first:
                start = max(0, min(int(first), total))
        except Exception:
            start = 0
    if start > 0:
        body = _skip_bytes(colls.zipstream.stream_zip(entries), start)
        headers["Content-Length"] = str(total - start)
        headers["Content-Range"] = f"bytes {start}-{total - 1}/{total}"
        return StreamingResponse(body, status_code=206,
                                 media_type="application/zip", headers=headers)
    return StreamingResponse(colls.zipstream.stream_zip(entries),
                             media_type="application/zip", headers=headers)


def _skip_bytes(gen, skip: int):
    """يتخطى أول `skip` بايت من مولد البث (لا يقرأ الملفات من جديد)."""
    dropped = 0
    for chunk in gen:
        if dropped >= skip:
            yield chunk
            continue
        need = skip - dropped
        if len(chunk) <= need:
            dropped += len(chunk)
            continue
        dropped = skip
        yield chunk[need:]


@app.get("/collections/{col_id}/file/{idx}")
def collection_file(col_id: str, idx: int, t: str | None = None,
                    range_header: str | None = Header(default=None, alias="Range"),
                    x_api_key: str | None = Header(default=None)):
    """تنزيل ملف واحد من مجلد التسليم (يدعم الاستئناف)."""
    authed = False
    if t and check_link("f", f"{col_id}:{idx}", t):
        authed = True
    else:
        try:
            owner = auth(x_api_key)
            col0 = colls.get(col_id)
            if col0 is not None and col0.owner == owner:
                authed = True
        except HTTPException:
            authed = False
    if not authed:
        raise HTTPException(401, "غير مصرّح")

    col = colls.get(col_id)
    if col is None:
        raise HTTPException(404, "المجموعة غير موجودة")
    if col.delivery != "folder":
        raise HTTPException(409, "هذه المجموعة مهيّأة كملف ZIP")
    if col.state not in ("done", "partial"):
        raise HTTPException(409, "المجموعة لم تنتهِ بعد")
    entry = next((e for e in col.order if int(e.get("idx", -1)) == idx), None)
    if entry is None:
        # idx هنا ترتيب العنصر الأصلي — نبحث أيضًا بالفهرس داخل col.order
        if 0 <= idx < len(col.order):
            entry = col.order[idx]
    if entry is None or not os.path.exists(entry["path"]):
        raise HTTPException(404, "الملف غير موجود")
    path = entry["path"]
    size = os.path.getsize(path)
    start, end = 0, max(0, size - 1)
    partial = bool(range_header)
    if range_header:
        try:
            part = range_header.strip().lower().replace("bytes=", "").split(",")[0]
            a, _, b = part.partition("-")
            if a.strip():
                start = max(0, min(int(a), max(0, size - 1)))
            if b.strip():
                end = min(int(b), max(0, size - 1))
            if end < start:
                end = max(0, size - 1)
        except Exception:
            start, end = 0, max(0, size - 1)
    if size == 0:
        return StreamingResponse(iter(()), status_code=200,
                                 media_type="audio/mpeg",
                                 headers={"Content-Length": "0",
                                          "X-Total-Bytes": "0"})
    length = end - start + 1
    headers = {"Content-Disposition": cd_header(entry["name"]),
               "Content-Length": str(length),
               "Accept-Ranges": "bytes",
               "Cache-Control": "no-store",
               "X-Total-Bytes": str(size)}
    if partial:
        headers["Content-Range"] = f"bytes {start}-{end}/{size}"
    return StreamingResponse(
        _file_range(path, start, end), status_code=206 if partial else 200,
        media_type="audio/mpeg", headers=headers)


def _file_range(path: str, start: int, end: int | None = None,
                chunk: int = 1 << 16):
    with open(path, "rb") as fh:
        if start:
            fh.seek(start)
        left = None if end is None else end - start + 1
        while True:
            want = chunk if left is None else min(chunk, left)
            if want <= 0:
                break
            data = fh.read(want)
            if not data:
                break
            if left is not None:
                left -= len(data)
            yield data


@app.delete("/collections/{col_id}")
def delete_collection(col_id: str, owner: str = Depends(auth)) -> dict:
    col = _own_collection(col_id, owner)
    colls.delete(col.id)
    return {"deleted": col.id}




def _file_iter(path: str, chunk: int = 1 << 16):
    with open(path, "rb") as fh:
        while True:
            block = fh.read(chunk)
            if not block:
                break
            yield block


@app.exception_handler(media.MediaError)
def _media_error(_request, exc: media.MediaError):  # pragma: no cover
    return JSONResponse(status_code=502, content={"detail": str(exc)})


if __name__ == "__main__":
    import uvicorn

    uvicorn.run(app, host="0.0.0.0", port=int(os.environ.get("PORT", "8000")))
