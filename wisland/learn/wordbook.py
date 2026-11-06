# -*- coding: utf-8 -*-
"""单词本：保存查过/想记的词，带例句、译文、例句音频与截图。SQLite 本地存储。"""
from __future__ import annotations

import sqlite3
import threading
import time
import wave
from dataclasses import dataclass
from pathlib import Path

import numpy as np

from ..core.config import DATA_DIR

WORDBOOK_DIR = DATA_DIR / "wordbook"
MEDIA_DIR = WORDBOOK_DIR / "media"
DB_PATH = WORDBOOK_DIR / "wordbook.db"


@dataclass
class Card:
    id: int
    word: str
    reading: str
    meaning: str
    sentence: str
    sentence_zh: str
    audio: str        # media 目录下的文件名
    image: str
    source: str
    created: float
    exported: int     # 0=未导出 1=已导出 .apkg 2=已推送 AnkiDroid
    note: str = ""


def save_wav(path: Path, audio: np.ndarray, rate: int = 16000) -> None:
    pcm = np.clip(audio.astype(np.float32), -1.0, 1.0)
    peak = float(np.max(np.abs(pcm))) if pcm.size else 0.0
    if 0 < peak < 0.5:                       # 系统声音偏小时适当放大
        pcm = pcm * (0.8 / peak)
    data = (pcm * 32767).astype("<i2").tobytes()
    with wave.open(str(path), "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(rate)
        w.writeframes(data)


class Wordbook:
    COLS = "id,word,reading,meaning,sentence,sentence_zh,audio,image,source,created,exported,note"

    def __init__(self, path: Path = DB_PATH):
        self.path = Path(path)
        MEDIA_DIR.mkdir(parents=True, exist_ok=True)
        self._lock = threading.RLock()
        self._conn = sqlite3.connect(str(self.path), check_same_thread=False)
        self._conn.execute(
            """CREATE TABLE IF NOT EXISTS cards(
                id INTEGER PRIMARY KEY AUTOINCREMENT, word TEXT, reading TEXT, meaning TEXT,
                sentence TEXT, sentence_zh TEXT, audio TEXT, image TEXT, source TEXT,
                created REAL, exported INTEGER DEFAULT 0, note TEXT DEFAULT '')""")
        self._conn.commit()
        self._listeners = []

    def on_change(self, fn) -> None:
        self._listeners.append(fn)

    def _changed(self) -> None:
        for fn in list(self._listeners):
            try:
                fn()
            except Exception:  # noqa: BLE001
                pass

    def add(self, word: str, reading: str, meaning: str, sentence: str = "", sentence_zh: str = "",
            audio: np.ndarray | None = None, image=None, source: str = "") -> int:
        stamp = time.strftime("%Y%m%d_%H%M%S") + f"_{int(time.time() * 1000) % 1000:03d}"
        audio_name = image_name = ""
        if audio is not None and getattr(audio, "size", 0) > 1600:
            audio_name = f"yisheng_{stamp}.wav"
            save_wav(MEDIA_DIR / audio_name, audio)
        if image is not None:               # QImage / QPixmap
            image_name = f"yisheng_{stamp}.jpg"
            if not image.save(str(MEDIA_DIR / image_name), "JPG", 80):
                image_name = ""
        with self._lock:
            cur = self._conn.execute(
                "INSERT INTO cards(word,reading,meaning,sentence,sentence_zh,audio,image,source,created)"
                " VALUES(?,?,?,?,?,?,?,?,?)",
                (word, reading, meaning, sentence, sentence_zh, audio_name, image_name, source, time.time()))
            self._conn.commit()
            new_id = cur.lastrowid
        self._changed()
        return new_id

    def count(self) -> int:
        with self._lock:
            return int(self._conn.execute("SELECT COUNT(*) FROM cards").fetchone()[0])

    def has_word(self, word: str) -> bool:
        with self._lock:
            return self._conn.execute("SELECT 1 FROM cards WHERE word=? LIMIT 1", (word,)).fetchone() is not None

    def all(self, search: str = "", only_new: bool = False) -> list[Card]:
        sql = f"SELECT {self.COLS} FROM cards"
        cond, args = [], []
        if search:
            cond.append("(word LIKE ? OR reading LIKE ? OR meaning LIKE ? OR sentence LIKE ?)")
            args += [f"%{search}%"] * 4
        if only_new:
            cond.append("exported=0")
        if cond:
            sql += " WHERE " + " AND ".join(cond)
        sql += " ORDER BY created DESC"
        with self._lock:
            return [Card(*r) for r in self._conn.execute(sql, args).fetchall()]

    def get(self, card_id: int) -> Card | None:
        with self._lock:
            r = self._conn.execute(f"SELECT {self.COLS} FROM cards WHERE id=?", (card_id,)).fetchone()
        return Card(*r) if r else None

    def update(self, card_id: int, **fields) -> None:
        allowed = {"word", "reading", "meaning", "sentence", "sentence_zh", "source", "exported", "note"}
        fields = {k: v for k, v in fields.items() if k in allowed}
        if not fields:
            return
        with self._lock:
            self._conn.execute(f"UPDATE cards SET {', '.join(k + '=?' for k in fields)} WHERE id=?",
                               (*fields.values(), card_id))
            self._conn.commit()
        self._changed()

    def mark_exported(self, ids: list[int], level: int) -> None:
        with self._lock:
            self._conn.executemany("UPDATE cards SET exported=MAX(exported, ?) WHERE id=?",
                                   [(level, i) for i in ids])
            self._conn.commit()
        self._changed()

    def delete(self, ids: list[int]) -> None:
        with self._lock:
            for i in ids:
                card = self.get(i)
                if card:
                    for name in (card.audio, card.image):
                        if name:
                            (MEDIA_DIR / name).unlink(missing_ok=True)
                self._conn.execute("DELETE FROM cards WHERE id=?", (i,))
            self._conn.commit()
        self._changed()


_instance: Wordbook | None = None


def get() -> Wordbook:
    global _instance
    if _instance is None:
        _instance = Wordbook()
    return _instance
