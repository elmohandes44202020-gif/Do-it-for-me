"""
collections.py — «دفتر ملاحظات كامل» في مهمة واحدة.

الفكرة:
  • التطبيق يرسل **كل** حلقات الدفتر (أي عدد) في طلب واحد.
  • السيرفر ينزّل ويحوّل بتسلسل مضبوط (Semaphore) حتى لا تنفجر الذاكرة.
  • يلي ذلك **الترتيب**: تُقرأ أسماء الملفات من ملف نصي مرجعي، ويُنقل كل ملف
    بالترتيب إلى مجلد `<اسم الدفتر>/تم الترتيب/` بفاصل 150ms بين كل نقل.
  • أخيرًا يُبنى ZIP واحد بثًّا أثناء الإرسال — باسم دفتر الملاحظات.

كل حلقات الدفتر تبقى في **مجلد واحد** (لا مجلد لكل دفعة)، والترتيب يُطبَّق مرة
واحدة على الكل، لأن التقسيم لمجلدات منفصلة يُفسد الترتيب العالمي.
"""
from __future__ import annotations

import difflib
import os
import re
import shutil
import threading
import time
import unicodedata
import uuid
from dataclasses import dataclass, field
from typing import Any, Iterable

import media
import zipstream

# ─────────────────────────────── الإعدادات ───────────────────────────────
WORK_DIR = os.environ.get("WORK_DIR", "/tmp/nlm")
COLS_DIR = os.path.join(WORK_DIR, "collections")
TTL_MINUTES = int(os.environ.get("TTL_MINUTES", "45"))
WORKERS = max(1, int(os.environ.get("COLLECTION_WORKERS", "1")))   # تسلسل كامل: يمنع OOM و403 المتزامن
MAX_COLLECTION_MB = int(os.environ.get("MAX_COLLECTION_MB", "1500"))
MAX_ITEMS = int(os.environ.get("MAX_COLLECTION_FILES", "400"))
MATCH_DEFAULT = float(os.environ.get("ORDER_MATCH", "0.60"))     # 50%..80%
MOVE_DELAY = float(os.environ.get("ORDER_MOVE_DELAY", "0.15"))   # 150ms
RETRIES = max(1, int(os.environ.get("DOWNLOAD_RETRIES", "2")))
DOWNLOAD_TIMEOUT = float(os.environ.get("DOWNLOAD_TIMEOUT_SEC", "60"))
FAIL_FAST_AFTER = int(os.environ.get("FAIL_FAST_AFTER", "3"))
SORTED = os.environ.get("SORTED_DIRNAME", "تم الترتيب")
MP3_BITRATE = int(os.environ.get("MP3_BITRATE", "96000"))
MP3_MONO = os.environ.get("MP3_MONO", "1") not in ("0", "false", "False", "")

_SEM = threading.Semaphore(WORKERS)
_LOCK = threading.Lock()
COLS: dict[str, "Collection"] = {}

_NUM_PREFIX = re.compile(r"^\s*(?:[0-9]+|[٠-٩]+)\s*[-.)–—:]?\s*")
_WS = re.compile(r"[\s_\-–—]+")


# ─────────────────────────────── تطبيع ومطابقة ───────────────────────────────
def _norm(s: str) -> str:
    """تطبيع للمقارنة: بلا .mp3، بلا ترقيم في البداية، بلا فروق تشكيل/مسافات."""
    s = unicodedata.normalize("NFKC", s or "").strip()
    s = re.sub(r"\.(mp3|m4a|txt)$", "", s, flags=re.I)
    s = _NUM_PREFIX.sub("", s)          # "3- العنوان" → "العنوان"
    s = re.sub(r"[\u064B-\u0652\u0640]", "", s)   # تشكيل + تطويل
    s = s.replace("أ", "ا").replace("إ", "ا").replace("آ", "ا")
    s = s.replace("ى", "ي").replace("ة", "ه")
    s = _WS.sub(" ", s).strip()
    return s.casefold()


def similarity(a: str, b: str) -> float:
    na, nb = _norm(a), _norm(b)
    if not na or not nb:
        return 0.0
    if na == nb:
        return 1.0
    if na in nb or nb in na:
        return max(0.90, difflib.SequenceMatcher(None, na, nb).ratio())
    return difflib.SequenceMatcher(None, na, nb).ratio()


def order_lines(text: str) -> list[str]:
    out = []
    for line in (text or "").splitlines():
        t = line.strip()
        if t and not t.startswith("#"):
            out.append(t)
    return out


# ─────────────────────────────── البيانات ───────────────────────────────
@dataclass
class Item:
    idx: int
    title: str
    url: str
    cookie: str | None = None
    cookie_map: dict[str, str] | None = None
    user_agent: str | None = None
    referer: str | None = None
    bitrate: int = MP3_BITRATE
    mono: bool = MP3_MONO

    state: str = "pending"      # pending|downloading|converting|done|error
    error: str = ""
    bytes_in: int = 0
    bytes_out: int = 0
    mp3: str | None = None
    final_name: str = ""
    order: int | None = None
    score: float = 0.0
    elapsed_in: float = 0.0
    elapsed_conv: float = 0.0
    attempts: int = 0
    status: int = 0


@dataclass
class Collection:
    id: str
    owner: str
    name: str                    # اسم دفتر الملاحظات (كما جاء من التطبيق)
    items: list[Item] = field(default_factory=list)
    order_text: str = ""
    match: float = MATCH_DEFAULT
    created_at: float = field(default_factory=time.time)
    finished_at: float | None = None
    state: str = "pending"       # pending|running|waiting_order|sorting|done|partial
    error: str = ""
    order: list[dict] = field(default_factory=list)   # [{name,path,size,crc,order}]
    unmatched_lines: list[str] = field(default_factory=list)
    unplaced_items: list[str] = field(default_factory=list)
    log: list[dict] = field(default_factory=list)
    last_tick: float = field(default_factory=time.time)
    sort_seconds: float = 0.0
    _lock: threading.Lock = field(default_factory=threading.Lock, repr=False)
    _zip: tuple[list[zipstream.Entry], int] | None = field(default=None, repr=False)

    # ── مسارات ──
    @property
    def dir(self) -> str:
        return os.path.join(COLS_DIR, self.id)

    @property
    def raw_dir(self) -> str:
        return os.path.join(self.dir, "raw")

    @property
    def sorted_dir(self) -> str:
        return os.path.join(self.dir, media.safe_name(self.name, 0, "notebook"), SORTED)

    def note(self, msg: str) -> None:
        self.last_tick = time.time()
        try:
            print(f"[col {self.id}] {msg}", flush=True)
        except Exception:
            pass
        with self._lock:
            self.log.append({"t": int(time.time()), "msg": msg[:300]})
            del self.log[:-200]

    # ── حالة ──
    def counts(self) -> dict[str, int]:
        c = {"pending": 0, "downloading": 0, "converting": 0, "done": 0, "error": 0}
        for it in self.items:
            c[it.state] = c.get(it.state, 0) + 1
        return c

    def _refresh(self) -> None:
        c = self.counts()
        if self.state in ("sorting", "done", "partial"):
            return
        active = c["pending"] + c["downloading"] + c["converting"]
        if active:
            self.state = "running"
            return
        if c["done"] == 0:
            self.state = "partial"
            return
        # دائماً نُنهي بالترتيب: ملف الترتيب إن وُجد، وإلا ترتيب ظهور الحلقات.
        start_finalize(self)

    def to_dict(self, base: str = "", detail: bool = False) -> dict[str, Any]:
        c = self.counts()
        expires = None
        if self.finished_at:
            expires = max(0, int(self.finished_at + TTL_MINUTES * 60 - time.time()))
        d: dict[str, Any] = {
            "id": self.id,
            "name": self.name,
            "state": self.state,
            "files_total": len(self.items),
            "files_done": c["done"],
            "files_failed": c["error"],
            "active": c["pending"] + c["downloading"] + c["converting"],
            "bytes_out": sum(i.bytes_out for i in self.items),
            "created_at": int(self.created_at),
            "expires_in_sec": expires,
            "sorted_dir": SORTED,
            "match": self.match,
            "zip_url": f"{base}/collections/{self.id}/zip"
                       if self.state in ("done", "partial") and c["done"] else None,
            "error": self.error,
            "unmatched_lines": self.unmatched_lines[:30],
            "unplaced_items": self.unplaced_items[:30],
            "log": self.log[-40:],
            "failures": [{"title": i.title, "error": i.error[:300]}
                         for i in self.items if i.state == "error"][:60],
        }
        if detail:
            d["items"] = [{
                "idx": i.idx, "title": i.title, "state": i.state,
                "bytes_out": i.bytes_out, "error": i.error[:200],
                "final_name": i.final_name, "order": i.order, "score": round(i.score, 2),
                "attempts": i.attempts, "status": i.status,
                "sec_in": round(i.elapsed_in, 1), "sec_conv": round(i.elapsed_conv, 1),
            } for i in self.items]
            d["ordered"] = [{"order": e["order"], "name": e["name"], "size": e["size"]}
                            for e in self.order]
            d["sort_seconds"] = round(self.sort_seconds, 1)
        return d


# ─────────────────────────────── المعالجة ───────────────────────────────
def _disk_free_mb() -> float:
    try:
        return shutil.disk_usage(COLS_DIR).free / (1024 * 1024)
    except Exception:
        return 10_000.0



def _maybe_fail_fast(col: Collection, last: str) -> None:
    """لو أول حلقات كلها 401/403 ⇒ الروابط منتهية: أوقف بدل الانتظار ساعات."""
    if "403" not in last and "401" not in last:
        return
    done = sum(1 for i in col.items if i.state == "done")
    if done:
        return
    auth_fails = sum(1 for i in col.items
                     if i.state == "error" and ("403" in (i.error or "") or "401" in (i.error or "")))
    if auth_fails + 1 >= FAIL_FAST_AFTER:
        if col.state != "partial":
            col.error = ("الروابط أو الكوكيز انتهت صلاحيتها (401/403 متكرر). "
                         "أعد الالتقاط من الاستديو وأرسل فورًا.")
            col.note("⛔ إيقاف سريع: " + col.error)

def _attempt(col: Collection, it: Item) -> None:
    """محاولة واحدة: تنزيل + تحويل. ترفع MediaError عند الفشل."""
    os.makedirs(col.raw_dir, exist_ok=True)
    src = os.path.join(col.raw_dir, f"{it.idx:04d}.m4a")
    dst = os.path.join(col.raw_dir, f"{it.idx:04d}.mp3")
    try:
        it.state = "downloading"
        col.last_tick = time.time()
        t0 = time.time()
        it.bytes_in = media.download(
            it.url, src, cookie=it.cookie, cookie_map=it.cookie_map,
            user_agent=it.user_agent, referer=it.referer,
            max_bytes=400 * 1024 * 1024, timeout=DOWNLOAD_TIMEOUT,
        )
        it.elapsed_in = time.time() - t0
        if it.bytes_in < 1024:
            raise media.MediaError(f"الملف صغير جدًا ({it.bytes_in} بايت) — الرابط ليس صوتًا")

        it.state = "converting"
        col.last_tick = time.time()
        t1 = time.time()
        info = media.m4a_to_mp3(src, dst, bitrate=it.bitrate, mono=it.mono)
        it.elapsed_conv = time.time() - t1
        it.bytes_out = int(info.get("bytes") or os.path.getsize(dst))
        it.mp3 = dst
        it.state = "done"
        col.note(f"✅ {it.title} · {it.bytes_out/1048576:.1f} م.ب")
    finally:
        try:
            if os.path.exists(src):
                os.remove(src)
        except OSError:
            pass


def _process(col: Collection, it: Item) -> None:
    # فحوص المساحة مرة واحدة قبل المحاولات
    try:
        used_mb = sum(i.bytes_out for i in col.items) / (1024 * 1024)
        if used_mb >= MAX_COLLECTION_MB:
            raise media.MediaError(
                f"تجاوز حد هذه المجموعة ({MAX_COLLECTION_MB} م.ب). "
                "احذف بعض الحلقات أو ابدأ مجموعة جديدة."
            )
        free = _disk_free_mb()
        if free < 120:
            raise media.MediaError(
                f"مساحة السيرفر على وشك الانتهاء (المتاح {free:.0f} م.ب). "
                "أعد المحاولة بعد قليل أو احذف مجموعات قديمة."
            )
    except media.MediaError as e:
        it.state = "error"
        it.error = str(e)
        col.note(f"❌ {it.title} · {str(e)[:120]}")
        return

    last = ""
    for attempt in range(RETRIES):
        it.attempts = attempt + 1
        try:
            _attempt(col, it)
            if it.attempts > 1:
                col.note(f"✅ {it.title} نجحت في المحاولة {it.attempts}")
            return
        except media.MediaError as e:
            last = str(e) or "فشل غير معروف"
        except Exception as e:                      # noqa: BLE001
            last = f"{type(e).__name__}: {e}"
        it.state = "pending"
        _maybe_fail_fast(col, last)
        if attempt + 1 < RETRIES:
            col.note(f"↻ {it.title} — محاولة {attempt + 2}/{RETRIES} بعد {last[:70]}")
            time.sleep(1.5 * (attempt + 1))

    it.state = "error"
    it.error = last or "سبب غير معروف (لم يصل أي تفسير من الشبكة)"
    col.note(f"❌ {it.title} · {it.error[:140]}")


def _run(col: Collection) -> None:
    for it in col.items:
        if it.state != "pending":
            continue
        with _SEM:
            _process(col, it)
            col._refresh()
    # أي عنصر لم يُعالج يُوسم صراحةً بسبب واضح (لا يبقى صامتًا أبدًا)
    with col._lock:
        for it in col.items:
            if it.state in ("pending", "downloading", "converting"):
                it.state = "error"
                it.error = it.error or "لم يكتمل التنزيل (توقف غير متوقع)"
                col.note(f"⚠️ {it.title} · {it.error[:100]}")
        col.finished_at = time.time()
    col._refresh()
    if col.state == "waiting_order":
        col.note("⏳ التنزيل والتحويل خلصوا — مستني ملف الترتيب النصي.")


# ─────────────────────────────── الترتيب والنقل ───────────────────────────────
def build_order(col: Collection) -> list[Item]:
    """يرتّب الحلقات وفق النص المرجعي بمطابقة ≥ col.match، والباقي في الآخر."""
    lines = order_lines(col.order_text)
    done = [i for i in col.items if i.state == "done"]
    pairs: list[tuple[float, int, int]] = []
    for li, line in enumerate(lines):
        for ii, it in enumerate(done):
            s = similarity(line, it.final_name or it.title)
            if s >= col.match:
                pairs.append((s, li, ii))
    pairs.sort(key=lambda p: (-p[0], p[1], p[2]))

    used_lines: set[int] = set()
    placed: dict[int, tuple[int, float]] = {}      # item idx → (order, score)
    for s, li, ii in pairs:
        if li in used_lines or ii in placed:
            continue
        used_lines.add(li)
        placed[ii] = (len(placed), s)

    col.unmatched_lines = [lines[i] for i in range(len(lines)) if i not in used_lines]
    rest = [it for ii, it in enumerate(done) if ii not in placed]
    col.unplaced_items = [it.title for it in rest]

    ordered = [done[ii] for ii, _ in sorted(placed.items(), key=lambda kv: kv[1][0])]
    for it in ordered:
        it.order = placed[done.index(it)][0]
        it.score = placed[done.index(it)][1]
    for k, it in enumerate(rest):
        it.order = len(ordered) + k
        it.score = 0.0
    return ordered + rest


def start_finalize(col: Collection) -> bool:
    with col._lock:
        if col.state in ("sorting",):
            return False
        col.state = "sorting"
    threading.Thread(target=_finalize, args=(col,), daemon=True).start()
    return True


def _finalize(col: Collection) -> None:
    t0 = time.time()
    try:
        ordered = build_order(col)
        if not ordered:
            raise media.MediaError("لا توجد ملفات ناجحة لترتيبها")

        target = col.sorted_dir
        os.makedirs(target, exist_ok=True)
        col.note(f"📂 الترتيب: {len(ordered)} ملف → {SORTED}/ بفاصل {int(MOVE_DELAY*1000)}ms")

        entries: list[dict] = []
        taken: set[str] = set()
        for it in ordered:
            name = media.episode_filename(it.final_name or it.title, it.idx + 1, taken)
            it.final_name = name
            dest = os.path.join(target, name)
            if os.path.exists(dest):
                os.remove(dest)
            os.replace(it.mp3, dest)          # «نقل» فعلي بالترتيب
            it.mp3 = dest
            crc, size = zipstream.crc_of(dest)
            entries.append({"order": it.order, "name": name, "path": dest,
                            "size": size, "crc": crc})
            time.sleep(MOVE_DELAY)            # 150ms بين كل نقل وملف
        col.order = entries
        col._zip = ([zipstream.Entry(name=e["name"], path=e["path"],
                                     size=e["size"], crc=e["crc"]) for e in entries],
                    zipstream.zip_size([zipstream.Entry(name=e["name"], path=e["path"],
                                                        size=e["size"], crc=e["crc"])
                                        for e in entries]))
        col.sort_seconds = time.time() - t0
        col.finished_at = time.time()
        c = col.counts()
        col.state = "done" if c["error"] == 0 else "partial"
        col.note(f"🗜️ ZIP جاهز · {len(entries)} ملف · "
                 f"{sum(e['size'] for e in entries)/1048576:.1f} م.ب")
    except Exception as e:                    # noqa: BLE001
        col.state = "partial"
        col.error = f"{type(e).__name__}: {e}"
        col.note("❌ فشل الترتيب: " + col.error[:160])


# ─────────────────────────────── API ───────────────────────────────
def create(owner: str, name: str, items: Iterable[dict], order_text: str = "",
           match: float | None = None) -> Collection:
    os.makedirs(COLS_DIR, exist_ok=True)
    col = Collection(id=uuid.uuid4().hex[:12], owner=owner,
                     name=media.safe_name(name, 0, "notebooklm-audio"))
    col.match = min(0.80, max(0.50, float(match if match is not None else MATCH_DEFAULT)))
    col.order_text = order_text or ""
    for idx, raw in enumerate(items):
        if idx >= MAX_ITEMS:
            break
        col.items.append(Item(
            idx=idx,
            title=str(raw.get("title") or f"حلقة {idx+1}"),
            url=str(raw.get("url") or ""),
            cookie=raw.get("cookie"), cookie_map=raw.get("cookie_map"),
            user_agent=raw.get("user_agent"),
            referer=raw.get("referer"),
            bitrate=int(raw.get("bitrate") or MP3_BITRATE),
            mono=bool(raw.get("mono", MP3_MONO)),
        ))
    with _LOCK:
        COLS[col.id] = col
    col.note(f"📥 استلمت {len(col.items)} حلقة — «{col.name}»")
    threading.Thread(target=_run, args=(col,), daemon=True).start()
    return col


def get(col_id: str) -> Collection | None:
    return COLS.get(col_id)


def retry(col: Collection, indices: list[int] | None = None) -> int:
    n = 0
    with col._lock:
        for it in col.items:
            if it.state == "error" and (not indices or it.idx in indices):
                it.state = "pending"
                it.error = ""
                n += 1
        if n:
            col.state = "running"
            col.finished_at = None
            col._zip = None
    if n:
        col.note(f"🔁 إعادة محاولة {n} حلقة")
        threading.Thread(target=_run, args=(col,), daemon=True).start()
    return n


def zip_entries(col: Collection) -> tuple[list[zipstream.Entry], int]:
    if col._zip is None:
        entries = [zipstream.Entry(name=e["name"], path=e["path"],
                                   size=e["size"], crc=e["crc"]) for e in col.order]
        col._zip = (entries, zipstream.zip_size(entries))
    return col._zip


def delete(col_id: str) -> bool:
    with _LOCK:
        col = COLS.pop(col_id, None)
    if not col:
        return False
    shutil.rmtree(col.dir, ignore_errors=True)
    return True


def janitor() -> None:
    now = time.time()
    for cid, col in list(COLS.items()):
        if col.finished_at and now > col.finished_at + TTL_MINUTES * 60:
            delete(cid)


def _janitor_loop() -> None:
    while True:
        time.sleep(120)
        try:
            janitor()
        except Exception:            # noqa: BLE001
            pass


threading.Thread(target=_janitor_loop, daemon=True).start()
