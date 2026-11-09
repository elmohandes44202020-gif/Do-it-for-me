"""Telegram Bot كسحابة تخزين ثانية بجانب MEGA (NoteWorm 0.9.1).

الفكرة العملية:
- المستخدم يصنع بوتًا واحدًا عند @BotFather ويأخذ التوكن، ويبدأ محادثة معه (/start).
- التطبيق يُشفّر التوكن + معرّف المحادثة بمفتاح السيرفر العام (RSA-OAEP) ويُرسلهما.
- السيرفر يفتح «جلسة تيليجرام» في الذاكرة فقط (لا يُكتب التوكن على القرص)، ثم
  يرسل الملفات المرتّبة إلى محادثة المستخدم عبر sendDocument بالترتيب.
- لكل محادثة/رسالة يمكن تنزيل الملف لاحقًا من أي جهاز عبر نقطة `/tg/file/...`
  في السيرفر (تُبنى من file_id)، أو من تطبيق تيليجرام نفسه.

حدود حقيقية يجب معرفتها:
- Bot API السحابي: أقصى حجم للملف **50 م.ب** (قابل للتغيير بـ TELEGRAM_MAX_MB).
  لتجاوزه يلزم تشغيل خادم Bot API محلي (2GB) وهو خارج نطاق سيرفر Faable.
- تيليجرام لا يعطي رابطًا عامًا للملف؛ الرابط العام يُقدَّم من سيرفرك عبر file_id.

التوكن لا يظهر في أي سجل ولا في أي رد API.
"""
from __future__ import annotations

import os
import threading
import time
from typing import Any, Iterator

import httpx

import mega_up

API_BASE = (os.environ.get("TELEGRAM_API_BASE") or "https://api.telegram.org").rstrip("/")
TOKEN_ENV = (os.environ.get("TELEGRAM_BOT_TOKEN") or "").strip()
CHAT_ENV = (os.environ.get("TELEGRAM_CHAT_ID") or "").strip()
MAX_MB = max(1, int(os.environ.get("TELEGRAM_MAX_MB") or 50))
ATTEMPTS = max(1, int(os.environ.get("TELEGRAM_ATTEMPTS") or 3))
TIMEOUT = float(os.environ.get("TELEGRAM_TIMEOUT") or 300)
FAKE = (os.environ.get("NW_FAKE_TG") or "").strip().lower() in ("1", "true", "yes")
SESS_TTL = int(os.environ.get("TELEGRAM_SESS_TTL_MIN") or 720) * 60

SESSIONS: dict[str, dict[str, Any]] = {}
_LOCK = threading.Lock()
_FAKE_DIR = os.path.join(os.environ.get("WORK_DIR", "/tmp/nlm"), "tg-fake")


# ─────────────────────────── تشفير الجلسات ───────────────────────────
def rsa_available() -> bool:
    return mega_up.rsa_available()


def public_pem() -> str:
    return mega_up.public_pem()


def open_sealed(sealed_b64: str) -> tuple[str, str]:
    """يفك ظرف الهاتف ويعيد (token, chat_id). يمسح المخزن المؤقت."""
    return mega_up.open_sealed(sealed_b64)


def mask(token: str) -> str:
    t = (token or "").strip()
    if not t:
        return "•"
    head = t.split(":")[0] if ":" in t else t[:4]
    return head + "•" * 8 + (":••••" if ":" in t else "")


def enabled() -> bool:
    return bool(TOKEN_ENV and CHAT_ENV) or FAKE


def env_info() -> dict:
    return {"enabled": bool(TOKEN_ENV and CHAT_ENV), "chat": CHAT_ENV,
            "max_mb": MAX_MB, "fake": FAKE}


# ─────────────────────────── استدعاءات API ───────────────────────────
def _url(token: str, method: str) -> str:
    return f"{API_BASE}/bot{token}/{method}"


def _call(token: str, method: str, data: dict | None = None,
          files: dict | None = None, timeout: float | None = None) -> dict:
    """استدعاء واحد مع إعادة محاولة (429/شبكة) — بلا تسجيل التوكن."""
    last: Exception | None = None
    for attempt in range(1, ATTEMPTS + 1):
        try:
            with httpx.Client(timeout=timeout or TIMEOUT) as c:
                if files:
                    r = c.post(_url(token, method), data=data or {}, files=files)
                else:
                    r = c.post(_url(token, method), data=data or {})
            body = {}
            try:
                body = r.json()
            except Exception:
                body = {}
            if r.status_code == 200 and body.get("ok"):
                return body.get("result") or {}
            if r.status_code == 429:
                wait = float((body.get("parameters") or {}).get("retry_after") or 3)
                time.sleep(min(30.0, wait))
                last = RuntimeError(f"Telegram 429 — انتظر {int(wait)} ثانية")
                continue
            desc = str(body.get("description") or f"HTTP {r.status_code}")
            last = RuntimeError(f"Telegram: {desc}")
            if r.status_code in (400, 401, 403, 404):
                raise last
            time.sleep(1.5 * attempt)
        except RuntimeError:
            raise
        except Exception as exc:                      # noqa: BLE001
            last = exc
            time.sleep(1.5 * attempt)
    raise RuntimeError(f"تعذّر الاتصال بتيليجرام بعد {ATTEMPTS} محاولات: {last}")


def me(token: str) -> dict:
    if FAKE:
        return {"id": 123456, "username": "noteworm_test_bot", "first_name": "NoteWorm"}
    r = _call(token, "getMe", timeout=30)
    return {"id": r.get("id"), "username": r.get("username") or "", "first_name": r.get("first_name") or ""}


def chat_ok(token: str, chat: str) -> dict:
    if FAKE:
        return {"id": chat, "type": "private"}
    r = _call(token, "getChat", {"chat_id": chat}, timeout=30)
    return {"id": r.get("id"), "type": r.get("type") or "", "title": r.get("title") or r.get("username") or ""}


# ─────────────────────────── الجلسات ───────────────────────────
def create_session(token: str, chat: str) -> str:
    token = (token or "").strip()
    chat = (chat or "").strip()
    if not token or not chat:
        raise RuntimeError("توكن البوت ومعرّف المحادثة مطلوبان")
    info = me(token)                       # تحقق فعلي من التوكن قبل تخزينه
    chat_info = chat_ok(token, chat)
    sid = os.urandom(12).hex()
    with _LOCK:
        SESSIONS[sid] = {"token": token, "chat": chat, "at": time.time(),
                         "bot": info.get("username") or "", "chat_info": chat_info,
                         "uploads": 0, "messages": 0}
    return sid


def session(sid: str) -> dict | None:
    if not sid:
        return None
    s0 = SESSIONS.get(sid)
    if not s0:
        return None
    if time.time() - s0["at"] > SESS_TTL:
        drop_session(sid)
        return None
    return s0


def drop_session(sid: str) -> bool:
    with _LOCK:
        s0 = SESSIONS.pop(sid, None)
    if not s0:
        return False
    s0["token"] = ""                       # إسقاط المرجع من الذاكرة
    return True


def live_sessions() -> int:
    for k in [k for k, v in SESSIONS.items() if time.time() - v["at"] > SESS_TTL]:
        drop_session(k)
    return len(SESSIONS)


def session_info(sid: str) -> dict:
    s0 = session(sid)
    if not s0:
        return {"alive": False}
    return {"alive": True, "bot": s0.get("bot"), "chat": s0.get("chat"),
            "uploads": s0.get("uploads", 0), "messages": s0.get("messages", 0),
            "expires_in_sec": max(0, int(SESS_TTL - (time.time() - s0["at"])))}


# ─────────────────────────── الإرسال ───────────────────────────
def _fake_store(path: str, file_id: str) -> None:
    os.makedirs(_FAKE_DIR, exist_ok=True)
    dest = os.path.join(_FAKE_DIR, file_id.replace(":", "_"))
    with open(path, "rb") as src, open(dest, "wb") as out:
        while True:
            chunk = src.read(1 << 20)
            if not chunk:
                break
            out.write(chunk)


def send_document(token: str, chat: str, path: str, filename: str,
                  caption: str = "") -> dict:
    """يرسل ملفًا واحدًا ويعيد {file_id, message_id, size}."""
    size = os.path.getsize(path)
    if size > MAX_MB * 1048576:
        raise RuntimeError(
            f"الملف «{filename}» حجمه {size/1048576:.1f} م.ب وأقصى حد لتيليجرام "
            f"{MAX_MB} م.ب — استخدم MEGA لهذا الملف أو قسّمه.")
    if FAKE:
        file_id = "FAKE:" + os.urandom(8).hex()
        _fake_store(path, file_id)
        return {"file_id": file_id, "message_id": int(time.time() * 1000) % 1000000,
                "size": size, "file_name": filename}
    with open(path, "rb") as fh:
        files = {"document": (filename, fh, "application/octet-stream")}
        data = {"chat_id": chat, "caption": caption[:1000] if caption else "",
                "disable_notification": "true"}
        res = _call(token, "sendDocument", data=data, files=files, timeout=max(TIMEOUT, 900))
    doc = res.get("document") or {}
    return {"file_id": doc.get("file_id") or "", "message_id": res.get("message_id") or 0,
            "size": int(doc.get("file_size") or size), "file_name": doc.get("file_name") or filename}


def get_file(token: str, file_id: str) -> dict:
    if FAKE:
        return {"path": file_id, "size": 0}
    r = _call(token, "getFile", {"file_id": file_id}, timeout=60)
    return {"path": r.get("file_path") or "", "size": int(r.get("file_size") or 0)}


def download_stream(token: str, file_id: str) -> tuple[Iterator[bytes], int]:
    """يعيد (مولّد بايتات, الحجم المتوقع) لتنزيل الملف من تيليجرام."""
    if FAKE:
        dest = os.path.join(_FAKE_DIR, file_id.replace(":", "_"))

        def fake_iter() -> Iterator[bytes]:
            with open(dest, "rb") as fh:
                while True:
                    chunk = fh.read(1 << 16)
                    if not chunk:
                        break
                    yield chunk

        return fake_iter(), os.path.getsize(dest)
    info = get_file(token, file_id)
    if not info["path"]:
        raise RuntimeError("تيليجرام لم يُعِد مسار الملف")
    url = f"{API_BASE}/file/bot{token}/{info['path']}"

    def real_iter() -> Iterator[bytes]:
        with httpx.Client(timeout=TIMEOUT) as c:
            with c.stream("GET", url) as r:
                if r.status_code != 200:
                    raise RuntimeError(f"تيليجرام: HTTP {r.status_code}")
                for chunk in r.iter_bytes(1 << 16):
                    yield chunk

    return real_iter(), int(info.get("size") or 0)


def delete_message(token: str, chat: str, message_id: int) -> bool:
    if FAKE or not message_id:
        return False
    try:
        _call(token, "deleteMessage", {"chat_id": chat, "message_id": message_id}, timeout=30)
        return True
    except Exception:
        return False
