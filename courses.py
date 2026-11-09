"""باقات Coursera (NoteWorm 0.9.2).

السيرفر يستقبل من الهاتف: فيديوهات MP4 (العربية مدمجة داخل الملف) + ملفات الترجمة
المنفصلة (SRT) + نصوص TXT/حزم القراءة، ثم يبني:

    ZIP واحد باسم الدورة    (delivery = zip)
    أو مجلد باسم الدورة      (delivery = folder)

ثم يستخدم **نفس** الطابور المتوالٍ للرفع إلى MEGA أو Telegram.

الهدف: أن يكون ناتج وضع كورسيرا مطابقًا لناتج وضع نوتبوك — ملف/مجلد مرتّب باسم
الدورة، مع كونه جاهزًا للرفع للسحابة والاستمرار بعد إغلاق التطبيق.
"""
from __future__ import annotations

import os
import shutil
import threading
import time
import uuid
from dataclasses import dataclass, field
from typing import Any, Callable

import media
import zipstream

COURSES: dict[str, "Course"] = {}
_LOCK = threading.Lock()
TTL_HOURS = int(os.environ.get("COURSE_TTL_HOURS") or 24)
MAX_FILES = int(os.environ.get("COURSE_MAX_FILES") or 400)
MAX_MB = int(os.environ.get("COURSE_MAX_MB") or 4096)


def base_dir() -> str:
    return os.path.join(os.environ.get("WORK_DIR", "/tmp/nlm"), "courses")


@dataclass
class CourseFile:
    idx: int
    name: str
    size: int
    path: str
    kind: str = "other"          # video | srt | txt | reading | other


@dataclass
class Course:
    id: str
    owner: str
    name: str
    delivery: str = "zip"        # zip | folder
    cloud: str = "mega"          # mega | telegram
    auto_mega: bool = True
    mega_sid: str = ""
    tg_sid: str = ""
    files: list[CourseFile] = field(default_factory=list)
    state: str = "uploading"     # uploading | packing | ready | error
    error: str = ""
    order: list[dict] = field(default_factory=list)
    created_at: float = field(default_factory=time.time)
    ready_at: float | None = None
    notes: list[str] = field(default_factory=list)
    zip_path: str = ""
    zip_bytes: int = 0
    zip_crc: int = 0
    cloud_sub: str = ""          # لدورات: نرفع مباشرة داخل مجلد الدورة
    # حالة الرفع (نفس حقول Collection كي يعمل المحرّك المتوالٍ بلا تغيير)
    mega_state: str = "idle"
    mega_link: str = ""
    mega_folder_link: str = ""
    mega_error: str = ""
    mega_percent: int = 0
    mega_files_done: int = 0
    mega_files_total: int = 0
    mega_current: str = ""
    mega_skipped: int = 0
    mega_folder: str = ""
    mega_sub: str = ""
    mega_bytes: int = 0
    mega_seconds: float = 0.0
    tg_state: str = "idle"
    tg_error: str = ""
    tg_percent: int = 0
    tg_files_done: int = 0
    tg_files_total: int = 0
    tg_current: str = ""
    tg_bytes: int = 0
    tg_seconds: float = 0.0
    tg_bot: str = ""
    tg_messages: list[dict] = field(default_factory=list)
    _lock: threading.Lock = field(default_factory=threading.Lock, repr=False)

    @property
    def dir(self) -> str:
        return os.path.join(base_dir(), self.id)

    @property
    def files_dir(self) -> str:
        return os.path.join(self.dir, "files")

    @property
    def out_dir(self) -> str:
        return os.path.join(self.dir, "out")

    def note(self, msg: str) -> None:
        self.notes.append({"at": time.time(), "msg": msg})
        if len(self.notes) > 300:
            del self.notes[:100]
        try:
            print(f"[course {self.id}] {msg}", flush=True)
        except Exception:
            pass

    def counts(self) -> dict[str, int]:
        return {"total": len(self.files),
                "bytes": sum(int(f.size or 0) for f in self.files)}

    def public(self, base: str = "") -> dict[str, Any]:
        out = {
            "id": self.id, "name": self.name, "state": self.state,
            "error": self.error, "delivery": self.delivery, "cloud": self.cloud,
            "auto_mega": self.auto_mega,
            "files": [{"idx": f.idx, "name": f.name, "size": f.size, "kind": f.kind}
                      for f in self.files],
            "count": len(self.files),
            "bytes": self.counts()["bytes"],
            "zip_bytes": self.zip_bytes,
            "created": self.created_at,
            "mega": {"state": self.mega_state, "percent": self.mega_percent,
                     "link": self.mega_link, "folder_link": self.mega_folder_link,
                     "error": self.mega_error, "files_done": self.mega_files_done,
                     "files_total": self.mega_files_total, "current": self.mega_current,
                     "folder": self.mega_folder, "sub": self.mega_sub},
            "tg": {"state": self.tg_state, "percent": self.tg_percent,
                   "error": self.tg_error, "files_done": self.tg_files_done,
                   "files_total": self.tg_files_total, "current": self.tg_current,
                   "bot": self.tg_bot,
                   "messages": len(self.tg_messages)},
            "notes": self.notes[-20:],
        }
        if self.state == "ready" and base:
            out["zip_url"] = f"{base}/courses/{self.id}/zip"
            out["files_download"] = [
                {"idx": f.idx, "name": f.name,
                 "url": f"{base}/courses/{self.id}/file/{f.idx}"} for f in self.files]
        return out


def safe_name(name: str, fallback: str = "course") -> str:
    try:
        return media.safe_name(name, 0, fallback)
    except Exception:
        return fallback


def create(owner: str, name: str, delivery: str = "zip", cloud: str = "mega",
           auto: bool = True, mega_sid: str = "", tg_sid: str = "") -> Course:
    os.makedirs(base_dir(), exist_ok=True)
    cid = uuid.uuid4().hex[:12]
    course = Course(id=cid, owner=owner, name=safe_name(name, "course"),
                    delivery="folder" if str(delivery).lower() in ("folder", "dir") else "zip",
                    cloud="telegram" if str(cloud).lower() in ("telegram", "tg", "bot") else "mega",
                    auto_mega=bool(auto), mega_sid=mega_sid or "", tg_sid=tg_sid or "")
    os.makedirs(course.files_dir, exist_ok=True)
    with _LOCK:
        COURSES[cid] = course
    course.note(f"📥 فتحت باقة الدورة «{course.name}» ({course.delivery})")
    return course


def get(cid: str) -> Course | None:
    return COURSES.get(cid)


def reserve(course: Course, name: str, kind: str = "other") -> dict:
    """يحجز اسمًا/مسارًا لملف قادم قبل بثّه (للتحميل غير المتزامن)."""
    if len(course.files) >= MAX_FILES:
        raise RuntimeError(f"الحد الأقصى {MAX_FILES} ملفًا في الباقة")
    clean = safe_name(name, "file")
    idx = len(course.files) + 1
    os.makedirs(course.files_dir, exist_ok=True)
    dest = os.path.join(course.files_dir, f"{idx:03d}-{clean}")
    return {"idx": idx, "name": clean, "path": dest, "kind": kind}


def commit(course: Course, slot: dict, size: int) -> dict:
    if size <= 0:
        try:
            os.remove(slot["path"])
        except Exception:
            pass
        raise RuntimeError("ملف فارغ")
    course.files.append(CourseFile(idx=slot["idx"], name=slot["name"], size=size,
                                   path=slot["path"], kind=slot.get("kind") or "other"))
    course.note(f"⬆ استلمت «{slot['name']}» ({size/1048576:.1f} م.ب)")
    return {"idx": slot["idx"], "name": slot["name"], "size": size}


def finalize(course: Course) -> bool:
    with course._lock:
        if course.state == "packing":
            return False
        course.state = "packing"
    threading.Thread(target=_pack, args=(course,), daemon=True,
                     name="course-pack").start()
    return True


def _pack(course: Course) -> None:
    """يرتّب الملفات بالاسم (ترقيم الهاتف) ثم يبني ZIP أو مجلدًا باسم الدورة."""
    t0 = time.time()
    try:
        if not course.files:
            raise RuntimeError("لا ملفات في الباقة")
        os.makedirs(course.out_dir, exist_ok=True)
        ordered = sorted(course.files, key=lambda f: (f.idx, f.name))
        entries: list[dict] = []
        taken: set[str] = set()
        for i, f in enumerate(ordered, start=1):
            name = f.name
            base, dot, ext = name.rpartition(".")
            if not dot:
                base, ext = name, ""
            import re as _re
            base = _re.sub(r"^\s*\d{1,4}\s*[-_.\)]\s*", "", base).strip() or base
            candidate = f"{i:03d} - {base}" + (f".{ext}" if ext else "")
            if candidate in taken:
                candidate = f"{i:03d} - {base} ({i})" + (f".{ext}" if ext else "")
            taken.add(candidate)
            entries.append({"order": i, "name": candidate, "path": f.path,
                            "size": f.size, "crc": 0, "idx": i, "kind": f.kind})
        course.order = entries
        if course.delivery == "folder":
            course.note(f"📁 المجلد جاهز · {len(entries)} ملف باسم «{course.name}»")
            course.state = "ready"
        else:
            zip_path = os.path.join(course.dir, f"{course.name}.zip")
            files, size = _build_zip(zip_path, entries)
            course.zip_path = zip_path
            course.zip_bytes = size
            course.note(f"🗜️ ZIP باسم «{course.name}.zip» · {files} ملف · "
                        f"{size/1048576:.1f} م.ب")
            course.state = "ready"
        course.ready_at = time.time()
        course.note(f"⏱️ التجهيز استغرق {time.time()-t0:.1f} ثانية")
        cb = ON_READY
        if cb is not None:
            try:
                cb(course)
            except Exception:
                pass
    except Exception as e:                      # noqa: BLE001
        course.state = "error"
        course.error = f"{type(e).__name__}: {e}"[:300]
        course.note("❌ فشل تجهيز الباقة: " + course.error[:140])


ON_READY: "Callable[[Course], None] | None" = None


def _build_zip(dest_path: str, entries: list[dict]) -> tuple[int, int]:
    import zipfile
    with zipfile.ZipFile(dest_path, "w", zipfile.ZIP_STORED, allowZip64=True) as z:
        for e in entries:
            z.write(e["path"], arcname=e["name"])
    return len(entries), os.path.getsize(dest_path)


def zip_entries(course: Course) -> tuple[list[zipstream.Entry], int]:
    entries = []
    for e in course.order:
        crc, size = zipstream.crc_of(e["path"])
        entries.append(zipstream.Entry(name=e["name"], path=e["path"], size=size, crc=crc))
    return entries, zipstream.zip_size(entries)


def delete(cid: str) -> bool:
    with _LOCK:
        course = COURSES.pop(cid, None)
    if not course:
        return False
    shutil.rmtree(course.dir, ignore_errors=True)
    return True


def janitor() -> None:
    now = time.time()
    for cid in [c.id for c in list(COURSES.values())
                if c.created_at and now - c.created_at > TTL_HOURS * 3600]:
        delete(cid)
