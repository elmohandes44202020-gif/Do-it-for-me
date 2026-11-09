"""اختبار 0.9.4 — التنزيل **على السيرفر** لباقات كورسيرا (بلا تنزيل على الهاتف).

السيناريو الحقيقي: التطبيق يكتشف روابط ڤيديو + ترجمة VTT عربية ويرسلها للسيرفر.
هنا نشغّل سيرفر HTTP محليًا يقدّم نفس الملفات، ثم:

1. إنشاء باقة ⇒ POST /courses/{cid}/fetch ⇒ السيرفر ينزّل الڤيديو والترجمة.
2. الترجمة تُحوَّل من VTT إلى SRT عربيًا، وتُدمج داخل MP4 بـ ffmpeg (mov_text)،
   مع حفظ `.ar.srt` منفصلًا أيضًا.
3. ملف فاشل (404) لا يُسقط الدفعة: خطأ واحد مسجَّل والباقي ناجح.
4. اختيار التسليم (ZIP) ⇒ finalize ⇒ ZIP بالترتيب `001 - ...` ثم رفع إلى MEGA.
5. لا شيء نُزّل على الهاتف: كل الملفات في مجلد الباقة على السيرفر فقط.

التشغيل: python3 t_fetch.py
"""
from __future__ import annotations

import io as _io
import os
import subprocess
import sys
import threading
import time
import zipfile
from functools import partial
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer

import httpx

HERE = os.path.dirname(os.path.abspath(__file__))
PORT_API = 8881
PORT_FILES = 8882
KEY = "fetch-key-1"
WORK = "/tmp/nw-fetch"

VTT_AR = """WEBVTT

00:00:00.000 --> 00:00:02.000
مقدمة الدرس

00:00:02.100 --> 00:00:05.000
الانحدار الخطي
"""


def build_sample_mp4(path: str) -> None:
    import av
    c = av.open(path, "w")
    s = c.add_stream("mpeg4", rate=10)
    s.width, s.height, s.pix_fmt = 160, 120, "yuv420p"
    for i in range(30):
        f = av.VideoFrame(160, 120, "yuv420p")
        f.pts = i
        for p in f.planes:
            p.update(b"\x80" * p.buffer_size)
        for pkt in s.encode(f):
            c.mux(pkt)
    for pkt in s.encode():
        c.mux(pkt)
    c.close()


def serve_files(root: str) -> ThreadingHTTPServer:
    handler = partial(SimpleHTTPRequestHandler, directory=root)
    httpd = ThreadingHTTPServer(("127.0.0.1", PORT_FILES), handler)
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    return httpd


def main() -> int:
    # مجلد الملفات خارج WORK_DIR (السيرفر ينظّف WORK_DIR عند البدء)
    files_dir = "/tmp/nw-fetch-www"
    os.makedirs(files_dir, exist_ok=True)
    build_sample_mp4(os.path.join(files_dir, "lecture.mp4"))
    with open(os.path.join(files_dir, "lecture.vtt"), "w", encoding="utf-8") as fh:
        fh.write(VTT_AR)
    httpd = serve_files(files_dir)

    env = {**os.environ, "API_KEYS": f"me:{KEY}", "WORK_DIR": WORK,
           "NW_FAKE_MEGA": "1", "NW_FAKE_TG": "1", "COURSE_FETCH_ALLOW_LOCAL": "1"}
    api = subprocess.Popen([sys.executable, "-m", "uvicorn", "main:app", "--host",
                            "127.0.0.1", "--port", str(PORT_API), "--log-level", "warning"],
                           cwd=HERE, env=env)
    base = f"http://127.0.0.1:{PORT_API}"
    files = f"http://127.0.0.1:{PORT_FILES}"
    H = {"X-Api-Key": KEY}
    try:
        for _ in range(150):
            try:
                if httpx.get(base + "/healthz", timeout=1).status_code == 200:
                    break
            except Exception:
                time.sleep(0.2)
        print("· إصدار السيرفر:", httpx.get(base + "/healthz").json()["version"])

        cid = httpx.post(base + "/courses", headers=H, json={
            "name": "دورة الأربع وحدات", "delivery": "zip",
            "cloud": "mega", "auto": True}, timeout=30).json()["course_id"]
        print("✓ باقة جديدة:", cid)

        r = httpx.post(base + f"/courses/{cid}/fetch", headers=H, timeout=30, json={
            "cookie": "CAUTH=abc; other=1",
            "user_agent": "NoteWorm-Test",
            "embed": True,
            "text": True,                      # 0.9.8: ملف .ar.txt نصي لكل محاضرة
            "items": [
                {"name": "الوحدة 1 - مقدمة.mp4", "kind": "video", "url": f"{files}/lecture.mp4",
                 "subtitle": {"url": f"{files}/lecture.vtt", "format": "vtt"}},
                {"name": "الوحدة 2 - الانحدار.mp4", "kind": "video", "url": f"{files}/lecture.mp4",
                 "subtitle": {"url": f"{files}/lecture.vtt", "format": "vtt"}},
                {"name": "الوحدة 3 - التصنيف.mp4", "kind": "video", "url": f"{files}/lecture.mp4",
                 "subtitle": {"url": f"{files}/lecture.vtt", "format": "vtt"}},
                {"name": "الوحدة 4 - محذوف.mp4", "kind": "video", "url": f"{files}/missing.mp4"},
            ]})
        assert r.status_code == 202, r.text
        print("✓ بدأ التنزيل على السيرفر:", r.json())

        st = None
        deadline = time.time() + 180
        while time.time() < deadline:
            st = httpx.get(base + f"/courses/{cid}", headers=H, timeout=30).json()
            if st["fetch"]["state"] in ("done", "error"):
                break
            time.sleep(0.3)
        fetch = st["fetch"]
        print("· حالة التنزيل:", fetch["state"], f'{fetch["done"]}/{fetch["total"]}',
              "· الدمج:", fetch["mux"], "· دُمجت:", fetch["muxed"])
        print("· الملفات على السيرفر:", [(f["idx"], f["name"], f["kind"]) for f in st["files"]])
        if fetch["errors"]:
            print("· أخطاء الملفات:", fetch["errors"])
        assert fetch["state"] in ("done", "error")
        assert fetch["done"] == 4, fetch
        names = [f["name"] for f in st["files"]]
        # أربع وحدات: لكل محاضرة MP4 مدموج + SRT + TXT ⇒ 3 ملفات × 3 محاضرات ناجحة
        assert len(st["files"]) == 9, names
        assert names[0] == "الوحدة 1 - مقدمة.mp4", names
        assert names[1] == "الوحدة 1 - مقدمة.ar.srt", names
        assert names[2] == "الوحدة 1 - مقدمة.ar.txt", names
        assert names[3] == "الوحدة 2 - الانحدار.mp4", names
        assert fetch["muxed"] == 3, fetch
        assert len(fetch["errors"]) == 1, fetch["errors"]
        assert "محذوف" in fetch["errors"][0]["name"]

        # الترجمة المنفصلة عربية وسليمة التوقيت
        srt = httpx.get(base + f"/courses/{cid}/file/2", headers=H, timeout=60)
        assert srt.status_code == 200
        text = srt.content.decode("utf-8")
        assert "-->" in text and "مقدمة الدرس" in text, text[:200]
        assert "," in text.split("-->")[0], "SRT يجب أن يستخدم الفاصلة"
        print("✓ ملف SRT عربي سليم (VTT ⇒ SRT):", text.splitlines()[1])

        # الملف النصي: عربي بلا توقيتات ولا أرقام أسطر
        txt = httpx.get(base + f"/courses/{cid}/file/3", headers=H, timeout=60)
        assert txt.status_code == 200, txt.status_code
        body = txt.content.decode("utf-8")
        print("✓ ملف نصي .ar.txt:", repr(body[:60]))
        assert "مقدمة الدرس" in body, body[:200]
        assert "-->" not in body and not any(l.strip().isdigit() for l in body.splitlines())

        # هل دُمجت الترجمة فعلًا داخل MP4؟
        blob = httpx.get(base + f"/courses/{cid}/file/1", headers=H, timeout=120).content
        if fetch["mux"] == "ffmpeg":
            import av
            open("/tmp/_muxed_check.mp4", "wb").write(blob)
            c = av.open("/tmp/_muxed_check.mp4")
            kinds = [s.type for s in c.streams]
            print("✓ مسارات الـMP4 النازل من السيرفر:", kinds)
            assert "subtitle" in kinds, kinds
            assert "video" in kinds
            c.close()
        else:
            print("· ffmpeg غير متاح في هذه البيئة — الترجمة مرفقة كملف .srt (مسار بديل)")

        # التسليم: ZIP ثم الطلب من السيرفر
        d = httpx.post(base + f"/courses/{cid}/delivery", headers=H, timeout=30,
                       json={"delivery": "zip"})
        assert d.json()["delivery"] == "zip", d.text
        assert httpx.post(base + f"/courses/{cid}/finalize", headers=H,
                          timeout=30).json()["state"] == "packing"
        deadline = time.time() + 120
        while time.time() < deadline:
            st = httpx.get(base + f"/courses/{cid}", headers=H, timeout=30).json()
            if st["state"] == "ready" and st["mega"]["state"] in ("done", "error"):
                break
            time.sleep(0.3)
        print("· الباقة:", st["state"], "· MEGA:", st["mega"]["state"],
              "· ZIP:", st["zip_bytes"], "بايت")
        assert st["state"] == "ready"
        assert st["mega"]["state"] == "done"

        url = st["zip_url"].replace("https://", "http://")
        with httpx.stream("GET", url, headers=H, timeout=120) as resp:
            assert resp.status_code == 200
            z = zipfile.ZipFile(_io.BytesIO(b"".join(resp.iter_bytes())))
        inside = sorted(z.namelist())
        print("✓ داخل الـZIP:", inside)
        assert inside == ["001 - الوحدة 1 - مقدمة.mp4", "002 - الوحدة 1 - مقدمة.ar.srt",
                          "003 - الوحدة 1 - مقدمة.ar.txt", "004 - الوحدة 2 - الانحدار.mp4",
                          "005 - الوحدة 2 - الانحدار.ar.srt", "006 - الوحدة 2 - الانحدار.ar.txt",
                          "007 - الوحدة 3 - التصنيف.mp4", "008 - الوحدة 3 - التصنيف.ar.srt",
                          "009 - الوحدة 3 - التصنيف.ar.txt"], inside

        # لوحة العمليات: صف التنزيل يعرض ما نُزّل فعلًا
        ops = httpx.get(base + "/ops", headers=H, timeout=30).json()
        row = next(x for x in ops["courses"] if x["id"] == cid)
        dl = row["download"]
        print(f"· /ops (تنزيل): «{row['name']}» {dl['state']} {dl['done']}/{dl['total']}"
              f" · مدموج {dl['muxed']} · أخطاء {dl['errors_count']} · ملفات {len(dl['files_done'])}")
        assert dl["state"] == "done" and dl["done"] == 4, dl
        assert dl["muxed"] == 3 and dl["errors_count"] == 1, dl
        assert len(dl["files_done"]) >= 9, dl

        print("\nكل اختبارات التنزيل على السيرفر نجحت ✅  (0.9.8 — بلا تنزيل على الهاتف)")
        return 0
    finally:
        api.terminate()
        httpd.shutdown()


raise SystemExit(main())
