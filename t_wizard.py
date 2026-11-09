"""اختبار 0.9.2 — عقد العميل: يحاكي حرفيًا ما يفعله CourseWizard.java في التطبيق 3.60.

نفس التسلسل: POST /courses ⇒ POST /courses/{id}/files (اسم عادي بلا ترقيم، ترتيب الوصول
هو الترتيب) ⇒ finalize ⇒ polling على {state, mega|tg.state, percent, link} ⇒ GET /courses.

التشغيل: python3 t_wizard.py
"""
from __future__ import annotations

import io as _io
import os
import subprocess
import sys
import time
import zipfile

import httpx

HERE = os.path.dirname(os.path.abspath(__file__))
PORT = 8866
KEY = "wizard-key-1"


def wait_health(base: str) -> None:
    for _ in range(150):
        try:
            if httpx.get(base + "/healthz", timeout=1).status_code == 200:
                return
        except Exception:
            time.sleep(0.2)
    raise SystemExit("السيرفر لم يبدأ")


def upload(base, cid, name, data, kind) -> dict:
    r = httpx.post(base + f"/courses/{cid}/files", params={"name": name, "kind": kind},
                   content=data, headers={"X-Api-Key": KEY,
                                          "Content-Type": "application/octet-stream"},
                   timeout=60)
    assert r.status_code == 201, r.text
    return r.json()


def poll(base, cid, cloud, auto, timeout=120.0):
    """نفس حلقة CourseWizard: تجهيز ⇒ رفع ⇒ رابط، مع كسر عند done/error."""
    deadline = time.time() + timeout
    ready_at = 0.0
    link = ""
    while time.time() < deadline:
        time.sleep(0.2)
        st = httpx.get(base + f"/courses/{cid}", headers={"X-Api-Key": KEY}, timeout=30).json()
        assert st["state"] != "error", st
        cs = st.get("tg" if cloud == "telegram" else "mega") or {}
        state_c = cs.get("state", "")
        if state_c == "done":
            link = cs.get("link", "") or link
            return st, link
        if state_c == "error":
            return st, link
        if st["state"] == "ready" and not auto:
            return st, st.get("zip_url", "")
        if st["state"] == "ready" and not state_c:
            if not ready_at:
                ready_at = time.time()
            elif time.time() - ready_at > 3:  # في الاختبار: 3 ثوانٍ بدل 90
                return st, st.get("zip_url", "")
        else:
            ready_at = 0.0
    raise SystemExit("انتهت المهلة داخل حلقة العميل")


def main() -> int:
    env = {**os.environ, "API_KEYS": f"me:{KEY}", "WORK_DIR": "/tmp/nw-wizard",
           "NW_FAKE_MEGA": "1", "NW_FAKE_TG": "1"}
    api = subprocess.Popen([sys.executable, "-m", "uvicorn", "main:app", "--host",
                            "127.0.0.1", "--port", str(PORT), "--log-level", "warning"],
                           cwd=HERE, env=env)
    base = f"http://127.0.0.1:{PORT}"
    H = {"X-Api-Key": KEY}
    try:
        wait_health(base)
        print("· إصدار السيرفر:", httpx.get(base + "/healthz").json()["version"])

        # ── حالة 1: ZIP + MEGA تلقائي (أسماء بلا ترقيم كما يرسلها التطبيق) ──
        r = httpx.post(base + "/courses", headers=H, json={
            "name": "أساسيات تعلم الآلة", "delivery": "zip", "cloud": "mega",
            "auto": True, "mega_session": "", "tg_session": ""}, timeout=30).json()
        c1 = r["course_id"]
        assert r["delivery"] == "zip" and r["cloud"] == "mega", r
        print("✓ فتحت الباقة:", c1)

        plan = [
            ("مقدمة.mp4", b"MP4DATA" * 4000, "video"),
            ("مقدمة.ar.srt", "1\n00:00:00,000 --> 00:00:02,000\nمقدمة\n".encode(), "srt"),
            ("الانحدار.mp4", b"MP4SECOND" * 3000, "video"),
            ("الانحدار.ar.srt", "1\n00:00:01,000 --> 00:00:03,000\nالانحدار\n".encode(), "srt"),
            ("ملاحظات.txt", b"notes", "txt"),
        ]
        for name, data, kind in plan:
            got = upload(base, c1, name, data, kind)
            print(f"  ⬆ {got['idx']:03d} ← {name} ({got['size']} بايت)")

        assert httpx.post(base + f"/courses/{c1}/finalize", headers=H,
                          timeout=30).json()["state"] == "packing"
        st, link = poll(base, c1, "mega", True)
        print(f"· ZIP: {st['state']} · MEGA {st['mega']['state']} · رابط: {bool(link)}")
        assert st["state"] == "ready" and st["mega"]["state"] == "done"
        assert st["mega"]["folder"] == "أساسيات تعلم الآلة"

        url = st["zip_url"].replace("https://", "http://")
        with httpx.stream("GET", url, headers=H, timeout=120) as resp:
            assert resp.status_code == 200
            blob = b"".join(resp.iter_bytes())
        names = sorted(zipfile.ZipFile(_io.BytesIO(blob)).namelist())
        print("✓ داخل الـZIP:", names)
        # الاسم الذي يرسله التطبيق بلا ترقيم ⇒ السيرفر يرقّمه "NNN - ..." بترتيب الوصول،
        # والملف يصل خلف فيديوه: فيديو ثم الترجمة الخاصة به ثم الفيديو التالي.
        assert names == ["001 - مقدمة.mp4", "002 - مقدمة.ar.srt", "003 - الانحدار.mp4",
                         "004 - الانحدار.ar.srt", "005 - ملاحظات.txt"], names
        files_order = [(f["idx"], f["name"]) for f in st["files"]]
        print("✓ ترتيب الوصول داخل الباقة:", files_order)
        assert [f[0] for f in files_order] == [1, 2, 3, 4, 5]

        # ── حالة 2: مجلد + Telegram بلا رفع تلقائي ⇒ رابط/BG صحيح بلا تعليق ──
        r2 = httpx.post(base + "/courses", headers=H, json={
            "name": "دورة الشبكات", "delivery": "folder", "cloud": "telegram",
            "auto": False}, timeout=30).json()
        c2 = r2["course_id"]
        upload(base, c2, "الدرس الأول.mp4", b"Y" * 12000, "video")
        upload(base, c2, "الدرس الأول.ar.srt", b"1\n00:00:00,000 --> 00:00:01,000\nx\n", "srt")
        httpx.post(base + f"/courses/{c2}/finalize", headers=H, timeout=30)
        st2, link2 = poll(base, c2, "telegram", False)
        print(f"· مجلد: {st2['state']} · TG {st2['tg'].get('state', '—')} · رابط: {bool(link2)}")
        assert st2["state"] == "ready" and st2["delivery"] == "folder"
        assert (st2["tg"].get("state") or "idle") in ("", "idle"), st2["tg"]

        # ── حالة 3: زر «أحدث باقة» ⇒ GET /courses ──
        lst = httpx.get(base + "/courses", headers=H, timeout=30).json()
        ids = [c["id"] for c in lst["courses"]]
        assert c1 in ids and c2 in ids, ids
        shown = {(c["name"], c["state"], c["cloud"]) for c in lst["courses"]}
        print("✓ GET /courses ⇒", shown)
        for c in lst["courses"]:
            if c["id"] == c1:
                assert c["mega"]["state"] == "done" and c["count"] == 5
                assert c["zip_bytes"] > 0

        # ── حالة 4: باقة مقفلة ترفض أي تعديل (409) ──
        bad = httpx.post(base + f"/courses/{c1}/files", params={"name": "x.txt", "kind": "txt"},
                         content=b"z", headers=H, timeout=30)
        assert bad.status_code == 409, bad.status_code
        print("✓ الباقة المقفلة ترفض الإضافة (409) والتطبيق يعرض السبب بلا انهيار")

        print("\nكل اختبارات عقد العميل لمعالج كورسيرا نجحت ✅  (السيرفر 0.9.3 / التطبيق 3.61)")
        return 0
    finally:
        api.terminate()


raise SystemExit(main())
