"""
links.py — استخراج روابط الصوت من نص أو ملف نصي.

الفكرة (بناءً على طلب المستخدم): التطبيق ينسخ الرابط داخل ملف نصي، ويرسله للسيرفر
كما هو — بصيغته التي تعمل على الهاتف — والسيرفر يفهمه وينزّل بنفسه.

الصيغ المدعومة في نفس الملف:
    https://lh3.googleusercontent.com/notebooklm/…
    الحلقة الأولى | https://lh3.googleusercontent.com/…
    الحلقة الثانية	https://…
    [الحلقة الثالثة](https://…)
    {"title":"…","url":"…"}                       (سطر JSON)
    [{"title":"…","url":"…"}, {"url":"…"}]        (مصفوفة JSON كاملة)
    اسم الملف نفسه يُستخدم عنوانًا لو لم يوجد عنوان.
"""
from __future__ import annotations

import json
import re
from typing import NamedTuple

URL_RE = re.compile(r"https?://[^\s\"'<>\)\]]+")
MD_LINK_RE = re.compile(r"\[([^\]]{1,200})\]\((https?://[^\)]+)\)")
TRAILING = ".,;:!?،؛…»\"'"


class LinkEntry(NamedTuple):
    title: str
    url: str


def _clean(url: str) -> str:
    url = url.strip().strip("\"'<>")
    while url and url[-1] in TRAILING:
        url = url[:-1]
    return url


def parse(text: str, default_title: str = "") -> list[LinkEntry]:
    """يستخرج أزواج (عنوان، رابط) من نص حر. لا يرفع استثناءات أبدًا."""
    if not text:
        return []
    text = text.replace("\r\n", "\n").replace("\r", "\n")

    # 1) مصفوفة JSON كاملة؟
    stripped = text.strip()
    if stripped.startswith("["):
        try:
            data = json.loads(stripped)
        except Exception:  # noqa: BLE001
            data = None
        if isinstance(data, list):
            out = []
            for item in data:
                if isinstance(item, str):
                    u = _clean(item)
                    if u:
                        out.append(LinkEntry("", u))
                elif isinstance(item, dict):
                    u = _clean(str(item.get("url") or item.get("link") or ""))
                    if u:
                        out.append(LinkEntry(str(item.get("title") or item.get("name") or "")[:200], u))
            return out

    out: list[LinkEntry] = []
    for raw_line in text.split("\n"):
        line = raw_line.strip()
        if not line:
            continue

        # 2) سطر JSON مفرد
        if line.startswith("{") and line.endswith("}"):
            try:
                obj = json.loads(line)
            except Exception:  # noqa: BLE001
                obj = None
            if isinstance(obj, dict):
                u = _clean(str(obj.get("url") or obj.get("link") or ""))
                if u:
                    out.append(LinkEntry(str(obj.get("title") or "")[:200], u))
                    continue

        # 3) صيغة markdown [عنوان](رابط)
        m = MD_LINK_RE.search(line)
        if m:
            out.append(LinkEntry(m.group(1).strip()[:200], _clean(m.group(2))))
            continue

        # 4) عنوان + رابط مفصولين بعلامة
        m = URL_RE.search(line)
        if not m:
            continue
        url = _clean(m.group(0))
        title = line[: m.start()] + " " + line[m.end():]
        title = title.strip().strip("|-\t,،:—–").strip()
        if not re.search(r"\w", title):
            title = ""
        out.append(LinkEntry(title[:200], url))

    if not out or (len(out) == 1 and not out[0].title and default_title):
        out = [LinkEntry(default_title[:200], e.url) for e in out]
    return out


def looks_like_media_url(url: str) -> bool:
    """روابط الصوت في NotebookLM تمرّ عبر googleusercontent / googlevideo."""
    low = url.lower()
    return any(k in low for k in ("googleusercontent.com", "googlevideo.com", "googleapis.com"))
