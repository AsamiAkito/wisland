# -*- coding: utf-8 -*-
"""下载实时字幕所需的模型到 models/（约 630MB）：

- Parakeet 日语识别（sherpa-onnx 格式，int8）
- silero_vad.onnx（静音检测）

用法：.venv\\Scripts\\python.exe scripts\\download_models.py
国内网络可先设置 HF_ENDPOINT=https://hf-mirror.com
"""
from __future__ import annotations

import os
import sys
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
MODELS = ROOT / "models"
PARAKEET_REPO = "csukuangfj/sherpa-onnx-nemo-parakeet-tdt_ctc-0.6b-ja-35000-int8"
VAD_URLS = [
    "https://github.com/k2-fsa/sherpa-onnx/releases/download/asr-models/silero_vad.onnx",
    "https://hf-mirror.com/csukuangfj/sherpa-onnx-vad-models/resolve/main/silero_vad.onnx",
]


def main() -> int:
    MODELS.mkdir(exist_ok=True)
    dest = MODELS / "parakeet-ja-0.6b-int8"
    if not (dest / "model.int8.onnx").exists():
        try:
            from huggingface_hub import snapshot_download
        except ImportError:
            print("请先安装：uv pip install --python .venv/Scripts/python.exe huggingface-hub")
            return 1
        print("下载 Parakeet 日语模型 …")
        snapshot_download(PARAKEET_REPO, local_dir=str(dest), allow_patterns=["*.onnx", "tokens.txt"])
    vad = MODELS / "silero_vad.onnx"
    if not vad.exists():
        for url in VAD_URLS:
            try:
                print("下载 silero_vad.onnx …", url)
                urllib.request.urlretrieve(url, vad)
                break
            except Exception as e:  # noqa: BLE001
                print("  失败：", e)
    ok = (dest / "model.int8.onnx").exists() and vad.exists()
    print("完成" if ok else "未完成，请检查网络后重试")
    return 0 if ok else 1


if __name__ == "__main__":
    os.chdir(ROOT)
    sys.exit(main())
