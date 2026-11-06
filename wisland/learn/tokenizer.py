# -*- coding: utf-8 -*-
"""日语分词：fugashi(MeCab) + unidic-lite，完全离线。

给每个词返回原形（辞書形）与读音，供字幕点词查词使用。
分词器第一次使用时才加载（约 0.5 秒），避免拖慢软件启动。
"""
from __future__ import annotations

import threading
from dataclasses import dataclass

# 这些词性值得点击查词；助词/助动词/符号只显示不链接
CONTENT_POS = {"名詞", "動詞", "形容詞", "副詞", "形状詞", "連体詞", "感動詞", "代名詞", "接続詞", "接頭辞"}
_NON_WORD_POS2 = {"数詞"}

_lock = threading.Lock()
_tagger = None
_failed = ""


@dataclass
class Token:
    surface: str
    lemma: str        # 辞書形，用于查词
    reading: str      # 原形的平假名读音
    pos: str
    clickable: bool


def kata_to_hira(text: str) -> str:
    return "".join(chr(ord(c) - 0x60) if "ァ" <= c <= "ヶ" else c for c in text or "")


def _get_tagger():
    global _tagger, _failed
    if _tagger is not None or _failed:
        return _tagger
    with _lock:
        if _tagger is None and not _failed:
            try:
                import fugashi  # noqa: WPS433 - 延迟导入
                _tagger = fugashi.Tagger()
            except Exception as e:  # noqa: BLE001
                _failed = f"{type(e).__name__}: {e}"
    return _tagger


def available() -> bool:
    return _get_tagger() is not None


def error() -> str:
    return _failed


def _feat(word, name: str) -> str:
    value = getattr(word.feature, name, None)
    return "" if value in (None, "*") else str(value)


def tokenize(text: str) -> list[Token]:
    """分词；分词器不可用时整句作为一个不可点击的块返回。"""
    tagger = _get_tagger()
    if tagger is None or not text:
        return [Token(text, text, "", "", False)] if text else []
    out: list[Token] = []
    pos_cursor = 0
    for word in tagger(text):
        surface = word.surface
        # MeCab 会吃掉空白，这里把原文中的空白补回来，保证显示与原句一致
        idx = text.find(surface, pos_cursor)
        if idx > pos_cursor:
            gap = text[pos_cursor:idx]
            out.append(Token(gap, gap, "", "空白", False))
        if idx >= 0:
            pos_cursor = idx + len(surface)
        pos1 = _feat(word, "pos1")
        pos2 = _feat(word, "pos2")
        lemma = _feat(word, "orthBase") or _feat(word, "lemma") or surface
        lemma = lemma.split("-")[0] or surface   # unidic 外来语 lemma 形如 "ゲーム-game"
        reading = kata_to_hira(_feat(word, "kanaBase") or _feat(word, "kana"))
        clickable = (pos1 in CONTENT_POS and pos2 not in _NON_WORD_POS2
                     and not surface.isascii() and bool(surface.strip()))
        out.append(Token(surface, lemma, reading, pos1, clickable))
    if pos_cursor < len(text):
        rest = text[pos_cursor:]
        out.append(Token(rest, rest, "", "空白", False))
    return out
