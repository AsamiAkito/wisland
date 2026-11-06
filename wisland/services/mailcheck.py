# -*- coding: utf-8 -*-
"""不打开 Thunderbird 也能收到新邮件提醒：直接用 IMAP 查收件箱的未读邮件。

- 账户列表首次自动从 Thunderbird 的 prefs.js 读取（只有服务器和用户名，Thunderbird 不保存可读的密码），
  写入 config.json → mail.imap，每个账户需要你自己填入授权码 / 应用专用密码（password）后才会检查；
- Thunderbird 正在运行时不检查（由 Thunderbird 自己的通知负责，避免重复提醒）；
- 只统计数量，不读取邮件内容。
"""
from __future__ import annotations

import imaplib
import re
import time
import traceback

import psutil
from PySide6 import QtCore

from ..core.bg import run_bg, ui
from ..core.config import cfg
from .notify import thunderbird_profile


def thunderbird_accounts() -> list[dict]:
    prof = thunderbird_profile()
    if not prof:
        return []
    try:
        text = (prof / "prefs.js").read_text(encoding="utf-8", errors="ignore")
    except OSError:
        return []
    servers: dict[str, dict] = {}
    for m in re.finditer(r'user_pref\("mail\.server\.(server\d+)\.(hostname|port|type|userName)",\s*"?([^")]*)"?\);', text):
        servers.setdefault(m.group(1), {})[m.group(2)] = m.group(3)
    out = []
    for s in servers.values():
        if s.get("type") == "imap" and s.get("hostname") and s.get("userName"):
            out.append({"host": s["hostname"], "port": int(s.get("port") or 993), "user": s["userName"],
                        "password": "", "enabled": True})
    return out


def thunderbird_running() -> bool:
    for p in psutil.process_iter(["name"]):
        if (p.info["name"] or "").lower() == "thunderbird.exe":
            return True
    return False


class MailCheckService(QtCore.QObject):
    new_mail = QtCore.Signal(str, int)        # 账户, 新邮件数

    def __init__(self, parent=None):
        super().__init__(parent)
        ui(lambda: None)
        self._known: dict[str, set] = {}
        self._busy = False
        self._timer = QtCore.QTimer(self)
        self._timer.timeout.connect(self.check)

    def start(self):
        if not cfg.get("mail.imap"):
            accts = thunderbird_accounts()
            if accts:
                cfg.set("mail.imap", accts)
        sec = max(30, int(cfg.get("mail.interval_s") or 120))
        self._timer.start(sec * 1000)
        QtCore.QTimer.singleShot(8000, self.check)

    def stop(self):
        self._timer.stop()

    def _accounts(self) -> list[dict]:
        return [a for a in (cfg.get("mail.imap") or [])
                if isinstance(a, dict) and a.get("enabled", True) and a.get("password")]

    def check(self):
        accts = self._accounts()
        if self._busy or not accts:
            return
        self._busy = True
        run_bg(self._check_all, accts, on_done=self._done, on_error=lambda e: setattr(self, "_busy", False))

    @staticmethod
    def _unseen(a: dict) -> set:
        m = imaplib.IMAP4_SSL(a["host"], int(a.get("port") or 993), timeout=20)
        try:
            m.login(a["user"], a["password"])
            # 部分服务商（163）要求先发送客户端 ID
            try:
                imaplib.Commands.setdefault("ID", ("AUTH", "SELECTED", "NONAUTH"))
                m._simple_command("ID", '("name" "wisland" "version" "1.0")')
            except Exception:  # noqa: BLE001
                pass
            m.select("INBOX", readonly=True)
            typ, data = m.uid("search", None, "UNSEEN")
            return set((data[0] or b"").split()) if typ == "OK" else set()
        finally:
            try:
                m.logout()
            except Exception:  # noqa: BLE001
                pass

    def _check_all(self, accts: list[dict]) -> list[tuple[str, set | None]]:
        out = []
        for a in accts:
            try:
                out.append((a["user"], self._unseen(a)))
            except Exception as e:  # noqa: BLE001
                print(f"IMAP 检查失败 {a.get('host')}: {str(e)[:80]}")
                out.append((a["user"], None))
        return out

    def _done(self, res):
        self._busy = False
        tb = thunderbird_running()
        for user, uids in res:
            if uids is None:
                continue
            first = user not in self._known
            new = uids - self._known.get(user, set())
            self._known[user] = uids
            if not first and new and not tb:
                self.new_mail.emit(user, len(new))


_ = (time, traceback)
