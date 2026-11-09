"""رفع ZIP إلى حساب MEGA — اختياري بالكامل.

يُفعَّل فقط عند ضبط متغيرَي البيئة:
    MEGA_EMAIL     بريد حساب ميجا
    MEGA_PASSWORD  كلمة المرور
    MEGA_FOLDER    (اختياري) اسم المجلد داخل ميجا — الافتراضي "NoteWorm"

كل الرفع تشفير من طرف المكتبة (mega.py) قبل الخروج من السيرفر،
ولا تُكتب كلمة المرور في أي سجل أو رد API.
"""
from __future__ import annotations

import base64
import os
import sys
import threading
import time
import zipfile
from typing import Any, Callable

EMAIL = (os.environ.get("MEGA_EMAIL") or "").strip()
PASSWORD = os.environ.get("MEGA_PASSWORD") or ""
FOLDER = (os.environ.get("MEGA_FOLDER") or "NoteWorm").strip() or "NoteWorm"

FAKE = (os.environ.get("NW_FAKE_MEGA") or "").strip().lower() in ("1", "true", "yes")
ATTEMPTS = max(1, int(os.environ.get("MEGA_ATTEMPTS") or 3))
CONNECT_TIMEOUT = max(5, int(os.environ.get("MEGA_CONNECT_TIMEOUT") or 30))
READ_TIMEOUT = max(30, int(os.environ.get("MEGA_READ_TIMEOUT") or 300))
PART_RETRY_SLEEP = max(0.0, float(os.environ.get("MEGA_RETRY_SLEEP") or 2.0))

_LOCK = threading.Lock()
_CLIENT: Any = None
_CLIENT_AT = 0.0
SESSION_TTL = 25 * 60          # جلسة ميجا ~25 دقيقة ثم نعيد الدخول




# ─────────────────── تحميل مكتبة ميجا عند الطلب ───────────────────
# لا نضع mega.py في requirements.txt إطلاقًا: لو فشل تثبيتها لا يجب أن يفشل
# النشر كله. نُثبّتها وقت أول استخدام داخل مجلد قابل للكتابة، وإن فشلت
# تبقى بقية خدمات السيرفر (تنزيل + تحويل + ترتيب + ZIP) تعمل طبيعيًا.

_LIB_DIR = os.environ.get("MEGA_LIB_DIR") or os.path.join(
    os.environ.get("WORK_DIR", "/tmp/nlm"), "megalib")
_lib_state = {"tried": False, "ok": False, "error": ""}


_TIMEOUTS_PATCHED = False


def install_net_timeouts() -> None:
    """mega.py يستخدم requests بلا أي timeout ⇒ اتصال «ميت» يعلّق الرفع للأبد.

    نضيف مهلة افتراضية (اتصال 30s / قراءة 300s) لكل طلب لم يُحدد له timeout،
    فيتحول التعليق إلى استثناء قابل لإعادة المحاولة.
    """
    global _TIMEOUTS_PATCHED
    if _TIMEOUTS_PATCHED:
        return
    try:
        import requests

        def _wrap(original):
            def request(self, method, url, **kw):
                if kw.get("timeout") is None:
                    kw["timeout"] = (CONNECT_TIMEOUT, READ_TIMEOUT)
                return original(self, method, url, **kw)
            return request

        requests.Session.request = _wrap(requests.Session.request)
        try:
            requests.api.request = _wrap(requests.api.request)
        except Exception:
            pass
        _TIMEOUTS_PATCHED = True
    except Exception:
        pass


def _shims() -> None:
    """mega.py 1.0.8 كُتب لبايثون ≤3.9: يحتاج ترميمًا بسيطًا على 3.10+."""
    import asyncio
    import collections
    import collections.abc
    import types
    for name in ("Sequence", "Mapping", "MutableMapping", "Iterable", "Callable",
                 "Set", "MutableSet", "Hashable", "KeysView", "ItemsView", "ValuesView"):
        if not hasattr(collections, name):
            setattr(collections, name, getattr(collections.abc, name))
    if not hasattr(asyncio, "coroutine"):
        asyncio.coroutine = types.coroutine        # tenacity 5.x يستخدمها


def _import_mega() -> Any:
    _shims()
    try:
        _shims()
        install_net_timeouts()
        from mega import Mega          # noqa: F401
        _lib_state.update(tried=True, ok=True, error="")
        return Mega
    except Exception:
        pass
    if not _lib_state["tried"]:
        _lib_state["tried"] = True
        try:
            import subprocess
            os.makedirs(_LIB_DIR, exist_ok=True)
            proc = subprocess.run(
                [sys.executable, "-m", "pip", "install", "--no-input", "--quiet",
                 "--target", _LIB_DIR, "mega.py==1.0.8"],
                capture_output=True, text=True, timeout=300)
            if proc.returncode != 0:
                _lib_state["error"] = (proc.stderr or proc.stdout or "")[-300:].strip()
        except Exception as e:               # noqa: BLE001
            _lib_state["error"] = f"{type(e).__name__}: {e}"[:300]
    if _LIB_DIR not in sys.path:
        sys.path.insert(0, _LIB_DIR)
    try:
        from mega import Mega
        _lib_state.update(ok=True, error="")
        return Mega
    except Exception as e:                   # noqa: BLE001
        _lib_state["ok"] = False
        if not _lib_state["error"]:
            _lib_state["error"] = f"{type(e).__name__}: {e}"[:300]
        raise RuntimeError("مكتبة ميجا غير متاحة: " + _lib_state["error"])


def lib_info() -> dict:
    return {"dir": _LIB_DIR, "ok": _lib_state["ok"],
            "tried": _lib_state["tried"], "error": _lib_state["error"]}


def enabled() -> bool:
    return bool(EMAIL and PASSWORD)


def info() -> dict:
    return {
        "enabled": enabled(),
        "rsa": rsa_available(),
        "sessions": live_sessions(),
        "email_set": bool(EMAIL),
        "folder": FOLDER,
        "library": _library_ok(),
        "lib": lib_info(),
    }


def _library_ok() -> bool:
    if _lib_state["ok"]:
        return True
    try:
        _import_mega()
        return True
    except Exception:
        return False


def _client() -> Any:
    global _CLIENT, _CLIENT_AT
    with _LOCK:
        if _CLIENT is None or (time.time() - _CLIENT_AT) > SESSION_TTL:
            from mega import Mega
            _CLIENT = Mega().login(EMAIL, PASSWORD)
            _CLIENT_AT = time.time()
        return _CLIENT


def _reset() -> None:
    global _CLIENT, _CLIENT_AT
    with _LOCK:
        _CLIENT = None
        _CLIENT_AT = 0.0


def _folder(m: Any) -> Any:
    """معرّف مجلد MEGA (str) أو None للرفع إلى الجذر.

    mega.py: find(name) يعيد (handle, node) — وليس handle مباشرة،
    و create_folder(name) يعيد {name: handle}. الخطأ القديم كان يمرّر أول حرف
    من الـhandle ⇒ MEGA ترد -11 (ENOENT).
    """
    try:
        hit = m.find(FOLDER)
        handle = None
        if isinstance(hit, (tuple, list)) and hit:
            node = hit[1] if len(hit) > 1 else None
            if isinstance(node, dict) and node.get("t") not in (None, 1):
                handle = None                     # ملف وليس مجلدًا
            else:
                handle = hit[0]
        elif isinstance(hit, dict):
            handle = list(hit.keys())[0] if hit else None
        elif isinstance(hit, str):
            handle = hit
        if isinstance(handle, str) and handle:
            return handle
    except Exception:
        pass
    try:
        made = m.create_folder(FOLDER)
        if isinstance(made, dict):
            for _k, v in made.items():
                if isinstance(v, str) and v:
                    return v
    except Exception:
        pass
    return None


def _fake_link(kind: str, name: str) -> str:
    import hashlib
    h = hashlib.sha1(name.encode("utf-8")).hexdigest()[:10].upper()
    return f"https://mega.nz/{kind}/{h}"


def _friendly_upload_error(exc: BaseException) -> str:
    msg = f"{type(exc).__name__}: {exc}"
    low = msg.lower()
    if "timeout" in low or "timed out" in low or "readtimeout" in low:
        return "انقطعت الاستجابة من MEGA (timeout) — أُعيدت المحاولة تلقائيًا"
    if "connection" in low or "reset" in low or "broken pipe" in low:
        return "انقطع الاتصال بـ MEGA أثناء الرفع — أُعيدت المحاولة تلقائيًا"
    if "-3" in low or "eagain" in low or "rate" in low:
        return "MEGA رفض الطلب مؤقتًا (EAGAIN) — أُعيدت المحاولة تلقائيًا"
    if "enoent" in low or "object (typically" in low:
        return "مسار الحفظ على MEGA غير صالح (ENOENT -11)"
    if "password" in low or "credentials" in low or "login" in low:
        return "بيانات دخول MEGA غير صحيحة"
    return msg[:300]


def upload(path: str, remote_name: str,
           on_state: Callable[..., None],
           client: Any = None) -> threading.Thread:
    """يرفع الملف في Thread منفصل. on_state(state=..., percent=..., link=..., error=...)."""

    def worker() -> None:
        t0 = time.time()
        try:
            # ملف واحد هو وحدة الرفع هنا (نسخة ZIP أو مجلد مضغوط) ⇒ العدّاد 0/1 ثم 1/1
            on_state(state="uploading", percent=0, files_done=0, files_total=1)
            if FAKE:
                time.sleep(0.05)
                link = _fake_link("file", remote_name)
                on_state(state="done", percent=100, link=link, folder_link=link,
                         files_done=1, files_total=1, seconds=round(time.time() - t0, 1))
                return
            m = client if client is not None else _client()
            dest = _folder(m)
            handle = None
            last: BaseException | None = None
            for attempt in range(1, ATTEMPTS + 1):
                try:
                    handle = m.upload(path, dest, dest_filename=remote_name)
                    last = None
                    break
                except Exception as first:               # noqa: BLE001
                    last = first
                    low = str(first).lower()
                    if dest and ("enoent" in low or "-11" in low or "not found" in low):
                        on_state(state="uploading", percent=0,
                                 error="مجلد MEGA غير متاح — نرفع إلى الجذر")
                        dest = None
                        try:
                            handle = m.upload(path, None, dest_filename=remote_name)
                            last = None
                            break
                        except Exception as second:      # noqa: BLE001
                            last = second
                        continue
                    if attempt < ATTEMPTS:
                        on_state(state="uploading", percent=0,
                                 error=f"إعادة المحاولة {attempt + 1}/{ATTEMPTS} — "
                                       + _friendly_upload_error(first))
                        time.sleep(PART_RETRY_SLEEP * attempt)
                        if client is None:
                            try:
                                _reset()
                                m = _client()
                                dest = _folder(m)
                            except Exception:
                                pass
            if last is not None:
                raise last
            link = ""
            try:
                link = m.get_upload_link(handle) or ""
            except Exception:
                try:
                    link = m.get_link(handle) or ""
                except Exception:
                    link = ""
            if not link:
                raise RuntimeError("تم الرفع لكن تعذّر إنشاء رابط عام")
            on_state(state="link", percent=100, link=link, files_done=1, files_total=1,
                     seconds=round(time.time() - t0, 1))
            # مجلد مشترك: نحاول إنشاء رابط للمجلد لعرض كل الملفات
            try:
                fl = m.get_folder_link(dest) if dest else ""
            except Exception:
                fl = ""
            on_state(state="done", percent=100, link=link, folder_link=fl,
                     files_done=1, files_total=1, seconds=round(time.time() - t0, 1))
        except Exception as e:                       # noqa: BLE001
            if client is None:
                _reset()
            on_state(state="error", percent=0, error=_friendly_upload_error(e)[:300],
                     seconds=round(time.time() - t0, 1))

    t = threading.Thread(target=worker, name="mega-upload", daemon=True)
    t.start()
    return t


def build_zip(dest_path: str, entries: list[dict]) -> tuple[int, int]:
    """يبني ZIP فعليًا على القرص من الملفات المرتبة (لأن الـZIP الأصلي يُبثّ ولا يُخزَّن).

    يعيد (عدد الملفات، الحجم بالبايت). ZIP_STORED لأن MP3 مضغوط أصلًا.
    """
    files = 0
    if os.path.exists(dest_path):
        os.remove(dest_path)
    with zipfile.ZipFile(dest_path, "w", zipfile.ZIP_STORED, allowZip64=True) as z:
        for e in entries:
            p = e.get("path")
            name = e.get("name")
            if not p or not name or not os.path.exists(p):
                continue
            z.write(p, arcname=name)
            files += 1
    size = os.path.getsize(dest_path)
    return files, size


# ─────────────────── قناة مشفّرة لبيانات الحساب ───────────────────
# الهاتف يشفّر (RSA-OAEP) ⇒ السيرفر يفك ⇒ ميجا تعيد التشفير داخليًا عند الدخول
# ⇒ نمسح النص الصريح من الذاكرة. لا يُكتب أي شيء على القرص.

try:
    from Crypto.PublicKey import RSA as _RSA
    from Crypto.Cipher import PKCS1_OAEP as _OAEP
    _KEY = _RSA.generate(2048)
    _RSA_OK = True
except Exception:                      # noqa: BLE001
    _KEY = None
    _RSA_OK = False

SESSIONS: dict[str, dict] = {}
SESS_TTL = int(os.environ.get("MEGA_SESS_TTL_MIN", "720")) * 60   # 12 ساعة افتراضيًا


def rsa_available() -> bool:
    return _RSA_OK


def public_pem() -> str:
    if not _RSA_OK:
        raise RuntimeError("وحدة التشفير غير متاحة على السيرفر (pycryptodome)")
    return _KEY.publickey().export_key().decode("ascii")


def open_sealed(sealed_b64: str) -> tuple[str, str]:
    """يفك ظرف الهاتف ويعيد (email, password). يمسح المخزن المؤقت بعد الاستخدام."""
    if not _RSA_OK:
        raise RuntimeError("وحدة التشفير غير متاحة على السيرفر")
    raw = base64.b64decode(sealed_b64 or "")
    buf = bytearray(_OAEP.new(_KEY).decrypt(raw))
    try:
        text = buf.decode("utf-8")
        email, _, password = text.partition("\n")
        return email.strip(), password
    finally:
        for i in range(len(buf)):      # تصفير الذاكرة
            buf[i] = 0
        del raw


def mask(email: str) -> str:
    if not email:
        return "•"
    name, _, dom = email.partition("@")
    keep = name[:2] if len(name) > 2 else name[:1]
    return keep + "•" * max(2, len(name) - len(keep)) + ("@" + dom if dom else "")


def create_session(email: str, password: str) -> str:
    Mega = _import_mega()
    m = Mega().login(email, password)          # ميجا تُشفّر بيانات الدخول داخليًا
    sid = base64.urlsafe_b64encode(os.urandom(18)).decode().rstrip("=")
    SESSIONS[sid] = {"client": m, "at": time.time(),
                     "email": mask(email), "uploads": 0}
    return sid


def session(sid: str) -> dict | None:
    s0 = SESSIONS.get(sid)
    if not s0:
        return None
    if time.time() - s0["at"] > SESS_TTL:
        drop_session(sid)
        return None
    return s0


def drop_session(sid: str) -> bool:
    s0 = SESSIONS.pop(sid, None)
    if not s0:
        return False
    try:
        s0["client"] = None        # إسقاط مرجع عميل ميجا
    except Exception:
        pass
    return True


def live_sessions() -> int:
    for k in [k for k, v in SESSIONS.items() if time.time() - v["at"] > SESS_TTL]:
        drop_session(k)
    return len(SESSIONS)


def upload_with(client: Any, path: str, remote_name: str,
                on_state: Callable[..., None]) -> threading.Thread:
    """نفس upload لكن بعميل جلسة محددة (بيانات الحساب لا تُخزَّن)."""
    return upload(path, remote_name, on_state, client=client)


SORTED_NAME = (os.environ.get("SORTED_DIRNAME") or "تم الترتيب").strip() or "تم الترتيب"


def _handle_of(made: Any) -> str | None:
    """create_folder يعيد {name: handle} — نستخرج الـhandle بأمان."""
    if isinstance(made, dict):
        for v in made.values():
            if isinstance(v, str) and v:
                return v
    if isinstance(made, str) and made:
        return made
    return None


def _ensure_folder(m: Any, name: str, parent: str | None) -> str:
    """يجد مجلدًا بالاسم داخل الأب أو يُنشئه، ويعيد الـhandle. يرفع استثناءً عند الفشل."""
    try:
        files = m.get_files() or {}
        for _h, info in files.items():
            if info.get("t") == 1 or info.get("t") == 2:      # 1/2 = مجلد
                a = info.get("a") or {}
                if a.get("n") == name and (parent is None or info.get("p") == parent):
                    return _h
    except Exception:
        pass
    try:
        made = m.create_folder(name, dest=parent) if parent else m.create_folder(name)
    except TypeError:
        made = m.create_folder(name)
    handle = _handle_of(made)
    if not handle:
        raise RuntimeError(f"تعذّر إنشاء مجلد «{name}» على MEGA")
    return handle


def _children(m: Any, parent: str) -> dict[str, int]:
    """{اسم: حجم} للملفات الموجودة داخل مجلد — للاستئناف بلا تكرار."""
    out: dict[str, int] = {}
    try:
        files = m.get_files() or {}
    except Exception:
        return out
    for _h, info in files.items():
        if info.get("p") == parent and info.get("t") == 0:
            a = info.get("a") or {}
            nm = a.get("n")
            if nm:
                out[str(nm)] = int(info.get("s") or 0)
    return out


def upload_folder(client: Any, files: list[dict], folder_name: str,
                  on_state: Callable[..., None],
                  sub: str | None = None) -> threading.Thread:
    """يرفع الملفات داخل: مجلد باسم الدفتر ← مجلد «تم الترتيب» ← الملفات.

    إصلاح 0.9.0 (سبب «توقف الرفع في المنتصف» + «فشل timeout»):
    - كل ملف يُعاد رفعُه حتى ATTEMPTS مرات بمهلة قصيرة بينها، فلا يُسقط ملف واحد
      الدفعة كلها كما كان يحدث قبلًا.
    - مهلات شبكة حقيقية (install_net_timeouts) فلا تعليق بلا نهاية.
    - إن كان العميل من متغيرات البيئة نُعيد تسجيل الدخول بعد الفشل ونتابع.
    - يتخطى ما هو موجود بنفس الاسم والحجم (استئناف بلا تكرار).
    """

    def worker() -> None:
        t0 = time.time()
        total = max(1, len(files))
        done = 0
        skipped = 0
        failed: list[str] = []
        try:
            on_state(state="uploading", percent=0, files_done=0, files_total=len(files),
                     current="")
            if FAKE:
                for i, f in enumerate(files, start=1):
                    if i % 2 == 0:
                        on_state(state="uploading", percent=int((i - 1) * 100 / total),
                                 files_done=i - 1, files_total=total,
                                 current=f"محاولة سابقة: {f.get('name')}")
                        time.sleep(0.15)
                    on_state(state="uploading", percent=int(i * 100 / total),
                             files_done=i, files_total=total, current=f.get("name") or "")
                    time.sleep(0.25)
                link = _fake_link("folder", folder_name)
                on_state(state="done", percent=100, link=link, folder_link=link,
                         files_done=len(files), files_total=total, skipped=0,
                         folder=folder_name, subfolder=SORTED_NAME, current="",
                         seconds=round(time.time() - t0, 1))
                return

            m = client if client is not None else _client()
            parent = _folder(m)                      # مجلد NoteWorm (قد يكون None)
            root = _ensure_folder(m, folder_name, parent)
            sub_name = SORTED_NAME if sub is None else str(sub)
            inner = _ensure_folder(m, sub_name, root) if sub_name else root
            existing = _children(m, inner)
            for f in files:
                path = f.get("path")
                name = f.get("name") or os.path.basename(path or "")
                if not path or not os.path.exists(path):
                    continue
                size = os.path.getsize(path)
                on_state(state="uploading", percent=int(done * 100 / total),
                         files_done=done, files_total=total, current=name)
                if existing.get(name) == size:
                    skipped += 1
                else:
                    last: BaseException | None = None
                    for attempt in range(1, ATTEMPTS + 1):
                        try:
                            m.upload(path, inner, dest_filename=name)
                            last = None
                            break
                        except BaseException as exc:      # noqa: BLE001
                            last = exc
                            if attempt < ATTEMPTS:
                                on_state(state="uploading",
                                         percent=int(done * 100 / total),
                                         files_done=done, files_total=total,
                                         current=f"{name} — إعادة المحاولة {attempt + 1}/{ATTEMPTS}")
                                time.sleep(PART_RETRY_SLEEP * attempt)
                                if client is None:
                                    try:
                                        _reset()
                                        m = _client()
                                        root = _ensure_folder(m, folder_name, parent)
                                        inner = _ensure_folder(m, SORTED_NAME, root)
                                        existing = _children(m, inner)
                                    except Exception:
                                        pass
                    if last is not None:
                        failed.append(name)
                        on_state(state="uploading", percent=int(done * 100 / total),
                                 files_done=done, files_total=total, current=name,
                                 error=f"تعذّر رفع {name}: {_friendly_upload_error(last)}")
                done += 1
                on_state(state="uploading", percent=int(done * 100 / total),
                         files_done=done, files_total=total, current=name)
            link = ""
            inner_link = ""
            try:
                link = m.get_folder_link(root) or ""
            except Exception:
                link = ""
            try:
                inner_link = m.get_folder_link(inner) or ""
            except Exception:
                inner_link = ""
            ok = done - len(failed)
            on_state(state="done" if ok > 0 else "error",
                     percent=100 if ok > 0 else 0,
                     link=link or inner_link, folder_link=link or inner_link,
                     files_done=done, files_total=total, skipped=skipped,
                     folder=folder_name, subfolder=SORTED_NAME, current="",
                     error=("" if not failed else
                            f"فشل رفع {len(failed)} ملف بعد {ATTEMPTS} محاولات: "
                            + "، ".join(failed[:5])),
                     seconds=round(time.time() - t0, 1))
        except Exception as e:                       # noqa: BLE001
            on_state(state="error", percent=0, error=_friendly_upload_error(e),
                     current="", seconds=round(time.time() - t0, 1))

    t = threading.Thread(target=worker, name="mega-folder", daemon=True)
    t.start()
    return t
