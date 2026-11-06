# -*- coding: utf-8 -*-
"""实时字幕所需模型的位置：config.json → live.models_dir（相对路径以项目根目录为准）。"""
from __future__ import annotations

from pathlib import Path

from ..core.config import ROOT, cfg, expand


def models_dir() -> Path:
    p = Path(expand(cfg.get("live.models_dir") or "models"))
    return p if p.is_absolute() else ROOT / p


MODELS = models_dir()
