"""اختبار 0.9.3 — خطأ «the bot can't send messages to the bot».

السبب: المستخدم يكتب معرّف البوت نفسه في خانة المحادثة (أو يتركه فارغًا)، والسيرفر
كان يقبله لأن getChat ينجح عليه، فيظهر الفشل متأخرًا أثناء الإرسال بنص Bot API خام.

المُتحقَّق هنا:
1. الربط بمعرّف البوت ⇒ يُصحَّح تلقائيًا إلى محادثة المستخدم مع ملاحظة عربية.
2. `/tg/detect` يكتشف المعرّف من آخر رسالة للبوت.
3. `resolve_chat` يصحّح جلسة قديمة معطوبة عند الإرسال (لا إفشال للرفع).
4. رسائل الأخطاء عربية بلا مصطلحات ولا اسم كلاس.

التشغيل: python3 t_tg_chat.py
"""
from __future__ import annotations

import base64
import os
import subprocess
import sys
import time

import httpx

HERE = os.path.dirname(os.path.abspath(__file__))
PORT = 8871
KEY = "tg-chat-key"
BOT_ID = "123456"          # نفس معرّف البوت الوهمي في tg_up.me()
GOOD_CHAT = "555000"       # المحادثة الوهمية التي «يکتشفها» السيرفر


def seal(pubkey_pem: str, text: str) -> str:
    from Crypto.Cipher import PKCS1_OAEP
    from Crypto.PublicKey import RSA
    return base64.b64encode(PKCS1_OAEP.new(RSA.import_key(pubkey_pem))
                            .encrypt(text.encode())).decode()


def main() -> int:
    env = {**os.environ, "API_KEYS": f"me:{KEY}", "WORK_DIR": "/tmp/nw-tgchat",
           "NW_FAKE_MEGA": "1", "NW_FAKE_TG": "1"}
    api = subprocess.Popen([sys.executable, "-m", "uvicorn", "main:app", "--host",
                            "127.0.0.1", "--port", str(PORT), "--log-level", "warning"],
                           cwd=HERE, env=env)
    base = f"http://127.0.0.1:{PORT}"
    H = {"X-Api-Key": KEY}
    try:
        for _ in range(150):
            try:
                if httpx.get(base + "/healthz", timeout=1).status_code == 200:
                    break
            except Exception:
                time.sleep(0.2)
        print("· إصدار السيرفر:", httpx.get(base + "/healthz").json()["version"])
        pem = httpx.get(base + "/tg/key", headers=H, timeout=30).json()["pubkey"]

        # 1) الربط بمعرّف البوت نفسه ⇒ تصحيح تلقائي (بدل الفشل لاحقًا)
        r = httpx.post(base + "/tg/session", headers=H, timeout=60,
                       json={"sealed": seal(pem, "123:TOKEN\n" + BOT_ID)})
        assert r.status_code == 200, r.text
        j = r.json()
        print("✓ الربط بمعرّف البوت:", {k: j[k] for k in ("chat", "chat_fixed", "bot")})
        assert j["chat"] == GOOD_CHAT, j
        assert j["chat_fixed"] is True
        assert "البوت نفسه" in j["note"], j["note"]
        sid = j["session_id"]

        # 2) اكتشاف المحادثة بالتوكن وحده
        d = httpx.post(base + "/tg/detect", headers=H, timeout=60,
                       json={"sealed": seal(pem, "123:TOKEN\n")})
        assert d.status_code == 200, d.text
        print("✓ /tg/detect:", d.json()["chat"], "| البوت:", d.json()["bot"])
        assert d.json()["chat"] == GOOD_CHAT

        # 3) الربط الصحيح يبقى كما هو (لا تصحيح بلا داعٍ)
        r2 = httpx.post(base + "/tg/session", headers=H, timeout=60,
                        json={"sealed": seal(pem, "123:TOKEN\n" + GOOD_CHAT)}).json()
        assert r2["chat"] == GOOD_CHAT and r2["chat_fixed"] is False and r2["note"] == "", r2
        print("✓ الربط الصحيح بلا تعديل")

        # 4) الإرسال بعد الجلسة المصحّحة يعمل لآخر خطوة
        c = httpx.post(base + "/collections", headers=H, timeout=60, json={
            "name": "اختبار المحادثة",
            "files": [{"title": "الحلقة الأولى", "url": f"{base}/t.mp3"}]}).json()["collection_id"]
        httpx.post(base + f"/collections/{c}/cloud", headers=H, timeout=30,
                   json={"cloud": "telegram", "session_id": sid})
        for _ in range(60):
            st = httpx.get(base + f"/collections/{c}/tg", headers=H, timeout=30).json()
            if st.get("state") in ("done", "error"):
                break
            time.sleep(0.2)
        print("· إرسال بعد التصحيح — الحالة:", st.get("state"), "| الملفات:", st.get("files_done"))
        # لا ملفات حقيقية على القرص هنا، فلا نتحقق من الإرسال نفسه بل من عدم رسالة تقنية
        assert "RuntimeError" not in (st.get("error") or ""), st

        print("\nكل اختبارات تصحيح معرّف محادثة تيليجرام نجحت ✅")
        return 0
    finally:
        api.terminate()


if __name__ == "__main__":
    rc = main()
    # 5) اختبارات وحدة داخل نفس العملية (بلا شبكة)
    os.environ["NW_FAKE_TG"] = "1"
    sys.path.insert(0, HERE)
    import tg_up

    msg = tg_up.friendly_error("Forbidden: the bot can't send messages to the bot")
    print("✓ رسالة الخطأ للمستخدم:", msg)
    assert "البوت نفسه" in msg and "Forbidden" not in msg

    msg2 = tg_up.friendly_error("Bad Request: chat not found")
    assert "لم أجد هذه المحادثة" in msg2

    tok = "123:TOKEN"
    fixed, note = tg_up.resolve_chat(tok, BOT_ID)
    print("✓ resolve_chat:", fixed, "|", note)
    assert fixed == GOOD_CHAT and note
    same, quiet = tg_up.resolve_chat(tok, GOOD_CHAT)
    assert same == GOOD_CHAT and quiet == ""

    import main as srv
    assert srv._user_error(RuntimeError("نص عربي مفهوم")) == "نص عربي مفهوم"
    assert "RuntimeError" not in srv._user_error(RuntimeError("نص عربي مفهوم"))
    print("✓ لا اسم كلاس تقني في رسائل المستخدم")
    print("\nكل الاختبارات نجحت ✅✅")
    raise SystemExit(rc)
