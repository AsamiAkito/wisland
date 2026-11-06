# -*- coding: utf-8 -*-
"""离线词典：导入 Yomitan/Yomichan 格式词典（.zip），存进本地 SQLite 查询。

Yomitan 格式是日语学习圈最通用的离线词典格式：JMdict（英）、各种日中词典、
音调词典等都有这个格式的发布。导入一次后完全离线。

term_bank_*.json 的每一行：
  [词条, 读音, 释义标签, 活用规则, 分数, [释义...], 序号, 词条标签]
"""
from __future__ import annotations

import json
import re
import sqlite3
import threading
import zipfile
import zlib
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable

from ..live.paths import MODELS

DICT_DIR = MODELS / "dict"
DB_PATH = DICT_DIR / "dictionary.db"

_BLOCK_TAGS = {"div", "li", "p", "tr", "ol", "ul", "table", "details", "summary"}
# 这些部分在查词弹窗里没必要显示（异体字表、出处说明）
_DROP_CONTENT = {"attribution", "attribution-footnote", "forms"}
# 这些部分只在弹窗里显示，不写进卡片的简明释义（例句、交叉引用、注释）
_EXTRA_CONTENT = {"extra-info", "xref", "sense-note", "example-sentence", "redirect-glossary"}


@dataclass
class Entry:
    term: str
    reading: str
    glosses: list[str]          # 简明纯文本释义（每个义项一项）
    dict_title: str
    tags: str = ""
    html: str = ""              # 完整释义（含例句），弹窗里显示
    extra: dict = field(default_factory=dict)

    def short_meaning(self, limit: int = 3) -> str:
        return "；".join(g.replace("\n", " ") for g in self.glosses[:limit])


def _esc(text: str) -> str:
    return (str(text).replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;"))


def _content_of(node: dict) -> str:
    data = node.get("data")
    return str(data.get("content", "")) if isinstance(data, dict) else ""


def _render(node, html: bool) -> str:
    """把 Yomitan structured-content 渲染成 HTML（html=True）或简明纯文本。"""
    if node is None:
        return ""
    if isinstance(node, str):
        return _esc(node) if html else node
    if isinstance(node, list):
        return "".join(_render(n, html) for n in node)
    if not isinstance(node, dict):
        return _esc(node) if html else str(node)
    kind = node.get("type")
    if kind == "text":
        return _render(str(node.get("text", "")), html)
    if kind == "image":
        return ""
    if kind == "structured-content":
        return _render(node.get("content"), html)
    tag = node.get("tag", "")
    content = _content_of(node)
    if tag in ("rt", "rp", "img") or content in _DROP_CONTENT:
        return ""
    if not html and any(content.startswith(x) for x in _EXTRA_CONTENT):
        return ""
    data = node.get("data") if isinstance(node.get("data"), dict) else {}
    is_tag = data.get("class") == "tag" or ("title" in node and tag == "span")
    if not html:
        if is_tag:
            return ""
        if tag == "br":
            return "\n"
        if tag == "ul" and content == "glossary":
            raw = node.get("content")
            items = [_render(c, False).strip() for c in (raw if isinstance(raw, list) else [raw])]
            return "；".join(i for i in items if i) + "\n"
        inner = _render(node.get("content"), False)
        if tag in _BLOCK_TAGS or tag in ("td", "th"):
            return inner.strip() + ("\n" if tag not in ("td", "th") else " ")
        return inner
    inner = _render(node.get("content"), True)
    if tag == "br":
        return "<br>"
    if is_tag:
        return f'<span style="color:#b0567c;font-size:small">[{inner}]</span> '
    if content == "example-sentence-a":
        return f'<div style="color:#46536a;margin-left:12px">{inner}</div>'
    if content == "example-sentence-b":
        return f'<div style="color:#8a93a6;margin-left:12px">{inner}</div>'
    if content == "example-keyword":
        return f"<b>{inner}</b>"
    if content in ("xref", "sense-note", "redirect-glossary"):
        return f'<div style="color:#8a93a6">{inner}</div>'
    if tag == "ul" and content == "glossary":
        items = node.get("content") if isinstance(node.get("content"), list) else [node.get("content")]
        return "<span>" + "；".join(_render(c, True).replace("<li>", "").replace("</li>", "")
                                    for c in items) + "</span>"
    if tag in ("ul", "ol", "li", "table", "tr", "td", "th", "div"):
        return f"<{tag}>{inner}</{tag}>"
    if tag in ("details", "summary", "p"):
        return f"<div>{inner}</div>"
    return inner


def _gloss_list(raw) -> tuple[list[str], str]:
    texts, htmls = [], []
    for g in raw or []:
        if isinstance(g, list):                      # 活用形重定向 [原形, [规则]]
            g = f"→ {g[0]}" if g else ""
        text = re.sub(r"\n{2,}", "\n", _render(g, False)).strip()
        h = _render(g, True).strip()
        if text:
            texts.append(text)
        if h:
            htmls.append(h if not isinstance(g, str) else f"<div>• {h}</div>")
    return texts, "".join(htmls)


class Dictionary:
    """线程安全的只读查询 + 导入。"""

    def __init__(self, path: Path = DB_PATH):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.RLock()
        self._conn = sqlite3.connect(str(self.path), check_same_thread=False)
        self._conn.execute("PRAGMA journal_mode=WAL")
        self._conn.executescript(
            """
            CREATE TABLE IF NOT EXISTS dicts(
                id INTEGER PRIMARY KEY, title TEXT UNIQUE, revision TEXT,
                priority INTEGER DEFAULT 0, enabled INTEGER DEFAULT 1, terms INTEGER DEFAULT 0);
            CREATE TABLE IF NOT EXISTS terms(
                dict_id INTEGER, term TEXT, reading TEXT, tags TEXT, glossary TEXT, score INTEGER);
            CREATE INDEX IF NOT EXISTS idx_term ON terms(term);
            CREATE INDEX IF NOT EXISTS idx_reading ON terms(reading);
            """
        )
        self._conn.commit()

    # ------------------------------------------------------------ 管理
    def list_dicts(self) -> list[dict]:
        with self._lock:
            rows = self._conn.execute(
                "SELECT id,title,revision,priority,enabled,terms FROM dicts ORDER BY priority DESC, id"
            ).fetchall()
        return [dict(id=r[0], title=r[1], revision=r[2], priority=r[3], enabled=bool(r[4]), terms=r[5])
                for r in rows]

    def is_empty(self) -> bool:
        with self._lock:
            return self._conn.execute("SELECT 1 FROM dicts WHERE enabled=1 LIMIT 1").fetchone() is None

    def remove(self, dict_id: int) -> None:
        with self._lock:
            self._conn.execute("DELETE FROM terms WHERE dict_id=?", (dict_id,))
            self._conn.execute("DELETE FROM dicts WHERE id=?", (dict_id,))
            self._conn.commit()

    def set_enabled(self, dict_id: int, on: bool) -> None:
        with self._lock:
            self._conn.execute("UPDATE dicts SET enabled=? WHERE id=?", (1 if on else 0, dict_id))
            self._conn.commit()

    def set_priority(self, dict_id: int, priority: int) -> None:
        with self._lock:
            self._conn.execute("UPDATE dicts SET priority=? WHERE id=?", (priority, dict_id))
            self._conn.commit()

    def import_yomitan(self, zip_path: str | Path,
                       progress: Callable[[str], None] | None = None) -> tuple[str, int]:
        """导入 Yomitan 词典 zip，返回 (标题, 词条数)。同名词典会被替换。"""
        zip_path = Path(zip_path)
        with zipfile.ZipFile(zip_path) as zf:
            names = zf.namelist()
            index = {}
            if "index.json" in names:
                index = json.loads(zf.read("index.json").decode("utf-8-sig"))
            title = str(index.get("title") or zip_path.stem)
            revision = str(index.get("revision") or "")
            banks = sorted(n for n in names if re.match(r"(.*/)?term_bank_\d+\.json$", n))
            if not banks:
                raise ValueError("这个 zip 里没有 term_bank_*.json，不是 Yomitan 词条词典"
                                 "（音调/频率词典暂不支持）")
            with self._lock:
                old = self._conn.execute("SELECT id, priority FROM dicts WHERE title=?", (title,)).fetchone()
                if old:
                    self.remove(old[0])
                prio = old[1] if old else -len(self.list_dicts())   # 后导入的排在后面
                cur = self._conn.execute("INSERT INTO dicts(title,revision,priority) VALUES(?,?,?)",
                                         (title, revision, prio))
                dict_id = cur.lastrowid
                total = 0
                for i, bank in enumerate(banks, 1):
                    rows = json.loads(zf.read(bank).decode("utf-8-sig"))
                    batch = []
                    for r in rows:
                        if not isinstance(r, list) or len(r) < 6:
                            continue
                        glosses, html = _gloss_list(r[5])
                        if not glosses and not html:
                            continue
                        tags = " ".join(t for t in (str(r[2] or ""), str(r[7] if len(r) > 7 else "")) if t)
                        batch.append((dict_id, str(r[0]), str(r[1] or ""), tags.strip(),
                                      zlib.compress(json.dumps({"t": glosses, "h": html}, ensure_ascii=False).encode("utf-8"), 6),
                                      int(r[4] or 0)))
                    self._conn.executemany(
                        "INSERT INTO terms(dict_id,term,reading,tags,glossary,score) VALUES(?,?,?,?,?,?)", batch)
                    total += len(batch)
                    if progress:
                        progress(f"{title}：{i}/{len(banks)}，已导入 {total} 条")
                self._conn.execute("UPDATE dicts SET terms=? WHERE id=?", (total, dict_id))
                self._conn.commit()
            try:
                self._conn.execute("VACUUM")     # 替换旧词典后回收空间
            except sqlite3.Error:
                pass
        return title, total

    # ------------------------------------------------------------ 查询
    def lookup(self, *candidates: str, reading: str = "", limit: int = 12) -> list[Entry]:
        """按候选词（原形、表层形…）依次查，先命中的优先；读音相同的词条排前面。"""
        seen = set()
        results: list[Entry] = []
        with self._lock:
            for word in [c for c in candidates if c]:
                rows = self._conn.execute(
                    """SELECT t.term,t.reading,t.tags,t.glossary,d.title FROM terms t
                       JOIN dicts d ON d.id=t.dict_id
                       WHERE d.enabled=1 AND (t.term=? OR (t.reading=? AND t.reading<>''))
                       ORDER BY (t.term=?) DESC, (t.reading=?) DESC, d.priority DESC, t.score DESC
                       LIMIT ?""",
                    (word, word, word, reading or "\0", limit * 3),
                ).fetchall()
                for term, rd, tags, gloss, title in rows:
                    key = (term, rd, title, bytes(gloss) if isinstance(gloss, (bytes, memoryview)) else gloss)
                    if key in seen:
                        continue
                    seen.add(key)
                    if isinstance(gloss, bytes):
                        gloss = zlib.decompress(gloss).decode("utf-8")
                    data = json.loads(gloss)
                    if isinstance(data, list):
                        data = {"t": data, "h": "".join(f"<div>• {_esc(x)}</div>" for x in data)}
                    results.append(Entry(term, rd, data.get("t") or [], title, tags or "", data.get("h") or ""))
                if results:
                    break
        # 同一词典同一词条的多行合并，界面更紧凑
        merged: dict[tuple, Entry] = {}
        for e in results:
            k = (e.term, e.reading, e.dict_title)
            if k in merged:
                merged[k].glosses.extend(g for g in e.glosses if g not in merged[k].glosses)
                merged[k].html += e.html
            else:
                merged[k] = e
        return list(merged.values())[:limit]


_instance: Dictionary | None = None


def get() -> Dictionary:
    global _instance
    if _instance is None:
        _instance = Dictionary()
    return _instance
