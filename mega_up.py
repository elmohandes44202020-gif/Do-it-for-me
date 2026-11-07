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
    """مجلد مستهدف داخل ميجا (يُنشأ مرة واحدة)."""
    try:
        node = m.find(FOLDER)
        if node:
            return node[0]
    except Exception:
        pass
    try:
        return m.create_folder(FOLDER)
    except Exception:
        return None


def upload(path: str, remote_name: str,
           on_state: Callable[..., None],
           client: Any = None) -> threading.Thread:
    """يرفع الملف في Thread منفصل. on_state(state=..., percent=..., link=..., error=...)."""

    def worker() -> None:
        t0 = time.time()
        try:
            on_state(state="uploading", percent=0)
            m = client if client is not None else _client()
            dest = _folder(m)
            handle = m.upload(path, dest, dest_filename=remote_name)
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
            on_state(state="link", percent=100, link=link,
                     seconds=round(time.time() - t0, 1))
            # مجلد مشترك: نحاول إنشاء رابط للمجلد لعرض كل الملفات
            try:
                fl = m.get_folder_link(dest) if dest else ""
            except Exception:
                fl = ""
            on_state(state="done", percent=100, link=link, folder_link=fl,
                     seconds=round(time.time() - t0, 1))
        except Exception as e:                       # noqa: BLE001
            _reset()
            msg = f"{type(e).__name__}: {e}"
            low = msg.lower()
            if "password" in low or "credentials" in low or "login" in low:
                msg = "بيانات دخول ميجا غير صحيحة (MEGA_EMAIL / MEGA_PASSWORD)"
            on_state(state="error", percent=0, error=msg[:300],
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
SESS_TTL = 30 * 60


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
