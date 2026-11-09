"""اختبار 0.9.7 — تشخيص اتصال MEGA (بلاغ «السيرفر حجب الاتصال»).

يثبت أن التصنيف صحيح في ثلاثة سيناريوهات، بخادم محلي يحاكي ردود ميجا:
  ① وضع الاختبار NW_FAKE_MEGA ⇒ fake + ok.
  ② ميجا تستقبل الطلب لكن ترد 402/فارغ على تسجيل الدخول ⇒ login_blocked + رسالة «ترفض».
  ③ ميجا ترد ‎-2 للفحص و‎-4 لبيانات دخول خاطئة ⇒ ok + «الاتصال سليم».

زيادة: يتأكد أن create_session يترجم هذه الحالات إلى رسائل عربية مفهومة.

التشغيل: python3 t_diag.py
"""
from __future__ import annotations

import json
import os
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer

os.environ.pop("NW_FAKE_MEGA", None)

import mega_up  # noqa: E402


class Stub(BaseHTTPRequestHandler):
    """يحاكي /cs: يرجع body بحسب المتغير mode."""
    mode = "blocked"

    def do_POST(self) -> None:                       # noqa: N802
        length = int(self.headers.get("Content-Length") or 0)
        raw = self.rfile.read(length).decode() or "[]"
        try:
            payload = json.loads(raw)
            action = (payload[0] or {}).get("a", "")
        except Exception:
            action = ""
        if self.mode == "blocked":
            self.send_response(402)
            self.end_headers()
            return
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.end_headers()
        body = "-4" if action == "us" else "-2"
        self.wfile.write(body.encode())

    def log_message(self, *args) -> None:
        pass


def main() -> int:
    srv = HTTPServer(("127.0.0.1", 0), Stub)
    port = srv.server_address[1]
    threading.Thread(target=srv.serve_forever, daemon=True).start()

    # ① وضع الاختبار
    mega_up.FAKE = True
    d = mega_up.diagnose()
    print("① وضع الاختبار:", d["verdict"][:60], "| ok =", d["ok"])
    assert d["ok"] is True and d["fake"] is True, d

    # ② ميجا ترفض تسجيل الدخول من عنوان السيرفر
    mega_up.FAKE = False
    mega_up.API_HOSTS = [f"127.0.0.1:{port}"]
    mega_up.API_SCHEME = "http"
    Stub.mode = "blocked"
    d = mega_up.diagnose()
    print("② محجوب:", d["verdict"][:70])
    print("   توصية:", d["advice"][:70])
    assert d["ok"] is False, d
    assert d["reachable"] is False or d["login_blocked"] is True, d
    assert "ترفض" in d["verdict"] or "402" in d["verdict"] or "فارغ" in d["verdict"], d
    assert "MEGA_PROXY" in d["advice"] or "تيليجرام" in d["advice"], d

    # ③ الاتصال سليم (ردود أرقام أخطاء ميجا العادية)
    Stub.mode = "ok"
    d = mega_up.diagnose()
    print("③ سليم:", d["verdict"][:70], "| login HTTP", d["login"].get("http"))
    assert d["ok"] is True and d["reachable"] is True and d["login_blocked"] is False, d
    assert d["verdict"].endswith("سليم من السيرفر."), d

    # ④ رسائل الدخول المفهومة عند الرفض
    Stub.mode = "blocked"
    try:
        mega_up.SESSIONS.clear()
        mega_up.create_session("probe@example.invalid", "x")
        raise AssertionError("كان المتوقع فشل الدخول")
    except RuntimeError as err:
        msg = str(err)
        print("④ رسالة الدخول:", msg[:80])
        assert "MEGA_PROXY" in msg or "تيليجرام" in msg, msg

    srv.shutdown()
    print("\nكل اختبارات تشخيص اتصال ميجا نجحت ✅  (0.9.7)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
