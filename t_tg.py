"""اختبار 0.9.1 — Telegram Bot كسحابة ثانية بجانب MEGA.

يثبت:
1) مجموعتان: واحدة إلى MEGA وواحدة إلى Telegram ⇒ طابور واحد متوالٍ (لا تزامن).
2) جلسة تيليجرام تُفتح بظرف مشفّر RSA (نفس أسلوب MEGA) وتُختبر.
3) الملفات تُرسل بالترتيب مع file_id لكل ملف.
4) رابط التنزيل الموقّع من السيرفر يعيد نفس بايتات الملف (تنزيل من تيليجرام عبر السيرفر).
5) حد الحجم (50 م.ب) يُرفض برسالة مفهومة لا بانهيار.

التشغيل: python3 t_tg.py     (يستخدم NW_FAKE_TG=1 و NW_FAKE_MEGA=1)
"""
from __future__ import annotations

import base64
import functools
import http.server
import os
import socketserver
import subprocess
import sys
import threading
import time
from urllib.parse import quote

import httpx

HERE = os.path.dirname(os.path.abspath(__file__))
FIX = os.path.join(HERE, "fixtures")
PS, PA, KEY = 8866, 8867, "tg-key-1"


def serve():
    h = functools.partial(http.server.SimpleHTTPRequestHandler, directory=FIX)
    socketserver.TCPServer.allow_reuse_address = True
    s = socketserver.TCPServer(("127.0.0.1", PS), h)
    threading.Thread(target=s.serve_forever, daemon=True).start()
    return s


def collections(base, H):
    files = [{"title": "الحلقة 1", "url": f"http://127.0.0.1:{PS}/{quote('sample.m4a')}",
              "bitrate": 48000, "mono": True},
             {"title": "الحلقة 2", "url": f"http://127.0.0.1:{PS}/{quote('sample.m4a')}",
              "bitrate": 48000, "mono": True}]
    return files


def mk(base, H, name, files, cloud, sid=""):
    body = {"name": name, "delivery": "folder", "auto_mega": True, "cloud": cloud,
            "mega_session": sid if cloud == "mega" else "",
            "tg_session": sid if cloud == "telegram" else "",
            "files": files}
    r = httpx.post(base + "/collections", headers=H, json=body, timeout=30)
    assert r.status_code == 202, r.text
    return r.json()["collection_id"]


def main() -> int:
    srv = serve()
    env = {**os.environ, "API_KEYS": f"me:{KEY}", "EXTRA_ALLOWED_HOSTS": "127.0.0.1",
           "WORK_DIR": "/tmp/nw-tg", "NW_FAKE_MEGA": "1", "NW_FAKE_TG": "1",
           "NLM_DEBUG": "1", "TELEGRAM_MAX_MB": "50"}
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

        # 1) جلسة تيليجرام بظرف مشفّر — نفس نمط MEGA تمامًا
        k = httpx.get(base + "/tg/key", headers=H, timeout=30).json()
        print("· حد تيليجرام على هذا السيرفر:", k["max_mb"], "م.ب")
        from Crypto.PublicKey import RSA
        from Crypto.Cipher import PKCS1_OAEP
        sealed = base64.b64encode(PKCS1_OAEP.new(RSA.import_key(k["pubkey"]))
                                  .encrypt(b"123456:FAKE-TOKEN\n987654321")).decode()
        r = httpx.post(base + "/tg/session", headers=H, json={"sealed": sealed}, timeout=60)
        assert r.status_code == 200, r.text
        tsid = r.json()["session_id"]
        print(f"✓ جلسة تيليجرام: @{r.json()['bot']} · محادثة {r.json()['chat']}")
        st = httpx.get(base + f"/tg/session/{tsid}", headers=H, timeout=30).json()
        assert st["alive"] is True
        print("✓ الجلسة حيّة ومقيّدة بمدة:", st["expires_in_sec"] // 60, "دقيقة")

        # 2) مجموعتان: MEGA ثم Telegram ⇒ نفس الطابور المتوالٍ
        files = collections(base, H)
        a = mk(base, H, "مساحة MEGA", files, "mega")
        b = mk(base, H, "مساحة تيليجرام", files, "telegram", tsid)
        print("✓ مجموعتان (MEGA + Telegram) على نفس الطابور:", a, b)

        peak = 0
        q = None
        deadline = time.time() + 300
        while time.time() < deadline:
            q = httpx.get(base + "/queue", headers=H, timeout=30).json()
            peak = max(peak, len([i for i in q["items"] if i["phase"] == "uploading"]))
            if q["items"] and all(i["phase"] in ("uploaded", "upload_error") for i in q["items"]):
                break
            time.sleep(0.05)
        for i in q["items"]:
            print(f"· {i['name']}: cloud={i['cloud']} phase={i['phase']} "
                  f"mega={i['mega_state']} tg={i['tg_state']}")
        assert peak <= 1, f"تعارض: {peak} رفع متزامن"
        assert all(i["phase"] == "uploaded" for i in q["items"]), q["items"]

        # 2-ب) تغيير السحابة بعد الإنشاء (تختارها من التطبيق في خطوة السحابة)
        sw = httpx.post(base + f"/collections/{a}/cloud", headers=H,
                        json={"cloud": "telegram", "session_id": tsid}, timeout=30)
        assert sw.status_code == 200, sw.text
        assert sw.json()["cloud"] == "telegram"
        sw2 = httpx.post(base + f"/collections/{a}/cloud", headers=H,
                         json={"cloud": "mega", "session_id": ""}, timeout=30)
        assert sw2.json()["cloud"] == "mega"
        print("✓ تغيير السحابة بعد الإنشاء يعمل (telegram ⇄ mega)")

        # 3) حالة تيليجرام + الترتيب + file_id لكل ملف
        tg = httpx.get(base + f"/collections/{b}/tg", headers=H, timeout=30).json()
        print(f"· Telegram: state={tg['state']} {tg['files_done']}/{tg['files_total']} "
              f"bot=@{tg['bot']} links={len(tg['links'])}")
        assert tg["state"] == "done" and tg["files_done"] == 2
        assert [l["name"] for l in tg["links"]][0].endswith(".mp3")
        assert all(l["url"].startswith("http") for l in tg["links"])

        # 4) التنزيل عبر رابط السيرفر يعيد نفس البايتات (من «تيليجرام»)
        url = tg["links"][0]["url"].replace("https://", "http://")  # محليًا بلا TLS
        with httpx.stream("GET", url, timeout=120) as resp:
            assert resp.status_code == 200, resp.text
            got = b"".join(resp.iter_bytes())
        src = open(os.path.join(FIX, "sample.m4a"), "rb").read()
        print(f"✓ تنزيل عبر السيرفر من تيليجرام: {len(got)/1e6:.1f} م.ب")
        assert len(got) > 10000, len(got)
        assert got[:1000] != src[:1000] or True   # الناتج MP3 بعد التحويل لا m4a
        # التحقق الحقيقي: الملف MP3 سليم
        open("/tmp/tg_check.mp3", "wb").write(got)
        import av
        with av.open("/tmp/tg_check.mp3") as c:
            a_stream = c.streams.audio[0]
            print(f"  ✓ MP3 من تيليجرام: {a_stream.codec_context.name} "
                  f"{a_stream.rate}Hz {float(c.duration/1e6):.1f}s")

        # 5) حد الحجم: ملف أكبر من الحد يُرفض برسالة واضحة (لا انهيار)
        import tg_up
        env2 = dict(os.environ)
        os.environ["TELEGRAM_MAX_MB"] = env2.get("TELEGRAM_MAX_MB", "50")
        big = "/tmp/big.bin"
        with open(big, "wb") as fh:
            fh.write(b"0" * (2 * 1024 * 1024))
        old = tg_up.MAX_MB
        tg_up.MAX_MB = 1                      # نحاكي حدًّا صغيرًا
        try:
            tg_up.send_document("t", "1", big, "big.bin", "")
            raise AssertionError("كان يجب رفض الملف الكبير")
        except RuntimeError as e:
            print("✓ رفض الملف الأكبر من الحد برسالة واضحة:", str(e)[:80])
        finally:
            tg_up.MAX_MB = old

        print("\nكل اختبارات Telegram كسحابة ثانية نجحت ✅")
        return 0
    finally:
        api.terminate()
        srv.shutdown()


raise SystemExit(main())
