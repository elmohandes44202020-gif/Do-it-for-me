"""اختبار 0.9.2 — باقات Coursera: MP4 (عربية مدمجة) + SRT/TXT ⇒ ZIP أو مجلد باسم الدورة
ثم الرفع إلى MEGA أو Telegram عبر نفس الطابور المتوالٍ.

التشغيل: python3 t_course.py     (NW_FAKE_MEGA=1 و NW_FAKE_TG=1)
"""
from __future__ import annotations

import base64
import io as _io
import os
import subprocess
import sys
import time
import zipfile

import httpx

HERE = os.path.dirname(os.path.abspath(__file__))
PA, KEY = 8855, "course-key-1"


def main() -> int:
    env = {**os.environ, "API_KEYS": f"me:{KEY}", "WORK_DIR": "/tmp/nw-course",
           "NW_FAKE_MEGA": "1", "NW_FAKE_TG": "1", "NLM_DEBUG": "1"}
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

        # جلسة تيليجرام (ظرف مشفّر) — لاختبار الرفع للسحابة الثانية
        k = httpx.get(base + "/tg/key", headers=H, timeout=30).json()
        from Crypto.PublicKey import RSA
        from Crypto.Cipher import PKCS1_OAEP
        sealed = base64.b64encode(PKCS1_OAEP.new(RSA.import_key(k["pubkey"]))
                                  .encrypt(b"123:TOKEN\n555000")).decode()
        tsid = httpx.post(base + "/tg/session", headers=H, json={"sealed": sealed},
                          timeout=60).json()["session_id"]

        # باقة 1: ZIP باسم الدورة + رفع إلى MEGA التلقائي
        r = httpx.post(base + "/courses", headers=H, json={
            "name": "أساسيات تعلم الآلة", "delivery": "zip", "cloud": "mega",
            "auto": True}, timeout=30)
        c1 = r.json()["course_id"]
        print("✓ باقة ZIP:", r.json()["name"], "|", c1)

        files = [
            ("001 - مقدمة.mp4", b"MP4DATA" * 5000, "video"),
            ("001 - مقدمة.ar.srt", b"1\n00:00:00,000 --> 00:00:02,000\n\xd9\x85\xd9\x82\xd8\xaf\xd9\x85\xd8\xa9\n", "srt"),
            ("002 - الانحدار.mp4", b"MP4SECOND" * 4000, "video"),
            ("002 - الانحدار.ar.srt", b"1\n00:00:01,000 --> 00:00:03,000\n\xd8\xa7\xd9\x84\xd8\xa7\xd9\x86\xd8\xad\xd8\xaf\xd8\xa7\xd8\xb1\n", "srt"),
            ("003 - ملاحظات.txt", b"notes", "txt"),
        ]
        for name, data, kind in files:
            rr = httpx.post(base + f"/courses/{c1}/files",
                            params={"name": name, "kind": kind},
                            content=data, headers=H, timeout=60)
            assert rr.status_code == 201, rr.text
            print(f"  ⬆ {name} → {rr.json()['size']} بايت")

        assert httpx.post(base + f"/courses/{c1}/finalize", headers=H,
                          timeout=30).json()["state"] == "packing"
        st = None
        deadline = time.time() + 120
        while time.time() < deadline:
            st = httpx.get(base + f"/courses/{c1}", headers=H, timeout=30).json()
            if st["state"] in ("ready", "error") and st["mega"]["state"] in ("done", "error"):
                break
            time.sleep(0.2)
        print(f"· الحالة: {st['state']} · ZIP {st['zip_bytes']/1024:.0f} ك.ب "
              f"· MEGA {st['mega']['state']} {st['mega']['percent']}%")
        assert st["state"] == "ready", st
        assert st["name"] == "أساسيات تعلم الآلة"
        assert st["mega"]["state"] == "done", st["mega"]
        assert st["mega"]["folder"] == "أساسيات تعلم الآلة"
        print("✓ رُفعت تلقائيًا إلى MEGA داخل مجلد باسم الدورة")

        # محتوى الـZIP: 5 ملفات مرقّمة وباسم الدورة
        url = st["zip_url"].replace("https://", "http://")
        with httpx.stream("GET", url, headers=H, timeout=120) as resp:
            assert resp.status_code == 200
            blob = b"".join(resp.iter_bytes())
        z = zipfile.ZipFile(_io.BytesIO(blob))
        names = sorted(z.namelist())
        print("✓ محتوى ZIP:", names)
        assert len(names) == 5
        assert any(n.endswith(".mp4") for n in names)
        assert any(n.endswith(".srt") for n in names)
        assert any(n.endswith(".txt") for n in names)
        srt = z.read([n for n in names if n.endswith(".srt")][0]).decode("utf-8")
        assert "-->" in srt
        print("✓ ملف الترجمة SRT داخل الـZIP سليم")

        # تنزيل ملف مفرد من الباقة
        one = httpx.get(base + f"/courses/{c1}/file/1", headers=H, timeout=60)
        assert one.status_code == 200 and len(one.content) == len(files[0][1])
        print("✓ تنزيل ملف مفرد يعمل:", len(one.content), "بايت")

        # باقة 2: مجلد باسم الدورة + تيليجرام
        r2 = httpx.post(base + "/courses", headers=H, json={
            "name": "دورة الشبكات", "delivery": "folder", "cloud": "telegram",
            "auto": True, "tg_session": tsid}, timeout=30).json()
        c2 = r2["course_id"]
        for name, data, kind in [("001 - الدرس الأول.mp4", b"X" * 20000, "video"),
                                 ("001 - الدرس الأول.ar.srt", b"1\n00:00:00,000 --> 00:00:01,000\nx\n", "srt")]:
            assert httpx.post(base + f"/courses/{c2}/files", params={"name": name, "kind": kind},
                              content=data, headers=H, timeout=60).status_code == 201
        httpx.post(base + f"/courses/{c2}/finalize", headers=H, timeout=30)
        deadline = time.time() + 120
        while time.time() < deadline:
            st2 = httpx.get(base + f"/courses/{c2}", headers=H, timeout=30).json()
            if st2["state"] == "ready" and st2["tg"]["state"] in ("done", "error"):
                break
            time.sleep(0.2)
        print(f"· باقة المجلد: {st2['state']} · Telegram {st2['tg']['state']} "
              f"{st2['tg']['files_done']}/{st2['tg']['files_total']}")
        assert st2["tg"]["state"] == "done" and st2["tg"]["files_done"] == 2

        # الطابور العام يعرض الباقتين بنفس المحرّك المتوالٍ
        q = httpx.get(base + "/queue", headers=H, timeout=30).json()
        ids = {i["id"] for i in q["items"]}
        assert c1 in ids and c2 in ids, (ids, c1, c2)
        print("✓ الباقتان تظهران في نفس الطابور المتوالٍ:", q["engine"]["sequential"])

        print("\nكل اختبارات معالج كورسيرا (ZIP/مجلد + MEGA/Telegram) نجحت ✅")
        return 0
    finally:
        api.terminate()


raise SystemExit(main())
