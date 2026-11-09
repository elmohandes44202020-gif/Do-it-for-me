"""اختبار 3.67 — خانات تسجيل الدخول للسحابة داخل خطوة الرفع (نفس تشفير الهاتف).

يتحقق من:
  ① /mega/key ⇒ ظرف RSA-OAEP(SHA1) يحتوي «email\\npassword» ⇒ /mega/session يعيد جلسة.
  ② /cloud/status يرى جلسة MEGA بعد الدخول.
  ③ /tg/key ⇒ «token\\nchat» ⇒ /tg/session يعيد جلسة + البوت.
  ④ /tg/detect يكتشف معرّف المحادثة من التوكن وحده.
  ⑤ باقة كاملة: تنزيل ← تغليف ← رفع بالجلسة الناتجة من الخانات ⇒ done.

التشغيل: python3 t_login.py
"""
from __future__ import annotations

import base64
import os
import subprocess
import sys
import time

import httpx

HERE = os.path.dirname(os.path.abspath(__file__))
PA, KEY = 8861, "login-key-1"


def seal(pubkey_pem: str, plain: str) -> str:
    from Crypto.PublicKey import RSA
    from Crypto.Cipher import PKCS1_OAEP
    return base64.b64encode(
        PKCS1_OAEP.new(RSA.import_key(pubkey_pem)).encrypt(plain.encode())).decode()


def wait_for(fn, limit=120.0, step=0.2):
    end = time.time() + limit
    last = None
    while time.time() < end:
        last = fn()
        if last:
            return last
        time.sleep(step)
    return last


def main() -> int:
    env = {**os.environ, "API_KEYS": f"me:{KEY}", "WORK_DIR": "/tmp/nw-login",
           "NW_FAKE_MEGA": "1", "NW_FAKE_TG": "1"}
    api = subprocess.Popen([sys.executable, "-m", "uvicorn", "main:app", "--host",
                            "127.0.0.1", "--port", str(PA), "--log-level", "warning"],
                           cwd=HERE, env=env)
    base = f"http://127.0.0.1:{PA}"
    H = {"X-Api-Key": KEY}
    try:
        for _ in range(150):
            try:
                if httpx.get(base + "/healthz", timeout=1).status_code == 200:
                    break
            except Exception:
                time.sleep(0.2)
        print("· إصدار السيرفر:", httpx.get(base + "/healthz").json()["version"])

        # ⓪ فحص الاتصال (الزر الجديد في خطوة الرفع)
        dg = httpx.get(base + "/mega/diag", headers=H, timeout=120).json()
        print("· /mega/diag:", dg.get("verdict"))
        assert "verdict" in dg and "advice" in dg and dg.get("fake") is True, dg
        assert dg["ok"] is True, dg

        # ① دخول MEGA كما يفعل الهاتف بالضبط
        k = httpx.get(base + "/mega/key", headers=H, timeout=30).json()
        assert k.get("pubkey", "").startswith("-----BEGIN PUBLIC KEY-----"), k
        r = httpx.post(base + "/mega/session", headers=H, timeout=120,
                       json={"sealed": seal(k["pubkey"], "student@example.com\nS3cretPass")})
        msid = r.json().get("session_id", "")
        print("✓ دخول MEGA من الخانات:", r.status_code, "· جلسة:", bool(msid),
              "· بريد:", r.json().get("email", ""))
        assert r.status_code == 200 and msid, r.text

        # ② الحالة كما تعرضها الخطوة 7
        st = httpx.get(base + "/cloud/status", headers=H,
                       params={"mega_session": msid}, timeout=30).json()
        print("· /cloud/status (بعد الدخول):", st["mega"])
        assert st["mega"]["session"] is True, st

        # ③ ربط البوت
        tk = httpx.get(base + "/tg/key", headers=H, timeout=30).json()
        r = httpx.post(base + "/tg/session", headers=H, timeout=120,
                       json={"sealed": seal(tk["pubkey"], "123:TOKEN\n555000")})
        tsid = r.json().get("session_id", "")
        print("✓ ربط البوت من الخانات:", r.status_code, "· البوت:", r.json().get("bot", ""),
              "· المحادثة:", r.json().get("chat", ""))
        assert r.status_code == 200 and tsid, r.text

        # ④ اكتشاف المحادثة بالتوكن وحده
        d = httpx.post(base + "/tg/detect", headers=H, timeout=120,
                       json={"sealed": seal(tk["pubkey"], "123:TOKEN")}).json()
        print("· اكتشاف المحادثة:", d.get("chat"), "· البوت:", d.get("bot"))
        assert d.get("chat") == "555000", d

        # ⑤ الخط كامل: باقة ⇒ تغليف ⇒ رفع بالجلسة الناتجة من الخانات
        cid = httpx.post(base + "/courses", headers=H, timeout=30, json={
            "name": "دورة الخانات", "delivery": "folder", "cloud": "mega",
            "auto": False, "mega_session": msid, "tg_session": tsid}).json()["course_id"]
        for fname, kind in (("001 - مقدمة.mp4", "video"), ("002 - مقدمة.ar.srt", "srt")):
            httpx.post(base + f"/courses/{cid}/files", params={"name": fname, "kind": kind},
                       content=b"DATA" * 2048, headers=H, timeout=60)
        httpx.post(base + f"/courses/{cid}/finalize", headers=H, timeout=30)
        wait_for(lambda: httpx.get(base + f"/courses/{cid}", headers=H,
                                   timeout=30).json()["state"] == "ready", 60)
        rr = httpx.post(base + f"/courses/{cid}/cloud", headers=H, timeout=30,
                        json={"cloud": "mega", "mega_session": msid, "tg_session": tsid})
        print("· /cloud بالجلسة:", rr.status_code, rr.json())
        assert rr.status_code == 202, rr.text
        stc = wait_for(lambda: (lambda j: j if j["mega"]["state"] in ("done", "error") else None)(
            httpx.get(base + f"/courses/{cid}", headers=H, timeout=30).json()), 120)
        assert stc and stc["mega"]["state"] == "done", stc and stc["mega"]
        print(f"✓ الرفع بالجلسة تم: {stc['mega']['percent']}% داخل «{stc['mega']['folder']}»")

        print("\nكل اختبارات خانات الدخول للسحابة نجحت ✅")
        return 0
    finally:
        api.terminate()
        try:
            api.wait(timeout=10)
        except Exception:
            api.kill()


if __name__ == "__main__":
    raise SystemExit(main())
