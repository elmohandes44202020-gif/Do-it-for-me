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
# ─────────────────── رسائل تيليجرام ⇒ عربية مفهومة ───────────────────
# المستخدم لا يجب أن يرى نص Bot API الخام أبدًا (طلب صريح: لا أخطاء تقنية).
FRIENDLY = (
    ("can't send messages to the bot",
     "معرّف المحادثة المربوط هو البوت نفسه — لا يستطيع البوت أن يراسل نفسه. "
     "اضغط «🔎 اكتشف معرّفي» في خطوة تيليجرام، أو أرسل /start للبوت واستخدم معرّفك الشخصي."),
    ("bots can't send messages to bots",
     "معرّف المحادثة المربوط هو بوت آخر — استخدم محادثتك الشخصية مع البوت."),
    ("peer_id_invalid",
     "معرّف المحادثة غير صالح — افتح البوت واضغط /start ثم أعد الربط."),
    ("chat not found",
     "لم أجد هذه المحادثة: تأكد أنك فتحت البوت وضغطت /start، وأن المعرّف هو معرّفك الشخصي."),
    ("bot was blocked by the user",
     "أنت حاجب البوت — افتح محادثة البوت في تيليجرام وأزل الحجب ثم أعد المحاولة."),
    ("user is deactivated",
     "حساب تيليجرام المستخدم غير مُفعّل — استخدم حسابًا آخر أو أعد /start للبوت."),
    ("bot can't initiate conversation",
     "البوت لا يستطيع بدء المحادثة: افتح محادثة البوت واضغط /start أولًا."),
    ("not enough rights",
     "البوت ليس عضوًا في هذه المحادثة أو لا يملك صلاحية إرسال الملفات فيها."),
    ("chat_write_forbidden",
     "لا صلاحية كتابة في هذه المحادثة — استخدم محادثة خاصة مع البوت."),
    ("unauthorized",
     "توكن البوت غير صحيح أو تم إلغاؤه — انسخ توكنًا جديدًا من @BotFather."),
    ("file is too big",
     "الملف أكبر من حد تيليجرام — بدّل التسليم إلى «مجلد» أو استخدم MEGA."),
    ("too many requests",
     "تيليجرام يطلب مهلة قصيرة — أعد المحاولة بعد ثوانٍ."),
    ("timed out", "انتهت مهلة الاتصال بتيليجرام — تحقّق من الشبكة وأعد المحاولة."),
)


def friendly_error(desc: str) -> str:
    """يحوّل وصف خطأ Bot API إلى جملة عربية واحدة مفهومة (بلا مصطلحات)."""
    text = str(desc or "").strip()
    low = text.lower()
    for needle, arabic in FRIENDLY:
        if needle in low:
            return arabic
    if not text:
        return "تعذّر الإرسال إلى تيليجرام — أعد المحاولة."
    return "تعذّر الإرسال إلى تيليجرام: " + text[:160]


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
            desc = str(body.get("description") or "")
            last = RuntimeError(friendly_error(desc if desc else f"HTTP {r.status_code}"))
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


FAKE_CHAT = (os.environ.get("NW_FAKE_TG_CHAT") or "555000").strip()


def detect_chat(token: str) -> dict:
    """يستنتج معرّف محادثتك من آخر رسالة وصلت للبوت (بعد /start).

    كان هذا هو سبب فشل «the bot can't send messages to the bot»: المستخدم يكتب
    معرّفًا خاطئًا (معرّف البوت نفسه) ولا طريقة سهلة لمعرفة معرّفه الحقيقي.
    """
    token = (token or "").strip()
    if not token:
        raise RuntimeError("توكن البوت مطلوب لاكتشاف المحادثة.")
    if FAKE:
        return {"id": FAKE_CHAT, "type": "private", "title": "محادثتك التجريبية"}
    updates = _call(token, "getUpdates", {"limit": 100, "timeout": 0,
                                          "allowed_updates": '["message"]'}, timeout=60)
    rows = updates if isinstance(updates, list) else []
    picked = None
    for upd in reversed(rows):                     # الأحدث أولًا
        msg = (upd or {}).get("message") or (upd or {}).get("edited_message") or {}
        chat = msg.get("chat") or {}
        if chat.get("id") is None:
            continue
        if (chat.get("type") or "") not in ("private", "group", "supergroup", "channel"):
            continue
        picked = chat
        break
    if picked is None:
        raise RuntimeError("لم أجد أي رسالة: افتح محادثة البوت في تيليجرام واضغط /start "
                           "ثم اضغط «🔎 اكتشف معرّفي» مرة أخرى.")
    return {"id": picked.get("id"), "type": picked.get("type") or "private",
            "title": picked.get("title") or picked.get("username")
            or picked.get("first_name") or ""}


def resolve_chat(token: str, chat: str) -> tuple[str, str]:
    """يعيد (المعرّف الصحيح, ملاحظة للمستخدم). يصحّح تلقائيًا معرّف البوت نفسه.

    سبب جذري: كان السيرفر يقبل معرّف البوت لأن getChat ينجح عليه، فيظهر الخطأ
    متأخرًا أثناء الإرسال بصيغة «the bot can't send messages to the bot».
    الآن يُصحّح عند الربط وعند الإرسال، والملاحظة تُعرض للمستخدم بالعربية.
    """
    chat = (chat or "").strip()
    if FAKE:
        if chat and chat != str(me(token).get("id")):
            return chat, ""
        found = detect_chat(token)
        note = ("🔧 كان معرّف المحادثة هو البوت نفسه أو فارغًا — "
                f"صُحّح تلقائيًا إلى محادثتك ({found['id']}).") if chat else ""
        return str(found["id"]), note
    bot_id = str((me(token) or {}).get("id") or "")
    if chat and (not bot_id or chat != bot_id):
        return chat, ""
    found = detect_chat(token)
    if str(found["id"]) == bot_id:
        raise RuntimeError("لم أجد محادثتك: افتح البوت واضغط /start ثم أعد الربط.")
    note = ("🔧 كان معرّف المحادثة هو البوت نفسه — صحّحته تلقائيًا إلى محادثتك "
            f"({found['id']})، والرسائل ستصل هناك.") if chat else ""
    return str(found["id"]), note


# ─────────────────────────── الجلسات ───────────────────────────
def create_session(token: str, chat: str) -> str:
    token = (token or "").strip()
    chat = (chat or "").strip()
    if not token:
        raise RuntimeError("توكن البوت مطلوب (من @BotFather).")
    info = me(token)                       # تحقق فعلي من التوكن قبل تخزينه
    bot_id = str(info.get("id") or "")
    note = ""
    if not chat or (bot_id and chat == bot_id):
        # الشائع: المستخدم كتب معرّف البوت نفسه ⇒ نكتشف محادثته بدل الفشل لاحقًا.
        chat, note = resolve_chat(token, chat)
    chat_info = chat_ok(token, chat)
    if bot_id and str(chat_info.get("id") or "") == bot_id:
        raise RuntimeError("هذا معرّف البوت نفسه — البوت لا يستطيع إرسال رسائل لنفسه. "
                           "أرسل /start للبوت ثم اضغط «🔎 اكتشف معرّفي» لتعبئة معرّفك الصحيح.")
    sid = os.urandom(12).hex()
    with _LOCK:
        SESSIONS[sid] = {"token": token, "chat": chat, "at": time.time(),
                         "bot": info.get("username") or "", "chat_info": chat_info,
                         "note": note, "uploads": 0, "messages": 0}
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
    info = s0.get("chat_info") or {}
    return {"alive": True, "bot": s0.get("bot"), "chat": s0.get("chat"),
            "chat_type": info.get("type", ""), "note": s0.get("note", ""),
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
