# -*- coding: utf-8 -*-
"""实时字幕服务：包装移植来的 Pipeline，供翻译页和灵动岛收起态使用。"""
from __future__ import annotations

import json
import os

from PySide6 import QtCore

from ..core.config import cfg, expand


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

    def __init__(self, parent=None):
        super().__init__(parent)
        self.pipe = None
        self.running = False
        # numpy / sherpa-onnx 导入要 0.5s 以上：启动后在后台预先导入，点「开始聆听」时不卡界面
        from ..core.bg import run_bg
        run_bg(lambda: __import__("wisland.live.pipeline"))

    def available(self) -> tuple[bool, str]:
        from .paths import MODELS
        if not (MODELS / "parakeet-ja-0.6b-int8").exists() or not (MODELS / "silero_vad.onnx").exists():
            return False, f"缺少识别模型：{MODELS}"
        return True, ""

    def toggle(self):
        self.stop() if self.running else self.start_listen()

    def start_listen(self):
        if self.running:
            return
        ok, msg = self.available()
        if not ok:
            self.error.emit(msg)
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
