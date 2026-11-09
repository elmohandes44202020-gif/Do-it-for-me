"""اختبار 0.9.10 — إصلاح «باقة الدورة غير موجودة» بعد إعادة تشغيل السيرفر.

يعيد تمثيل عطل المستخدم بالحرف:
  ① باقة تُنشأ وتُنزَّل على السيرفر (ناجحة).
  ② السيرفر يُوقف وتُمسح الباقات من القرص (كما تفعل النسخ القديمة عند الإقلاع).
  ③ التطبيق يعيد استخدام المعرّف القديم ⇒ 404 «باقة الدورة غير موجودة … انتهت».
  ④ `POST /courses/ensure` بالمعرّف القديم ⇒ باقة جديدة `recreated=true` بمعرّف مختلف.
  ⑤ التنزيل يكتمل على الباقة الجديدة ويُغلَّف ويُرفع تلقائيًا (done).
  ⑥ ensure على باقة مغلَّفة ⇒ ينسخ باقة جديدة بدل الرسالة المتكررة.
  ⑦ مفتاح API آخر ⇒ 409 «لمفتاح API آخر» (لا يُسرَّب ولا يُخلط).

التشغيل: python3 t_ensure.py
"""
from __future__ import annotations

import os
import shutil
import subprocess
import sys
import time

import httpx

from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from functools import partial
import threading

VTT_AR = """WEBVTT

00:00:00.000 --> 00:00:02.000
مقدمة الدرس
"""


def build_sample_mp4(path: str) -> None:
    import av
    c = av.open(path, "w")
    st = c.add_stream("mpeg4", rate=10)
    st.width, st.height, st.pix_fmt = 160, 120, "yuv420p"
    for i in range(20):
        f = av.VideoFrame(160, 120, "yuv420p")
        f.pts = i
        for pl in f.planes:
            pl.update(b"\x80" * pl.buffer_size)
        for pkt in st.encode(f):
            c.mux(pkt)
    for pkt in st.encode():
        c.mux(pkt)
    c.close()


PORT_FILES = 8878

HERE = os.path.dirname(os.path.abspath(__file__))
PA = 8877
KEY = "ensure-key-1"
WORK = "/tmp/nw-ensure"


def start():
    env = {**os.environ, "API_KEYS": f"me:{KEY},other:{KEY}-b", "WORK_DIR": WORK,
           "NW_FAKE_MEGA": "1", "NW_FAKE_TG": "1", "COURSE_FETCH_ALLOW_LOCAL": "1"}
    proc = subprocess.Popen([sys.executable, "-m", "uvicorn", "main:app", "--host",
                             "127.0.0.1", "--port", str(PA), "--log-level", "warning"],
                            cwd=HERE, env=env)
    for _ in range(200):
        try:
            if httpx.get(f"http://127.0.0.1:{PA}/healthz", timeout=1).status_code == 200:
                return proc
        except Exception:
            time.sleep(0.2)
    raise RuntimeError("السيرفر لم يبدأ")


def main() -> int:
    shutil.rmtree(WORK, ignore_errors=True)
    base = f"http://127.0.0.1:{PA}"
    H = {"X-Api-Key": KEY}
    files_dir = "/tmp/nw-ensure-www"
    os.makedirs(files_dir, exist_ok=True)
    build_sample_mp4(os.path.join(files_dir, "lecture.mp4"))
    with open(os.path.join(files_dir, "lecture.vtt"), "w", encoding="utf-8") as fh:
        fh.write(VTT_AR)
    httpd = ThreadingHTTPServer(("127.0.0.1", PORT_FILES),
                                partial(SimpleHTTPRequestHandler, directory=files_dir))
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    fl = f"http://127.0.0.1:{PORT_FILES}"
    item = {"name": "الوحدة 1 - محاضرة 1.mp4", "kind": "video", "url": f"{fl}/lecture.mp4",
            "subtitle": {"url": f"{fl}/lecture.vtt", "format": "vtt"}}

    api = start()
    cid_old = ""
    try:
        print("· إصدار السيرفر:", httpx.get(base + "/healthz").json()["version"])
        cid_old = httpx.post(base + "/courses", headers=H, timeout=30, json={
            "name": "دورة الاختبار", "delivery": "folder", "cloud": "mega", "auto": True}).json()["course_id"]
        r = httpx.post(base + f"/courses/{cid_old}/fetch", headers=H, timeout=60,
                       json={"items": [item], "cookie": "", "user_agent": "", "embed": True, "text": True})
        print("① تنزيل أول مرة:", r.status_code, r.json().get("state"))
        assert r.status_code == 202, r.text
        st = None
        for _ in range(600):
            st = httpx.get(base + f"/courses/{cid_old}", headers=H, timeout=30).json()
            if st["fetch"]["state"] in ("done", "error"):
                break
            time.sleep(0.2)
        print("   التنزيل:", st["fetch"]["state"], "· ملفات الباقة:", len(st["files"]),
              "· أخطاء:", st["fetch"]["errors_count"])
        assert st["fetch"]["state"] == "done" and len(st["files"]) >= 2, st
        httpx.post(base + f"/courses/{cid_old}/finalize", headers=H, timeout=30)
        for _ in range(600):
            st = httpx.get(base + f"/courses/{cid_old}", headers=H, timeout=30).json()
            if st["state"] == "ready" and (st.get("mega") or {}).get("state") == "done":
                break
            time.sleep(0.2)
        print("   بعد التغليف والرفع التلقائي:", st["state"], "/", (st.get("mega") or {}).get("state"))
        assert st["state"] == "ready"
    finally:
        api.terminate()
        api.wait(timeout=15)

    # ② محاكاة ما يفعله النشر/النسخة القديمة: الباقات تختفي
    shutil.rmtree(os.path.join(WORK, "courses"), ignore_errors=True)
    api = start()
    try:
        r = httpx.post(base + f"/courses/{cid_old}/fetch", headers=H, timeout=30,
                       json={"items": [item], "cookie": "", "user_agent": "", "embed": True, "text": True})
        msg = (r.json() or {}).get("detail", "")
        print("③ المعرّف القديم:", r.status_code, "·", msg[:70])
        assert r.status_code == 404 and "انتهت" in msg, r.text

        en = httpx.post(base + "/courses/ensure", headers=H, timeout=30, json={
            "course_id": cid_old, "name": "دورة الاختبار", "delivery": "folder",
            "cloud": "mega", "auto": True}).json()
        print("④ ensure:", en["recreated"], "·", en["note"], "· معرّف جديد:",
              en["course_id"] != cid_old)
        assert en["recreated"] is True and en["course_id"] and en["course_id"] != cid_old, en
        cid = en["course_id"]

        # ensure مرة أخرى على الباقة الحيّة ⇒ لا ينسخ
        en2 = httpx.post(base + "/courses/ensure", headers=H, timeout=30, json={
            "course_id": cid, "name": "دورة الاختبار", "delivery": "folder", "auto": True}).json()
        print("   ensure على باقة حيّة ⇒ recreated:", en2["recreated"], "· نفس المعرّف:",
              en2["course_id"] == cid)
        assert en2["recreated"] is False and en2["course_id"] == cid, en2

        r = httpx.post(base + f"/courses/{cid}/fetch", headers=H, timeout=60,
                       json={"items": [item], "cookie": "", "user_agent": "", "embed": True, "text": True})
        assert r.status_code == 202, r.text
        for _ in range(600):
            st = httpx.get(base + f"/courses/{cid}", headers=H, timeout=30).json()
            if st["fetch"]["state"] in ("done", "error"):
                break
            time.sleep(0.2)
        httpx.post(base + f"/courses/{cid}/finalize", headers=H, timeout=30)
        done = None
        for _ in range(600):
            st = httpx.get(base + f"/courses/{cid}", headers=H, timeout=30).json()
            done = (st.get("mega") or {}).get("state")
            if st["state"] == "ready" and done == "done":
                break
            time.sleep(0.2)
        print("⑤ تنزيل + تغليف + رفع تلقائي على الباقة الجديدة:", st["state"], "/", done,
              "· ملفات:", st.get("count"))
        assert st["state"] == "ready" and done == "done", st

        en3 = httpx.post(base + "/courses/ensure", headers=H, timeout=30, json={
            "course_id": cid, "name": "دورة الاختبار", "delivery": "folder", "auto": True}).json()
        print("⑥ ensure على باقة مغلَّفة ⇒ recreated:", en3["recreated"])
        assert en3["recreated"] is True and en3["course_id"] != cid, en3

    finally:
        api.terminate()
        api.wait(timeout=15)

    # ⑦ بمفتاح آخر: 409 صريحة
    api = start()
    try:
        rr = httpx.post(base + "/courses/ensure", headers={"X-Api-Key": KEY + "-b"}, timeout=30,
                        json={"course_id": cid})
        print("⑦ مفتاح آخر:", rr.status_code, "·", rr.json().get("detail", "")[:50])
        assert rr.status_code == 409 and "مفتاح API آخر" in rr.json()["detail"], rr.text
    finally:
        api.terminate()
        api.wait(timeout=15)

    print("\nكل اختبارات «الباقة انتهت ← باقة جديدة تلقائيًا» نجحت ✅  (0.9.10 / 3.71)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
