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
import shutil
import threading
import time
import uuid
from dataclasses import dataclass, field
from typing import Any

from fastapi import Depends, FastAPI, Header, HTTPException, Request
from fastapi.responses import JSONResponse, StreamingResponse
from pydantic import BaseModel, Field

import media
import zipstream

# ─────────────────────────────── التطبيق ───────────────────────────────
app = FastAPI(title="NotebookLM → MP3", version="0.1.0")

# ─────────────────────────────── الإعدادات ───────────────────────────────
WORK_DIR = os.environ.get("WORK_DIR", "/tmp/nlm")
TTL_MINUTES = int(os.environ.get("TTL_MINUTES", "45"))
MP3_BITRATE = int(os.environ.get("MP3_BITRATE", "96000"))
MP3_MONO = os.environ.get("MP3_MONO", "1") not in ("0", "false", "False", "")
MAX_FILES = int(os.environ.get("MAX_FILES", "30"))
MAX_FILE_MB = int(os.environ.get("MAX_FILE_MB", "400"))
MAX_JOB_MB = int(os.environ.get("MAX_JOB_MB", "800"))
DEBUG = os.environ.get("NLM_DEBUG", "0") == "1"


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
    cookie: str | None = None          # اختياري — يُستخدم لحظيًا فقط
    referer: str | None = None
    bitrate: int | None = None
    mono: bool | None = None


class JobRequest(BaseModel):
    files: list[FileSpec] = Field(min_length=1)
    cookie: str | None = None          # كوكي مشترك لكل الملفات
    user_agent: str | None = None
    zip_name: str = "notebooklm-audio"


class ProbeRequest(BaseModel):
    url: str
    cookie: str | None = None
    user_agent: str | None = None
    referer: str | None = None


# ─────────────────────────────── حالة المهام ───────────────────────────────
@dataclass
class JobFile:
    title: str
    url: str
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
                    "title": f.title,
                    "state": f.state,
                    "bytes_in": f.bytes_in,
                    "bytes_out": f.bytes_out,
                    "duration_sec": f.duration_sec,
                    "error": f.error,
                }
                for f in self.files
            ],
            "total_bytes_out": sum(f.bytes_out for f in self.files),
            "zip_url": f"{base}/jobs/{self.id}/zip" if self.state == "done" else None,
            "expires_in_sec": (
                max(0, int(self.expires_at - time.time())) if self.expires_at else None
            ),
        }


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
    used = 0

    try:
        for index, f in enumerate(job.files, start=1):
            src = os.path.join(job_dir, f"src-{index}.m4a")
            name = media.safe_name(f.title, index)
            dst = os.path.join(job_dir, f"{index:02d}-{name}.mp3")
            try:
                f.state = "downloading"
                used += media.download(
                    f.url,
                    src,
                    cookie=f.cookie or job.shared_cookie,
                    user_agent=job.user_agent,
                    referer=f.referer,
                    max_bytes=min(MAX_FILE_MB * 1024 * 1024, budget - used),
                    on_bytes=lambda n, _f=f: setattr(_f, "bytes_in", n),
                )
                f.state = "converting"
                info = media.m4a_to_mp3(src, dst, bitrate=f.bitrate, mono=f.mono)
                f.duration_sec = info["duration_sec"]
                f.bytes_out = info["bytes"]
                f.mp3_path = dst
                f.zip_name = os.path.basename(dst)
                f.crc, size = zipstream.crc_of(dst)
                f.bytes_out = size
                f.state = "done"
            except Exception as exc:  # noqa: BLE001
                f.state = "error"
                f.error = f"{type(exc).__name__}: {exc}" if DEBUG else str(exc)
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
    threading.Thread(target=_worker_loop, daemon=True, name="nlm-worker").start()
    threading.Thread(target=_janitor_loop, daemon=True, name="nlm-janitor").start()




@app.get("/healthz")
def healthz() -> dict:
    return {
        "ok": True,
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
            referer=spec.referer,
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


def _get_job(job_id: str, owner: str) -> Job:
    job = JOBS.get(job_id)
    if not job or job.owner != owner:
        raise HTTPException(404, "المهمة غير موجودة أو انتهت صلاحيتها")
    return job


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
def job_zip(job_id: str, owner: str = Depends(auth)):
    job = _get_job(job_id, owner)
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
    """ترويسة تنزيل تدعم الأسماء العربية (RFC 5987) مع بديل ASCII."""
    from urllib.parse import quote

    ascii_fallback = "".join(c if 32 <= ord(c) < 127 and c not in '"\\' else "_" for c in filename)
    return (f'attachment; filename="{ascii_fallback}"; '
            f"filename*=UTF-8''{quote(filename, safe='')}")


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
