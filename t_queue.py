"""اختبار 0.9.0 — طابور MEGA المتوالٍ + الرفع التلقائي + مقاومة الانقطاع.

التشغيل: python3 t_queue.py
يستخدم محاكاة MEGA (NW_FAKE_MEGA=1) فلا حاجة لحساب حقيقي.
"""
from __future__ import annotations

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
PS, PA, KEY = 8877, 8878, "q-key-1"


def serve():
    h = functools.partial(http.server.SimpleHTTPRequestHandler, directory=FIX)
    socketserver.TCPServer.allow_reuse_address = True
    s = socketserver.TCPServer(("127.0.0.1", PS), h)
    threading.Thread(target=s.serve_forever, daemon=True).start()
    return s


def mk(base: str, H: dict, name: str, n: int, auto: bool) -> str:
    files = []
    for i in range(n):
        files.append({"title": f"{name} • الحلقة {i + 1}",
                      "url": f"http://127.0.0.1:{PS}/{quote('sample.m4a')}",
                      "bitrate": 48000, "mono": True})
    body = {"name": name, "delivery": "folder", "auto_mega": auto,
            "mega_session": "", "files": files}
    r = httpx.post(base + "/collections", headers=H, json=body, timeout=30)
    assert r.status_code == 202, r.text
    return r.json()["collection_id"]


def main() -> int:
    srv = serve()
    env = {**os.environ, "API_KEYS": f"me:{KEY}", "EXTRA_ALLOWED_HOSTS": "127.0.0.1",
           "WORK_DIR": "/tmp/nw-queue", "NW_FAKE_MEGA": "1", "NLM_DEBUG": "1"}
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

        # مساحتان (مجموعتان) — الأولى 2 حلقات والثانية 1، والرفع التلقائي مُفعَّل
        a = mk(base, H, "المساحة الأولى", 2, True)
        b = mk(base, H, "المساحة الثانية", 1, True)
        print("✓ مجموعتان:", a, b, "| auto_mega=on")

        peak_uploads = 0
        phases: dict[str, set] = {a: set(), b: set()}
        up_start: dict[str, float] = {}
        up_end: dict[str, float] = {}
        deadline = time.time() + 300
        q = None
        while time.time() < deadline:
            q = httpx.get(base + "/queue", headers=H, timeout=30).json()
            act = [i for i in q["items"] if i["phase"] == "uploading"]
            peak_uploads = max(peak_uploads, len(act))
            now = time.time()
            for i in q["items"]:
                phases[i["id"]].add(i["phase"])
                if i["phase"] == "uploading":
                    up_start.setdefault(i["id"], now)
                if i["phase"] in ("uploaded", "upload_error"):
                    up_end.setdefault(i["id"], now)
            if q["items"] and all(i["phase"] in ("uploaded", "upload_error") for i in q["items"]):
                break
            time.sleep(0.05)

        for i in q["items"]:
            print(f"· {i['name']}: phase={i['phase']} mega={i['mega_state']} "
                  f"{i['mega_files_done']}/{i['mega_files_total']} link={i['link']}")
        print("· مراحل المساحة الأولى:", sorted(phases[a]))
        print("· مراحل المساحة الثانية:", sorted(phases[b]))
        print("· أقصى عدد رفع متزامن:", peak_uploads)
        print(f"· زمن الرفع: الأولى {up_start.get(a,0):.2f}→{up_end.get(a,0):.2f} · "
              f"الثانية {up_start.get(b,0):.2f}→{up_end.get(b,0):.2f}")

        assert peak_uploads <= 1, f"تعارض! رفع متزامن = {peak_uploads}"
        assert "processing" in phases[a] and "sorting" in phases[a]
        assert "upload_queued" in phases[b] or "uploaded" in phases[b]
        assert up_start[a] < up_start[b], "المساحة الثانية بدأت قبل الأولى"
        assert up_start[b] >= up_end[a] - 0.3, (
            f"تداخل: انتهاء الأولى {up_end[a]:.2f} وبدء الثانية {up_start[b]:.2f}")
        assert all(i["phase"] == "uploaded" for i in q["items"]), q["items"]
        assert q["engine"]["sequential"] is True

        # إعادة الطلب لا تُكرّر الطابور
        again = httpx.post(base + f"/collections/{b}/mega", headers=H,
                           json={"session_id": ""}, timeout=30)
        assert again.status_code in (200, 202), again.text
        assert again.json()["state"] in ("done", "queued", "link"), again.text
        print("✓ إعادة طلب الرفع لا تُكرّره:", again.json()["state"])

        # رفع يدوي لمجموعة ثالثة (بلا auto) يعمل ويُحترم الترتيب
        c = mk(base, H, "المساحة الثالثة", 1, False)
        for _ in range(600):
            st = httpx.get(base + f"/collections/{c}", headers=H, timeout=30).json()
            if st["state"] in ("done", "partial"):
                break
            time.sleep(0.15)
        r = httpx.post(base + f"/collections/{c}/mega", headers=H,
                       json={"session_id": ""}, timeout=30)
        assert r.status_code in (200, 202), r.text
        print("✓ رفع يدوي أُضيف للطابور:", r.json())
        deadline = time.time() + 120
        while time.time() < deadline:
            q = httpx.get(base + "/queue", headers=H, timeout=30).json()
            row = next(i for i in q["items"] if i["id"] == c)
            if row["phase"] in ("uploaded", "upload_error"):
                break
            time.sleep(0.15)
        assert row["phase"] == "uploaded", row
        print("✓ الثالثة رُفعت بعد انتهاء ما قبلها:", row["phase"])

        print("\nكل اختبارات الطابور المتوالٍ نجحت ✅")
        return 0
    finally:
        api.terminate()
        srv.shutdown()


raise SystemExit(main())
