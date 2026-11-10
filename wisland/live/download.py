# -*- coding: utf-8 -*-
"""在 wisland 里直接下载实时字幕模型（约 630MB），不用另开命令行。

- 依次尝试 HF_ENDPOINT 环境变量、huggingface.co、hf-mirror.com（国内镜像）
- 有系统代理（Clash 等）时先走代理，失败再直连
- 断点续传：下载中断后再点一次会接着下（临时文件 *.part）
"""
from __future__ import annotations

import os
import threading
from pathlib import Path

import httpx
from PySide6 import QtCore

from .http_util import system_proxy
from .paths import MODELS

PARAKEET = "csukuangfj/sherpa-onnx-nemo-parakeet-tdt_ctc-0.6b-ja-35000-int8"
VAD_REPO = "csukuangfj/sherpa-onnx-vad-models"
VAD_GITHUB = "https://github.com/k2-fsa/sherpa-onnx/releases/download/asr-models/silero_vad.onnx"

# (保存位置, 仓库, 仓库内文件名, 大概大小 字节 —— 拿不到 Content-Length 时用来估算进度)
FILES = [
    ("silero_vad.onnx", VAD_REPO, "silero_vad.onnx", 644_000),
    ("parakeet-ja-0.6b-int8/tokens.txt", PARAKEET, "tokens.txt", 29_000),
    ("parakeet-ja-0.6b-int8/model.int8.onnx", PARAKEET, "model.int8.onnx", 655_542_604),
]


def missing() -> list[tuple[str, str, str, int]]:
    return [f for f in FILES if not (MODELS / f[0]).exists()]


def _endpoints() -> list[str]:
    out = []
    for e in (os.environ.get("HF_ENDPOINT"), "https://huggingface.co", "https://hf-mirror.com"):
        e = (e or "").strip().rstrip("/")
        if e and e not in out:
            out.append(e)
    return out


def _urls(repo: str, name: str) -> list[str]:
    urls = [f"{e}/{repo}/resolve/main/{name}" for e in _endpoints()]
    if name == "silero_vad.onnx":
        urls.insert(1, VAD_GITHUB)
    return urls


def _clients() -> list[httpx.Client]:
    t = httpx.Timeout(20.0, connect=8.0)
    hdr = {"User-Agent": "wisland"}
    out = []
    px = system_proxy()
    if px:
        out.append(httpx.Client(proxy=px, timeout=t, follow_redirects=True, headers=hdr, trust_env=False))
    out.append(httpx.Client(timeout=t, follow_redirects=True, headers=hdr, trust_env=False))
    return out


class ModelDownloader(QtCore.QObject):
    progress = QtCore.Signal(float, str)     # (0..1, 说明文字)
    finished = QtCore.Signal(bool, str)      # (成功, 失败原因)

    def __init__(self, parent=None):
        super().__init__(parent)
        self._thread: threading.Thread | None = None
        self._cancel = threading.Event()

    @property
    def running(self) -> bool:
        return bool(self._thread and self._thread.is_alive())

    def start(self):
        if self.running:
            return
        self._cancel.clear()
        self._thread = threading.Thread(target=self._run, name="wisland-models", daemon=True)
        self._thread.start()

    def cancel(self):
        self._cancel.set()

    # ---------------- 后台线程 ----------------
    def _run(self):
        todo = missing()
        total = sum(f[3] for f in todo) or 1
        done = 0
        clients = _clients()
        try:
            for rel, repo, name, size in todo:
                dest = MODELS / rel
                dest.parent.mkdir(parents=True, exist_ok=True)
                err = ""
                for url in _urls(repo, name):
                    for c in clients:
                        try:
                            self._get(c, url, dest, lambda n, d=done: self._report(d + n, total))
                            err = ""
                            break
                        except _Cancelled:
                            self.finished.emit(False, "已取消")
                            return
                        except Exception as e:  # noqa: BLE001
                            err = f"{type(e).__name__}: {e}"[:160]
                    if dest.exists():
                        break
                if not dest.exists():
                    self.finished.emit(False, "模型下载失败（" + (err or "网络错误") + "），检查网络或代理后再点一次会接着下载")
                    return
                done += size
            self.finished.emit(True, "")
        finally:
            for c in clients:
                c.close()

    def _report(self, n: int, total: int):
        f = min(1.0, n / total)
        self.progress.emit(f, f"正在下载识别模型 {f * 100:.0f}%（{n / 1e6:.0f} / {total / 1e6:.0f} MB）")

    def _get(self, c: httpx.Client, url: str, dest: Path, on_bytes):
        part = dest.with_name(dest.name + ".part")
        have = part.stat().st_size if part.exists() else 0
        headers = {"Range": f"bytes={have}-"} if have else {}
        with c.stream("GET", url, headers=headers) as r:
            if r.status_code == 416:          # 已经下完
                part.replace(dest)
                return
            r.raise_for_status()
            if have and r.status_code != 206:  # 服务器不支持续传：从头下
                have = 0
            with open(part, "ab" if have else "wb") as f:
                n = have
                on_bytes(n)
                for chunk in r.iter_bytes(1 << 20):
                    if self._cancel.is_set():
                        raise _Cancelled()
                    f.write(chunk)
                    n += len(chunk)
                    on_bytes(n)
        part.replace(dest)


class _Cancelled(Exception):
    pass
