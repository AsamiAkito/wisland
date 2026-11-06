# -*- coding: utf-8 -*-
"""学习服务：分词、查词、加入单词本、同步到 Anki（AnkiConnect）、导出 .apkg。"""
from __future__ import annotations

import ctypes
import time
from ctypes import wintypes

from PySide6 import QtCore, QtGui

from ..core.bg import run_bg, ui
from ..core.config import cfg


def meaning_for_card(entries, limit_lines: int = 6) -> str:
    """把词典条目压成卡片上的简明释义（最多两部词典，例如中文 + 英文）。"""
    if not entries:
        return ""
    lines = []
    first = entries[0]
    for e in entries:
        if e.term != first.term or e.reading != first.reading:
            continue
        glosses = e.glosses
        if len(glosses) > 1 and all("\n" not in g for g in glosses):
            text = "；".join(glosses[:8])
        else:
            senses = [s.strip() for s in "\n".join(glosses).split("\n") if s.strip()]
            nums = "①②③④⑤⑥⑦⑧⑨⑩"
            text = "\n".join(f"{nums[i]} {s}" if len(senses) > 1 and i < len(nums) else s
                             for i, s in enumerate(senses[:limit_lines]))
        if text and text not in lines:
            lines.append(text)
        if len(lines) >= 2:
            break
    rows = [r.strip() for r in "\n".join(lines).split("\n") if r.strip()]
    return "\n".join(rows[:limit_lines])


# ----------------------------------------------------------------- 播放器截图（卡片配图，可选）
_u32 = ctypes.WinDLL("user32")


def _find_window(title_part: str) -> int:
    if not title_part:
        return 0
    found = []
    proc = ctypes.WINFUNCTYPE(ctypes.c_bool, wintypes.HWND, wintypes.LPARAM)

    def cb(h, _lp):
        if _u32.IsWindowVisible(h):
            buf = ctypes.create_unicode_buffer(256)
            _u32.GetWindowTextW(h, buf, 256)
            if title_part.lower() in buf.value.lower():
                found.append(h)
                return False
        return True
    _u32.EnumWindows(proc(cb), 0)
    return found[0] if found else 0


def grab_player_image() -> QtGui.QImage | None:
    """截取播放器窗口（learn.player_title，找不到就截光标所在屏幕），缩到 960 宽。"""
    near = QtGui.QCursor.pos()
    screen = QtGui.QGuiApplication.screenAt(near) or QtGui.QGuiApplication.primaryScreen()
    pm = screen.grabWindow(0) if screen else None
    if pm is None or pm.isNull():
        return None
    img = pm.toImage()
    h = _find_window(str(cfg.get("learn.player_title") or ""))
    if h:
        r = wintypes.RECT()
        _u32.GetWindowRect(h, ctypes.byref(r))
        dpr = screen.devicePixelRatio()
        o = screen.geometry().topLeft()
        crop = QtCore.QRect(r.left - int(o.x() * dpr), r.top - int(o.y() * dpr),
                            r.right - r.left, r.bottom - r.top).intersected(img.rect())
        if crop.width() > 200 and crop.height() > 150:
            img = img.copy(crop)
    if img.width() > 960:
        img = img.scaledToWidth(960, QtCore.Qt.TransformationMode.SmoothTransformation)
    return img


class LearnService(QtCore.QObject):
    notice = QtCore.Signal(str)          # 给灵动岛 / 页面的提示
    changed = QtCore.Signal()            # 单词本有变化

    def __init__(self, parent=None):
        super().__init__(parent)
        self._syncing = False
        self._tok_cache: dict[str, list] = {}
        self._timer = QtCore.QTimer(self, interval=20000, timeout=self.auto_sync)
        # 分词器 / 词典首次加载较慢：后台预热
        run_bg(self._warm)

    @staticmethod
    def _warm():
        from . import dictionary, tokenizer
        tokenizer.tokenize("こんにちは")
        dictionary.get()

    def start(self):
        from . import wordbook
        wordbook.get().on_change(lambda: ui(self.changed.emit))     # 回调可能来自后台线程
        self._timer.start()
        QtCore.QTimer.singleShot(5000, self.auto_sync)

    def stop(self):
        self._timer.stop()

    # ---------- 分词 / 查词 ----------
    def tokens(self, text: str) -> list:
        if text in self._tok_cache:
            return self._tok_cache[text]
        from . import tokenizer
        try:
            toks = tokenizer.tokenize(text) if tokenizer.available() else []
        except Exception:  # noqa: BLE001
            toks = []
        if len(self._tok_cache) > 400:
            self._tok_cache.clear()
        self._tok_cache[text] = toks
        return toks

    def lookup(self, word: str, surface: str = "", reading: str = "") -> tuple[list, str]:
        """返回 (词条, 错误信息)。"""
        from . import dictionary
        try:
            d = dictionary.get()
            if d.is_empty():
                return [], "未安装词典（scripts/download_models.py --dict）"
            return d.lookup(word, surface, reading=reading), ""
        except Exception as e:  # noqa: BLE001
            return [], f"词典读取失败：{e}"

    def has_word(self, word: str) -> bool:
        from . import wordbook
        return wordbook.get().has_word(word)

    # ---------- 单词本 ----------
    def add_card(self, *, word, reading, meaning, sentence="", sentence_zh="", audio=None,
                 push: bool = False) -> int:
        from . import wordbook
        image = grab_player_image() if cfg.get("learn.screenshot", False) else None
        card_id = wordbook.get().add(word, reading, meaning, sentence, sentence_zh, audio, image,
                                     source=str(cfg.get("learn.source", "") or ""))
        short = (meaning or "").splitlines()[0][:18] if meaning else ""
        self.notice.emit(f"已加入单词本 · {word}" + (f"  {short}" if short else ""))
        if push:
            self.push_cards([card_id])
        elif cfg.get("learn.auto_sync", False):
            QtCore.QTimer.singleShot(800, self.auto_sync)
        return card_id

    def cards(self, search: str = "") -> list:
        from . import wordbook
        return wordbook.get().all(search)

    def delete(self, ids: list[int]):
        from . import wordbook
        wordbook.get().delete(ids)

    # ---------- Anki ----------
    def anki_url(self) -> str:
        url = str(cfg.get("learn.ankiconnect_url", "") or "").strip()
        return url or "127.0.0.1:8765"

    def push_cards(self, ids: list[int], silent: bool = False):
        """发送到 AnkiConnect；silent=True 时 Anki 没开就安静放弃（自动同步用）。"""
        if self._syncing or not ids:
            return
        self._syncing = True
        url = self.anki_url()
        deck = str(cfg.get("learn.deck", "日语::生词") or "日语::生词")

        def job():
            from . import wordbook
            from .anki import AnkiConnect
            client = AnkiConnect(url, timeout=2.0 if silent else 6.0)
            try:
                client.version()
            except Exception:  # noqa: BLE001
                if silent:
                    return None
                raise
            client.timeout = 10.0
            client.ensure_model()
            try:
                if deck not in client.deck_names():
                    client.call("createDeck", deck=deck)
            except Exception:  # noqa: BLE001  AnkiConnect Android 不支持 createDeck
                pass
            book = wordbook.get()
            ok, errors = [], []
            for i in ids:
                card = book.get(i)
                if card is None:
                    continue
                try:
                    client.add_card(card, deck)
                    ok.append(i)
                except Exception as e:  # noqa: BLE001
                    if "duplicate" in str(e).lower():
                        ok.append(i)
                    else:
                        errors.append(f"{card.word}：{e}")
            book.mark_exported(ok, 2)
            return ok, errors

        def done(result):
            self._syncing = False
            if result is None:
                return
            ok, errors = result
            if ok or errors:
                msg = f"已同步 {len(ok)} 个词到 Anki"
                if errors:
                    msg += f"，失败 {len(errors)}：{errors[0]}"
                self.notice.emit(msg)
            self.changed.emit()

        def fail(e):
            self._syncing = False
            if not silent:
                self.notice.emit(f"同步失败：Anki 未打开或未安装 AnkiConnect（{str(e)[:40]}）")

        run_bg(job, on_done=done, on_error=fail)

    def unsynced_ids(self) -> list[int]:
        return [c.id for c in self.cards() if c.exported < 2]

    def auto_sync(self):
        if cfg.get("learn.auto_sync", False) and not self._syncing:
            ids = self.unsynced_ids()
            if ids:
                self.push_cards(ids, silent=True)

    def export_apkg(self, path: str) -> int:
        from . import anki, wordbook
        book = wordbook.get()
        ids = [c.id for c in book.all() if c.exported < 1]
        cards = [c for c in (book.get(i) for i in ids) if c]
        n = anki.export_apkg(cards, path, str(cfg.get("learn.deck", "日语::生词") or "日语::生词"))
        book.mark_exported(ids, 1)
        return n


_ = time
