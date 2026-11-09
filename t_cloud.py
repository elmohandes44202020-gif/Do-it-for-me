"""اختبار 0.9.5 — رفع الباقة إلى السحابة من السيرفر (MEGA + Telegram) ولوحة العمليات.

يعالج بلاغ: «خطوة الرفع على ميجا أو تليجرام لا ترفع شيئًا وتكتفي بقول عملية جارية الآن».
يشغّل السيرفر بوضع MEGA/Telegram الوهمي ثم يتحقق من:
  ① الرفع إلى MEGA يبدأ فعلًا ويصل done مع اسم مجلد الرفع.
  ② الرفع إلى Telegram يبدأ فعلًا ويُرسل ملفات (رسائل > 0).
  ③ رفض الرفع قبل التغليف برسالة عربية صريحة (409) — لا صمت ولا queued:false.
  ④ /ops يعرض عمليتي التنزيل والرفع معًا.

التشغيل: python3 t_cloud.py
"""
from __future__ import annotations

import base64
import os
import subprocess
import sys
import time

import httpx

HERE = os.path.dirname(os.path.abspath(__file__))
PA, KEY = 8858, "cloud-key-1"


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
    env = {**os.environ, "API_KEYS": f"me:{KEY}", "WORK_DIR": "/tmp/nw-cloud",
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

        # جلسة تيليجرام مشفّرة (اختبار النقل عبر البوت)
        k = httpx.get(base + "/tg/key", headers=H, timeout=30).json()
        from Crypto.PublicKey import RSA
        from Crypto.Cipher import PKCS1_OAEP
        sealed = base64.b64encode(PKCS1_OAEP.new(RSA.import_key(k["pubkey"]))
                                  .encrypt(b"123:TOKEN\n555000")).decode()
        tsid = httpx.post(base + "/tg/session", headers=H, json={"sealed": sealed},
                          timeout=60).json()["session_id"]

        def make_course(name, cloud, finalize=True):
            cid = httpx.post(base + "/courses", headers=H, json={
                "name": name, "delivery": "folder", "cloud": cloud, "auto": False},
                timeout=30).json()["course_id"]
            for fname, kind in (("001 - مقدمة.mp4", "video"), ("002 - مقدمة.ar.srt", "srt")):
                httpx.post(base + f"/courses/{cid}/files", params={"name": fname, "kind": kind},
                           content=b"DATA" * 2048, headers=H, timeout=60)
            if finalize:
                httpx.post(base + f"/courses/{cid}/finalize", headers=H, timeout=30)
                wait_for(lambda: httpx.get(base + f"/courses/{cid}", headers=H,
                                           timeout=30).json()["state"] == "ready", 60)
            return cid

        # ① MEGA
        c1 = make_course("دورة ميجا", "mega")
        r = httpx.post(base + f"/courses/{c1}/cloud", headers=H,
                       json={"cloud": "mega"}, timeout=30)
        print("· رد /cloud (mega):", r.status_code, r.json())
        assert r.status_code == 202 and r.json()["queued"] is True, r.text
        st = wait_for(lambda: (lambda j: j if j["mega"]["state"] in ("done", "error") else None)(
            httpx.get(base + f"/courses/{c1}", headers=H, timeout=30).json()), 120)
        assert st and st["mega"]["state"] == "done", st and st["mega"]
        print(f"✓ MEGA رفع فعلًا: {st['mega']['percent']}% · المجلد «{st['mega']['folder']}»"
              f" · ملفات {st['mega']['files_done']}/{st['mega']['files_total']}"
              f" · رابط {bool(st['mega']['folder_link'] or st['mega']['link'])}")
        assert st["mega"]["folder"] == "دورة ميجا", st["mega"]

        # ② Telegram
        c2 = make_course("دورة تيليجرام", "telegram")
        r = httpx.post(base + f"/courses/{c2}/cloud", headers=H,
                       json={"cloud": "telegram", "tg_session": tsid}, timeout=30)
        print("· رد /cloud (telegram):", r.status_code, r.json())
        assert r.status_code == 202, r.text
        st2 = wait_for(lambda: (lambda j: j if j["tg"]["state"] in ("done", "error") else None)(
            httpx.get(base + f"/courses/{c2}", headers=H, timeout=30).json()), 120)
        assert st2 and st2["tg"]["state"] == "done", st2 and st2["tg"]
        print(f"✓ Telegram أرسل فعلًا: {st2['tg']['percent']}% · رسائل {st2['tg']['messages']}"
              f" · ملفات {st2['tg']['files_done']}/{st2['tg']['files_total']}")
        assert st2["tg"]["messages"] > 0, st2["tg"]

        # ③ الرفع قبل التغليف ⇒ رسالة عربية صريحة
        c3 = make_course("باقة غير مغلّفة", "mega", finalize=False)
        r = httpx.post(base + f"/courses/{c3}/cloud", headers=H,
                       json={"cloud": "mega"}, timeout=30)
        print("· رفض الرفع قبل التغليف:", r.status_code, r.json().get("detail"))
        assert r.status_code == 409, r.text
        assert "غلّف" in r.json()["detail"] or "التنزيل" in r.json()["detail"], r.text

        # ③.٥ حالة جلسات السحابة كما يراها السيرفر
        cs = httpx.get(base + "/cloud/status", headers=H,
                       params={"mega_session": "", "tg_session": tsid}, timeout=30).json()
        print("· /cloud/status:", cs)
        assert cs["mega"]["fake"] is True, cs
        assert cs["telegram"]["session"] is True, cs

        # ④ لوحتا العمليات من استدعاء واحد
        ops = httpx.get(base + "/ops", headers=H, timeout=30).json()
        row1 = next(x for x in ops["courses"] if x["id"] == c1)
        row2 = next(x for x in ops["courses"] if x["id"] == c2)
        print(f"· /ops: {ops['count']} باقة · طابور {len(ops['queue'])}"
              f" · آخر رفع «{ops['engine']['name']}»")
        assert row1["upload"]["state"] == "done" and row1["upload"]["folder"] == "دورة ميجا", row1
        assert row2["upload"]["backend"] == "telegram" and row2["upload"]["messages"] > 0, row2
        print(f"✓ لوحة الرفع: «{row1['name']}» → {row1['upload']['backend']}"
              f" {row1['upload']['percent']}% داخل «{row1['upload']['folder']}»")
        print(f"✓ لوحة التنزيل: «{row2['name']}» → {row2['download']['state']}"
              f" {row2['download']['done']}/{row2['download']['total']}")

        print("\nكل اختبارات الرفع من السيرفر ولوحات العمليات نجحت ✅  (0.9.8)")
        return 0
    finally:
        api.terminate()
        try:
            api.wait(timeout=10)
        except Exception:
            api.kill()


if __name__ == "__main__":
    raise SystemExit(main())
