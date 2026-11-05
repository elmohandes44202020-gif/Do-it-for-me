"""
اختبار شامل محلي للخدمة: ينزّل الملفات من سيرفر HTTP محلي، يحوّلها، ويبني الـZIP.
التشغيل:  python test_e2e.py
"""
from __future__ import annotations

import functools
import http.server
import json
import os
import shutil
import socketserver
import subprocess
import sys
import threading
import time
from urllib.parse import quote

import httpx

HERE = os.path.dirname(os.path.abspath(__file__))
FIX = os.path.join(HERE, "fixtures")
PORT_STATIC = 8899
PORT_API = 8900
KEY = "test-key-123"


def serve_static():
    handler = functools.partial(http.server.SimpleHTTPRequestHandler, directory=FIX)
    socketserver.TCPServer.allow_reuse_address = True
    httpd = socketserver.TCPServer(("127.0.0.1", PORT_STATIC), handler)
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    return httpd


def main() -> int:
    print("· تشغيل سيرفر ملفات محلي")
    httpd = serve_static()

    env = {
        **os.environ,
        "API_KEYS": f"me:{KEY}",
        "EXTRA_ALLOWED_HOSTS": "127.0.0.1,localhost",
        "TTL_MINUTES": "5",
        "NLM_DEBUG": "1",
        "WORK_DIR": "/tmp/nlm-test",
    }
    print("· تشغيل التطبيق على المنفذ", PORT_API)
    api = subprocess.Popen(
        [sys.executable, "-m", "uvicorn", "main:app", "--host", "127.0.0.1",
         "--port", str(PORT_API), "--log-level", "warning"],
        cwd=HERE, env=env,
    )

    base = f"http://127.0.0.1:{PORT_API}"
    H = {"X-Api-Key": KEY}

    try:
        for _ in range(100):
            try:
                r = httpx.get(base + "/healthz", timeout=1)
                if r.status_code == 200:
                    break
            except Exception:
                time.sleep(0.2)
        else:
            print("✗ التطبيق لم يبدأ")
            return 1

        health = httpx.get(base + "/healthz").json()
        print("✓ healthz:", json.dumps(health["settings"], ensure_ascii=False))

        # 1) رفض بدون مفتاح
        r = httpx.post(base + "/jobs", json={"files": [{"url": "https://x.y/z"}]})
        assert r.status_code == 401, r.status_code
        print("✓ رفض الطلب بدون مفتاح API (401)")

        # 2) رفض نطاق غير مسموح (حماية SSRF)
        r = httpx.post(base + "/jobs", headers=H, json={"files": [{"url": "https://evil.example/a.m4a"}]})
        assert r.status_code == 400, r.text
        print("✓ رفض نطاق غير مسموح (400)")

        urls = [
            ("https://note.example".replace("note.example", "127.0.0.1"), "الحلقة الأولى: مقدمة"),
            (f"http://127.0.0.1:{PORT_STATIC}/Episode-02.m4a", "Episode 02 - Deep Dive"),
        ]
        files = [
            {"title": "الحلقة الأولى: مقدمة",
             "url": f"http://127.0.0.1:{PORT_STATIC}/{quote('الحلقة-الأولى.m4a')}"},
            {"title": "Episode 02 - Deep Dive",
             "url": f"http://127.0.0.1:{PORT_STATIC}/Episode-02.m4a"},
        ]

        # 3) probe
        r = httpx.post(base + "/probe", headers=H, json={"url": files[0]["url"]}, timeout=30)
        print("✓ probe:", json.dumps(r.json(), ensure_ascii=False))

        # 4) إنشاء مهمة
        t0 = time.time()
        r = httpx.post(base + "/jobs", headers=H,
                       json={"files": files, "zip_name": "notebooklm-audio"})
        assert r.status_code == 202, r.text
        job_id = r.json()["job_id"]
        print("✓ أنشئت المهمة:", job_id)

        # 5) متابعة الحالة
        state = None
        for _ in range(300):
            s = httpx.get(f"{base}/jobs/{job_id}", headers=H).json()
            if s["state"] != state:
                state = s["state"]
                print(f"  ← الحالة: {state} ({s['progress_percent']}%)")
            if state in ("done", "error"):
                break
            time.sleep(0.2)
        elapsed = time.time() - t0
        print("✓ انتهت في %.1fs" % elapsed)
        print(json.dumps({k: v for k, v in httpx.get(f'{base}/jobs/{job_id}', headers=H).json().items()
                          if k != 'files'}, ensure_ascii=False))

        # 6) تنزيل الـZIP والتحقق منه
        zpath = "/tmp/out.zip"
        with httpx.stream("GET", f"{base}/jobs/{job_id}/zip", headers=H, timeout=120) as r:
            assert r.status_code == 200, r.status_code
            total = int(r.headers.get("x-total-bytes", 0))
            clen = int(r.headers.get("content-length", 0))
            assert clen == total, (clen, total)
            got = 0
            with open(zpath, "wb") as fh:
                for b in r.iter_bytes(1 << 16):
                    fh.write(b)
                    got += len(b)
        print(f"✓ تنزيل ZIP: {got/1e6:.1f} MB (متوقع {total/1e6:.1f} MB)")
        assert got == total, (got, total)

        import zipfile
        with zipfile.ZipFile(zpath) as z:
            bad = z.testzip()
            assert bad is None, f"ملف تالف داخل الأرشيف: {bad}"
            names = z.namelist()
            print("✓ الأرشيف سليم، المحتوى:", names)
            z.extractall("/tmp/unz")

        # 7) تحقق من صلاحية الـMP3 داخليًا
        os.makedirs("/tmp/unz", exist_ok=True)
        import av
        for name in sorted(os.listdir("/tmp/unz")):
            with av.open(os.path.join("/tmp/unz", name)) as c:
                a = c.streams.audio[0]
                print(f"  ✓ {name}: {a.codec_context.name} {a.rate}Hz {a.layout.name} "
                      f"{float(c.duration/1e6):.1f}s")

        # 8) اختبار ملف فردي + حذف المهمة
        r = httpx.get(f"{base}/jobs/{job_id}/files/1", headers=H)
        print("✓ تحميل ملف منفرد:", r.status_code, r.headers.get("content-type"), f"{len(r.content)/1e6:.1f} MB")
        print("✓ حذف المهمة:", httpx.delete(f"{base}/jobs/{job_id}", headers=H).json())

        print("\nكل الاختبارات نجحت ✅")
        return 0
    finally:
        api.terminate()
        httpd.shutdown()


if __name__ == "__main__":
    raise SystemExit(main())
