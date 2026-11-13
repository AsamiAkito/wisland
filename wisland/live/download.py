# -*- coding: utf-8 -*-
"""实时字幕模型下载：断点续传、Hugging Face → hf-mirror 回退、进度 / 取消。

模型文件：
- parakeet-ja-0.6b-int8/model.int8.onnx  （约 625 MB）
- parakeet-ja-0.6b-int8/tokens.txt
- silero_vad.onnx
"""
from __future__ import annotations

import os
import re
import threading
import time
from pathlib import Path

import httpx

from .. import __version__ as APP_VERSION

from .paths import models_dir

MODEL_REPO = "csukuangfj/sherpa-onnx-nemo-parakeet-tdt_ctc-0.6b-ja-35000-int8"
HF_HOST = "https://huggingface.co"
MIRROR_HOST = "https://hf-mirror.com"
VAD_URLS = [
    "https://github.com/k2-fsa/sherpa-onnx/releases/download/asr-models/silero_vad.onnx",
    "https://hf-mirror.com/csukuangfj/sherpa-onnx-vad-models/resolve/main/silero_vad.onnx",
]
UA = f"wisland/{APP_VERSION} (live subtitle model downloader)"
CHUNK = 256 * 1024


class DownloadCancelled(Exception):
    """用户取消；已写入的 .part 保留，下次从这里续传。"""


def _hf_urls(name: str) -> list[str]:
    path = f"{MODEL_REPO}/resolve/main/{name}"
    return [f"{HF_HOST}/{path}", f"{MIRROR_HOST}/{path}"]


def model_specs() -> list[dict]:
    root = models_dir()
    para = root / "parakeet-ja-0.6b-int8"
    return [
        {"dest": para / "model.int8.onnx", "urls": _hf_urls("model.int8.onnx"),
         "label": "日语识别模型"},
        {"dest": para / "tokens.txt", "urls": _hf_urls("tokens.txt"),
         "label": "识别词表"},
        {"dest": root / "silero_vad.onnx", "urls": VAD_URLS,
         "label": "静音检测模型"},
    ]


def models_ready() -> bool:
    try:
        for spec in model_specs():
            path = spec["dest"]
            if not path.is_file() or path.stat().st_size <= 0:
                return False
    except OSError:
        return False
    return True


def _part_path(dest: Path) -> Path:
    return dest.with_name(dest.name + ".part")


def _probe_size(client: httpx.Client, spec: dict, cancel: threading.Event) -> int:
    """尽力取得文件总大小；拿不到就返回 0（进度只显示已下载 MB）。"""
    for url in spec.get("urls") or []:
        if cancel.is_set():
            raise DownloadCancelled()
        try:
            r = client.head(url)
            if r.status_code < 400:
                n = r.headers.get("content-length")
                if n and n.isdigit():
                    return int(n)
        except Exception:  # noqa: BLE001
            pass
        try:
            r = client.get(url, headers={"Range": "bytes=0-0"})
            if r.status_code in (200, 206):
                m = re.search(r"/(\d+)\s*$", r.headers.get("content-range") or "")
                if m:
                    return int(m.group(1))
                n = r.headers.get("content-length")
                if n and n.isdigit():
                    return int(n)
        except Exception:  # noqa: BLE001
            pass
    return 0


def _stream_to_part(client: httpx.Client, url: str, part: Path, cancel: threading.Event,
                    progress) -> None:
    """下载到 .part；有旧内容时用 Range 续传。"""
    for attempt in range(2):
        pos = part.stat().st_size if part.exists() else 0
        headers = {"Range": f"bytes={pos}-"} if pos else {}
        with client.stream("GET", url, headers=headers) as r:
            if r.status_code == 416 and pos:
                part.unlink(missing_ok=True)
                continue
            if pos and r.status_code == 206:
                mode, done = "ab", pos
            else:
                if r.status_code >= 400:
                    r.raise_for_status()
                mode, done = "wb", 0
            total = 0
            m = re.search(r"/(\d+)\s*$", r.headers.get("content-range") or "")
            if m:
                total = int(m.group(1))
            elif r.headers.get("content-length"):
                try:
                    total = done + int(r.headers["content-length"])
                except ValueError:
                    total = 0
            last_t, last_n = 0.0, done
            part.parent.mkdir(parents=True, exist_ok=True)
            with open(part, mode) as f:
                for chunk in r.iter_bytes(CHUNK):
                    if cancel.is_set():
                        raise DownloadCancelled()
                    f.write(chunk)
                    done += len(chunk)
                    now = time.monotonic()
                    if now - last_t >= 0.25 or done - last_n >= 4 * 1024 * 1024:
                        progress(done, total)
                        last_t, last_n = now, done
            progress(done, total)
            if total and done < total:
                raise IOError("下载不完整，请重试")
            return
    raise IOError("服务器不支持断点续传")


def _download_spec(client: httpx.Client, spec: dict, cancel: threading.Event,
                   progress, note) -> int:
    dest = spec["dest"]
    expected = int(spec.get("size") or 0)
    if dest.is_file() and dest.stat().st_size > 0:
        if not expected or dest.stat().st_size == expected:
            return dest.stat().st_size
    dest.parent.mkdir(parents=True, exist_ok=True)
    part = _part_path(dest)
    last = None
    urls = spec.get("urls") or []
    for i, url in enumerate(urls):
        try:
            _stream_to_part(client, url, part, cancel, progress)
            if expected and part.stat().st_size != expected:
                raise IOError("文件大小校验失败")
            os.replace(part, dest)
            return dest.stat().st_size
        except DownloadCancelled:
            raise
        except Exception as e:  # noqa: BLE001
            last = e
            if i + 1 < len(urls):
                note("下载源连接失败，改用备用镜像…")
            continue
    raise last or IOError("下载失败")


def download_models(cancel: threading.Event | None = None, progress=None, note=None) -> None:
    """下载缺失的模型；取消时抛出 DownloadCancelled，保留 .part 供续传。"""
    cancel = cancel or threading.Event()
    progress = progress or (lambda done, total, label: None)
    note = note or (lambda text: None)
    specs = model_specs()
    if models_ready():
        return
    note("正在检查模型文件…")
    timeout = httpx.Timeout(30.0, connect=10.0, read=30.0)
    with httpx.Client(timeout=timeout, follow_redirects=True,
                      headers={"User-Agent": UA}) as client:
        for spec in specs:
            if cancel.is_set():
                raise DownloadCancelled()
            spec["size"] = _probe_size(client, spec, cancel)
        total = sum(int(s.get("size") or 0) for s in specs)
        done_base = 0
        for spec in specs:
            if cancel.is_set():
                raise DownloadCancelled()
            dest = spec["dest"]
            label = spec.get("label") or dest.name
            expected = int(spec.get("size") or 0)
            if dest.is_file() and dest.stat().st_size > 0 and (not expected or dest.stat().st_size == expected):
                done_base += dest.stat().st_size
                progress(done_base, total, label)
                continue
            note(f"正在下载{label}…")

            def on_progress(done: int, _file_total: int, base=done_base, label=label):
                progress(base + done, total or (base + _file_total), label)

            got = _download_spec(client, spec, cancel, on_progress, note)
            done_base += got
            if not total or done_base > total:
                total = done_base
            progress(done_base, total, label)
    if not models_ready():
        raise IOError("模型文件下载完成但校验失败，请重试")
    progress(total, total, "模型已就绪")
