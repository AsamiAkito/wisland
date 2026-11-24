"""账户用量页。每个数据源独立刷新，网络查询均在后台运行。"""
from __future__ import annotations

import time

import httpx
from PySide6 import QtCore, QtWidgets

from ...core.bg import run_bg
from ...services.usage import codex_limits, latest_session_tokens, token_totals
from ..theme import G, W, theme, ui_font
from ..widgets import Card, IconButton, Label, ScrollArea, hbox, vbox
from .base import Page


class UsagePage(Page):
    key, glyph, title, height = "usage", G.PULSE, "用量", 330

    def __init__(self, ctx):
        super().__init__(ctx)
        self.pending = 0
        self.updated = 0.0
        self.refresh_btn = IconButton(G.REFRESH, 28, 12, "刷新用量")
        self.refresh_btn.clicked.connect(self.refresh)
        self.status = Label("", 11, level=3)
        self.fields = {}
        scroll = ScrollArea()
        cards = []
        for provider, keys in (("DeepSeek", ("tokens", "balance")), ("Codex", ("session", "limits"))):
            card = Card(radius=14)
            labels = []
            for key in keys:
                label = QtWidgets.QLabel("读取中…")
                label.setTextFormat(QtCore.Qt.TextFormat.PlainText)
                label.setWordWrap(True)
                label.setFont(ui_font(13))
                self.fields[key] = label
                labels.append(label)
            card.setLayout(vbox(Label(provider, 15, W.DemiBold), *labels, spacing=5, margins=(14, 10, 14, 10)))
            cards.append(card)
        scroll.inner.setLayout(vbox(*cards, None))
        self.setLayout(vbox(hbox(None, self.status, self.refresh_btn), (scroll, 1)))
        theme.changed.connect(self._style)
        self._style()

    def _style(self):
        for label in self.fields.values():
            label.setStyleSheet(f"background:transparent;color:{theme.qss_color(theme.text2())}")

    def on_show(self):
        super().on_show()
        if not self.updated or time.monotonic() - self.updated > 60:
            self.refresh()

    def refresh(self):
        if self.pending:
            return
        def tokens():
            inputs, outputs = token_totals()
            return f"Wisland 已记录 {inputs + outputs:,} tokens · 输入 {inputs:,} · 输出 {outputs:,}"

        def session():
            count = latest_session_tokens()
            return "最近本机会话暂无 token 记录" if count is None else f"最近本机会话累计 {count:,} tokens"

        jobs = {"tokens": tokens, "balance": self.ctx.translate.balance, "session": session, "limits": codex_limits}
        self.pending = len(jobs)
        self.refresh_btn.setEnabled(False)
        self.status.setText("正在刷新…")
        for key, fn in jobs.items():
            run_bg(fn, on_done=lambda value, k=key: self._done(k, value),
                   on_error=lambda exc, k=key: self._error(k, exc))

    def _error(self, key, exc):
        if isinstance(exc, httpx.HTTPStatusError):
            code = exc.response.status_code
            message = "请检查 DeepSeek API Key" if code == 401 else f"余额查询失败（HTTP {code}）"
        elif key == "limits" and isinstance(exc, RuntimeError):
            message = str(exc)
        else:
            message = {"tokens": "本地用量记录读取失败", "session": "本机会话记录读取失败",
                       "limits": "无法读取 Codex 额度，请检查 CLI 和登录状态",
                       "balance": "无法读取余额，请检查网络后刷新"}[key]
        self._done(key, message)

    def _done(self, key, value):
        self.fields[key].setText(value)
        self.pending -= 1
        if not self.pending:
            self.updated = time.monotonic()
            self.status.setText(time.strftime("%H:%M 更新"))
            self.refresh_btn.setEnabled(True)
