# -*- coding: utf-8 -*-
"""消息来源品牌图标：QQ / 微信 / 邮件 / 飞书 / 钉钉。

SVG 在打包时已经转成 256px 的 PNG，统一放进 wisland/ui/assets。
运行时按需加载并缓存；找不到素材时返回 None，由调用方退回原来的色块 + 字体图标。
"""
from __future__ import annotations

import sys
from pathlib import Path

from PySide6 import QtCore, QtGui

KIND_FILES = {
    "qq": "qq.png",
    "wechat": "wechat.png",
    "mail": "mail.png",
    "feishu": "feishu.png",
    "dingtalk": "dingtalk.png",
}

_CACHE: dict[str, QtGui.QPixmap] = {}


def asset_path(name: str) -> Path | None:
    """源码运行取 wisland/ui/assets，打包后优先取 _MEIPASS 下的同名目录。"""
    p = Path(__file__).resolve().parent / "assets" / name
    if p.exists():
        return p
    meipass = getattr(sys, "_MEIPASS", "")
    if meipass:
        p2 = Path(meipass) / "wisland" / "ui" / "assets" / name
        if p2.exists():
            return p2
    return None


def kind_pixmap(kind: str) -> QtGui.QPixmap | None:
    if kind not in KIND_FILES:
        return None
    pm = _CACHE.get(kind)
    if pm is not None:
        return pm
    path = asset_path(KIND_FILES[kind])
    if path is None:
        return None
    pm = QtGui.QPixmap(str(path))
    if pm.isNull():
        return None
    _CACHE[kind] = pm
    return pm


def paint_icon_chip(p: QtGui.QPainter, rect: QtCore.QRectF, pm: QtGui.QPixmap, alpha: float = 1.0):
    """白色圆角底 + 品牌图标的统一小方块；钉钉自带蓝底，会留一圈白边。"""
    if pm is None or pm.isNull():
        return
    w = rect.width()
    rad = max(3.0, w * 0.27)
    inset = max(1.5, w * 0.09)
    p.save()
    p.setOpacity(alpha)
    p.setRenderHint(QtGui.QPainter.RenderHint.SmoothPixmapTransform, True)
    p.setPen(QtGui.QPen(QtGui.QColor(0, 0, 0, 28), 1))
    p.setBrush(QtGui.QColor(250, 250, 252))
    p.drawRoundedRect(rect, rad, rad)
    inner = rect.adjusted(inset, inset, -inset, -inset)
    clip = QtGui.QPainterPath()
    clip.addRoundedRect(inner, rad * 0.72, rad * 0.72)
    p.setClipPath(clip, QtCore.Qt.ClipOperation.IntersectClip)
    p.drawPixmap(inner, pm, QtCore.QRectF(pm.rect()))
    p.restore()
