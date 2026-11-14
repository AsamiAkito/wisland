# -*- coding: utf-8 -*-
"""实时字幕服务：包装移植来的 Pipeline，供翻译页和灵动岛收起态使用。"""
from __future__ import annotations

import json
import os
import threading

from PySide6 import QtCore

from ..core.config import cfg, expand
from .download import DownloadCancelled, download_models, models_ready


class _LiveConfig:
    """给 Pipeline / Translator 用的配置适配器：读 wisland config.json 的 live.* 节，
    翻译 Key 与 wisland 翻译页共用（translate.api_key，或 translate.import_from 指向的译声配置）。"""

    def get(self, path: str, default=None):
        if path == "translate.api_key":
            return _api_key()
        if path in ("translate.base_url", "translate.model"):
            v = cfg.get(path)
            if v:
                return v
        v = cfg.get("live." + path)
        return default if v is None else v

    def set(self, path, value, save=True):
        cfg.set("live." + path, value, save)


def _api_key() -> str:
    key = str(cfg.get("translate.api_key") or "").strip()
    if key:
        return key
    src = expand(cfg.get("translate.import_from") or "")
    if src and os.path.exists(src):
        try:
            return str(json.load(open(src, encoding="utf-8")).get("translate", {}).get("api_key") or "")
        except Exception:  # noqa: BLE001
            return ""
    return ""


class LiveService(QtCore.QObject):
    status = QtCore.Signal(str)
    error = QtCore.Signal(str)
    partial = QtCore.Signal(str)                 # 正在说的日语（流式草稿）
    segment = QtCore.Signal(str, str)            # (sid, 日语整句)
    live_zh = QtCore.Signal(str, str)            # (sid, 实时译文)
    final_zh = QtCore.Signal(str, str)           # (sid, 最终译文)
    level = QtCore.Signal(float)
    running_changed = QtCore.Signal(bool)
    download_started = QtCore.Signal()
    downloading_changed = QtCore.Signal(bool)
    download_progress = QtCore.Signal(int, int, str)   # 已下载字节、总字节（0=未知）、阶段
    download_note = QtCore.Signal(str)
    download_done = QtCore.Signal(bool, str)

    def __init__(self, parent=None):
        super().__init__(parent)
        self.pipe = None
        self.running = False
        self.downloading = False
        self._dl_cancel: threading.Event | None = None
        # numpy / sherpa-onnx 导入要 0.5s 以上：启动后在后台预先导入，点「开始聆听」时不卡界面
        from ..core.bg import run_bg
        run_bg(lambda: __import__("wisland.live.pipeline"))

    def available(self) -> tuple[bool, str]:
        if not models_ready():
            return False, "首次使用需要下载识别模型（约 630 MB）"
        return True, ""

    def toggle(self):
        if self.downloading:
            self.cancel_download()
            return
        self.stop() if self.running else self.start_listen()

    def start_listen(self):
        if self.running or self.downloading:
            return
        ok, _msg = self.available()
        if not ok:
            self._start_download()
            return
        from .pipeline import Pipeline       # 重依赖（numpy / sherpa-onnx）用到时才导入
        p = Pipeline(_LiveConfig())
        p.sig_status.connect(self.status)
        p.sig_error.connect(self.error)
        p.sig_partial.connect(self.partial)
        p.sig_segment.connect(lambda seg: self.segment.emit(seg.sid, seg.jp))
        p.sig_live_zh.connect(self.live_zh)
        p.sig_final_zh.connect(self.final_zh)
        p.sig_level.connect(self.level)
        p.finished.connect(lambda p=p: self._finished() if self.pipe is p else None)
        self.pipe = p
        self.running = True
        p.start()
        self.running_changed.emit(True)

    def _start_download(self):
        if self.downloading or self.running:
            return
        self.downloading = True
        self._dl_cancel = threading.Event()
        self.download_started.emit()
        self.downloading_changed.emit(True)
        from ..core.bg import run_bg
        run_bg(lambda: download_models(self._dl_cancel, self._download_progress, self.download_note.emit),
               on_done=lambda _res: self._download_finished(True, ""),
               on_error=self._download_error)

    def _download_progress(self, done: int, total: int, label: str):
        if self.downloading:
            self.download_progress.emit(int(done), int(total), label)

    def _download_error(self, e: Exception):
        if isinstance(e, DownloadCancelled):
            self._download_finished(False, "已取消下载；再点「开始聆听」会从断点继续。")
        else:
            self._download_finished(False, f"模型下载失败：{e}")

    def _download_finished(self, ok: bool, msg: str):
        if not self.downloading:
            return
        self.downloading = False
        self._dl_cancel = None
        self.downloading_changed.emit(False)
        self.download_done.emit(ok, msg)
        if ok:
            ready, err = self.available()
            if not ready:
                self.error.emit(err or "模型文件校验失败")
                return
            try:
                self.start_listen()
            except Exception as e:  # noqa: BLE001
                self.error.emit(f"模型已下载，但启动字幕失败：{e}")

    def cancel_download(self):
        if not self.downloading:
            return
        if self._dl_cancel is not None:
            self._dl_cancel.set()
        self.download_note.emit("正在取消下载…")

    def clip(self, sid: str):
        for pipe in (self.pipe, getattr(self, "_last_pipe", None)):
            if pipe is not None and sid:
                try:
                    a = pipe.get_clip(sid)
                    if a is not None:
                        return a
                except Exception:  # noqa: BLE001
                    pass
        return None

    def stop(self, wait: bool = False):
        """停止聆听。等待音频线程结束放到后台，不阻塞界面（退出程序时 wait=True）。"""
        if self.downloading:
            self.cancel_download()
        p, self.pipe = self.pipe, None
        if p is not None:
            self._last_pipe = p
        self._finished()
        if p is None:
            return
        if wait:
            try:
                p.stop()
            except Exception:  # noqa: BLE001
                pass
            return
        from ..core.bg import run_bg
        self._stopping = p                     # 保持引用直到线程结束
        run_bg(lambda: p.stop(), on_done=lambda _r: setattr(self, "_stopping", None))

    def _finished(self):
        if self.running:
            self.running = False
            self.running_changed.emit(False)
        self.pipe = None

    def start(self):
        pass
