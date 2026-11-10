# -*- coding: utf-8 -*-
"""本地程序路径：首次启动给「消息与邮件 → 打开应用」填入默认 exe。

只解析开始菜单快捷方式和常见安装位置；用户改过之后由 notify.open_seeded 标记，
不会再次覆盖。
"""
from __future__ import annotations

import os
from pathlib import Path

from ..core.config import cfg, expand

# 目标 exe 名（小写），按这个匹配快捷方式，避免把 QQ音乐 之类的同名程序认成 QQ
BASENAMES = {
    "qq": ("qq.exe", "qqnt.exe"),
    "wechat": ("weixin.exe", "wechat.exe"),
    "feishu": ("feishu.exe", "lark.exe"),
    "dingtalk": ("dingtalk.exe",),
    "mail": ("olk.exe", "outlook.exe", "thunderbird.exe", "hxoutlook.exe"),
}

KNOWN = {
    "qq": (
        "%ProgramFiles%/Tencent/QQNT/QQ.exe",
        "%ProgramFiles(x86)%/Tencent/QQNT/QQ.exe",
        "%ProgramFiles%/Tencent/QQ/Bin/QQ.exe",
        "%ProgramFiles(x86)%/Tencent/QQ/Bin/QQ.exe",
        "%LocalAppData%/Tencent/QQNT/QQ.exe",
    ),
    "wechat": (
        "%ProgramFiles%/Tencent/Weixin/Weixin.exe",
        "%ProgramFiles(x86)%/Tencent/Weixin/Weixin.exe",
        "%ProgramFiles%/Tencent/WeChat/WeChat.exe",
        "%ProgramFiles(x86)%/Tencent/WeChat/WeChat.exe",
        "%LocalAppData%/Tencent/Weixin/Weixin.exe",
    ),
    "feishu": (
        "%ProgramFiles%/ByteDance/Feishu/Feishu.exe",
        "%ProgramFiles(x86)%/ByteDance/Feishu/Feishu.exe",
        "%LocalAppData%/Feishu/Feishu.exe",
        "%LocalAppData%/ByteDance/Feishu/Feishu.exe",
    ),
    "dingtalk": (
        "%ProgramFiles%/DingTalk/DingTalk.exe",
        "%ProgramFiles(x86)%/DingTalk/DingTalk.exe",
        "%LocalAppData%/DingTalk/DingTalk.exe",
    ),
    "mail": (
        "%LocalAppData%/Microsoft/WindowsApps/olk.exe",
        "%ProgramFiles%/Microsoft Office/root/Office16/OUTLOOK.EXE",
        "%ProgramFiles(x86)%/Microsoft Office/root/Office16/OUTLOOK.EXE",
        "%ProgramFiles%/Mozilla Thunderbird/thunderbird.exe",
        "%ProgramFiles(x86)%/Mozilla Thunderbird/thunderbird.exe",
    ),
}


def _start_menu_targets() -> list[str]:
    try:
        import comtypes
        import comtypes.client
    except Exception:  # noqa: BLE001
        return []
    roots = []
    for base in (os.environ.get("APPDATA"), os.environ.get("ProgramData")):
        if base:
            root = Path(base) / "Microsoft" / "Windows" / "Start Menu" / "Programs"
            if root.exists():
                roots.append(root)
    if not roots:
        return []
    targets: list[str] = []
    comtypes.CoInitialize()
    try:
        shell = comtypes.client.CreateObject("WScript.Shell", dynamic=True)
        for root in roots:
            for link in root.rglob("*.lnk"):
                try:
                    sc = shell.CreateShortcut(str(link))
                    target = str(getattr(sc, "TargetPath", "") or "").strip().strip('"')
                except Exception:  # noqa: BLE001
                    continue
                if target.lower().endswith(".exe") and os.path.isfile(target):
                    targets.append(target)
    finally:
        comtypes.CoUninitialize()
    return targets


def detect_open_apps() -> dict[str, str]:
    """返回能确认存在的本地程序路径；找不到的不放。"""
    menu = _start_menu_targets()
    found: dict[str, str] = {}
    for kind, names in BASENAMES.items():
        for raw in KNOWN.get(kind, ()):
            path = Path(expand(raw))
            if path.is_file():
                found[kind] = path.as_posix()
                break
        if kind in found:
            continue
        for target in menu:
            if Path(target).name.casefold() in names:
                found[kind] = Path(target).as_posix()
                break
    return found


def seed_open_apps() -> int:
    """首次启动填充 notify.open 中仍为空的项；用户改过/清空后不再覆盖。"""
    if bool(cfg.get("notify.open_seeded", False)):
        return 0
    try:
        found = detect_open_apps()
    except Exception as e:  # noqa: BLE001
        print("[notify] 读取本地程序路径失败：", e)
        return 0
    current = cfg.get("notify.open")
    if not isinstance(current, dict):
        current = {}
    added = 0
    for kind, path in found.items():
        if not str(current.get(kind) or "").strip():
            current[kind] = path
            added += 1
    cfg.set("notify.open", current, save=False)
    cfg.set("notify.open_seeded", True, save=False)
    try:
        cfg.save()
    except OSError as e:
        print("[notify] 保存本地程序路径失败：", e)
    if added:
        print(f"[notify] 已自动配置 {added} 个打开应用路径")
    return added
