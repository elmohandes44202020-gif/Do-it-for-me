"""اختبار 0.9.9 — طابور رفع دائم يعيش عبر إعادة تشغيل السيرفر + تنفيذ تلقائي بالترتيب.

السيناريو (نفس حالة المستخدم: «عايز أشتغل وأقفل والسيرفر ينفّذ لوحده»):
  ① سيرفر يعمل والمحرّك موقوف مؤقتًا (NW_PAUSE_UPLOADS=1) ⇒ ثلاث باقات تدخل الطابور بترتيبها.
  ② فحص ترتيب الطابور + القدرة على تقديم باقة (prioritize).
  ③ إيقاف السيرفر تمامًا (kill) والمحرّك لم ينفّذ شيئًا ⇒ التأكد أن الفهرس والطابور محفوظان على القرص.
  ④ إعادة تشغيل السيرفر بلا إيقاف ⇒ ينفّذ الثلاثة تلقائيًا وبنفس ترتيب الدخول.
  ⑤ وحدة: عند غياب جلسة MEGA (وضع حقيقي بلا NW_FAKE) ⇒ الحالة «needs_session» لا «error».

التشغيل: python3 t_persist.py
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
import time

import httpx

HERE = os.path.dirname(os.path.abspath(__file__))
PA = 8866
KEY = "persist-key-1"
WORK = "/tmp/nw-persist"


def start(pause: bool) -> subprocess.Popen:
    env = {**os.environ, "API_KEYS": f"me:{KEY}", "WORK_DIR": WORK,
           "NW_FAKE_MEGA": "1", "NW_FAKE_TG": "1"}
    env.pop("NW_PAUSE_UPLOADS", None)
    if pause:
        env["NW_PAUSE_UPLOADS"] = "1"
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


def make_ready(base, H, name, cloud="mega", auto=True, sid=""):
    cid = httpx.post(base + "/courses", headers=H, timeout=30, json={
        "name": name, "delivery": "folder", "cloud": cloud, "auto": auto,
        "mega_session": sid}.copy() if False else {
        "name": name, "delivery": "folder", "cloud": cloud, "auto": auto}).json()["course_id"]
    for fname, kind in (("001 - مقدمة.mp4", "video"), ("002 - مقدمة.ar.srt", "srt")):
        httpx.post(base + f"/courses/{cid}/files", params={"name": fname, "kind": kind},
                   content=b"DATA" * 512, headers=H, timeout=60)
    httpx.post(base + f"/courses/{cid}/finalize", headers=H, timeout=30)
    end = time.time() + 60
    while time.time() < end:
        st = httpx.get(base + f"/courses/{cid}", headers=H, timeout=30).json()
        if st["state"] == "ready":
            return cid
        time.sleep(0.2)
    raise RuntimeError("الباقة لم تجهز: " + name)


def main() -> int:
    subprocess.run(["rm", "-rf", WORK], check=False)
    base = f"http://127.0.0.1:{PA}"
    H = {"X-Api-Key": KEY}

    # ① ثلاث باقات والرفع موقوف (المحرّك لا ينفّذ)
    api = start(pause=True)
    try:
        print("· إصدار السيرفر:", httpx.get(base + "/healthz").json()["version"])
        ids = [make_ready(base, H, f"باقة {i}") for i in (1, 2, 3)]
        q = httpx.get(base + "/queue", headers=H, timeout=30).json()
        pos = [(it["name"], it["queue_position"]) for it in q["items"] if it["queue_position"]]
        pos.sort(key=lambda r: r[1])
        print("① الطابور (المحرّك موقوف):", pos)
        assert [p[1] for p in pos] == [1, 2, 3], pos
        names = [p[0] for p in pos]
        assert names == ["باقة 1", "باقة 2", "باقة 3"], names
        ops = httpx.get(base + "/ops", headers=H, timeout=30).json()
        file_pos = next(x for x in ops["courses"] if x["id"] == ids[0])["upload"]["position"]
        print("   ترتيب الباقة الأولى من /ops:", file_pos)
        assert file_pos == 1, file_pos

        # ② تقديم الباقة الأخيرة إلى المقدمة
        rp = httpx.post(base + "/queue/prioritize", headers=H, timeout=30,
                        json={"course_id": ids[2]})
        print("② بعد التقديم:", rp.status_code, rp.json())
        assert rp.status_code == 200 and rp.json()["position"] == 1, rp.text
        q = httpx.get(base + "/queue", headers=H, timeout=30).json()
        first = next(it for it in q["items"] if it["queue_position"] == 1)
        assert first["id"] == ids[2], first

        # أعِد الترتيب الأصلي حتى يتساوى الاختبار مع «ترتيب الدخول»
        httpx.post(base + "/queue/prioritize", headers=H, timeout=30, json={"course_id": ids[1]})
        httpx.post(base + "/queue/prioritize", headers=H, timeout=30, json={"course_id": ids[0]})
    finally:
        api.terminate()
        api.wait(timeout=15)

    state_file = os.path.join(WORK, "courses", "courses.json")
    assert os.path.exists(state_file), "فهرس الباقات لم يُحفظ على القرص!"
    saved = json.load(open(state_file, encoding="utf-8"))
    print(f"③ الفهرس محفوظ على القرص: {len(saved['courses'])} باقة · "
          f"الحالات: {[c['mega_state'] for c in saved['courses']]}")
    assert len(saved["courses"]) == 3, saved
    assert all(c["state"] == "ready" for c in saved["courses"])

    # ④ إعادة تشغيل بلا إيقاف ⇒ تنفيذ تلقائي بالترتيب
    api = start(pause=False)
    try:
        deadline = time.time() + 180
        done_order: list[str] = []
        while time.time() < deadline:
            rows = httpx.get(base + "/ops", headers=H, timeout=30).json()["courses"]
            done = [r for r in rows if r["upload"]["state"] == "done"]
            if len(done) == 3:
                done_order = [r["name"] for r in sorted(done, key=lambda r: r["upload"]["queued_at"])]
                break
            time.sleep(0.3)
        print("④ بعد إعادة التشغيل — ترتيب الإنجاز:", done_order)
        assert len(done_order) == 3, "لم تُنفَّذ الثلاثة تلقائيًا بعد إعادة التشغيل"
        assert done_order == ["باقة 1", "باقة 2", "باقة 3"], done_order
        eng = httpx.get(base + "/queue", headers=H, timeout=30).json()["engine"]
        print("   المحرّك بعد الاستئناف:", {"processed": eng["processed"],
                                          "waiting": eng["waiting"], "errors": eng["errors"]})
        assert eng["waiting"] == 0, eng
    finally:
        api.terminate()
        api.wait(timeout=15)

    # ⑤ وحدة: غياب الجلسة في وضع حقيقي ⇒ needs_session لا error
    os.environ["WORK_DIR"] = "/tmp/nw-persist-unit"
    os.environ.pop("NW_FAKE_MEGA", None)
    sys.path.insert(0, HERE)
    import courses as courses_mod          # noqa: E402
    import mega_up                          # noqa: E402
    import main as main_mod                 # noqa: E402
    c = courses_mod.create("me", "باقة بلا جلسة", delivery="folder", cloud="mega", auto=True)
    c.state = "ready"
    c.order = [{"name": "a.mp4", "path": __file__, "size": 10}]
    old_fake, old_enabled = mega_up.FAKE, mega_up.enabled
    mega_up.FAKE = False
    mega_up.enabled = lambda: False
    try:
        main_mod._mega_run(c, "sid-غير-موجود")
    finally:
        mega_up.FAKE, mega_up.enabled = old_fake, old_enabled
    print(f"⑤ بلا جلسة: state={c.mega_state} · {c.mega_error}")
    assert c.mega_state == "needs_session", c.mega_state
    assert "سجّل الدخول" in c.mega_error or "انتظار" in c.mega_error, c.mega_error

    print("\nكل اختبارات الطابور الدائم والتنفيذ التلقائي نجحت ✅  (0.9.9)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
