# -*- coding: utf-8 -*-
"""本地程序路径：每次启动给「消息与邮件 → 打开应用」的空路径填入默认 exe。

只解析开始菜单快捷方式和常见安装位置；只填空白项，不覆盖用户已经填好的路径。
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
    "dsh": ("deepseek harness.exe",),
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
    "dsh": (
        "%LocalAppData%/Programs/DeepSeek Harness/DeepSeek Harness.exe",
        "%LocalAppData%/Programs/deepseek-harness/DeepSeek Harness.exe",
        "%ProgramFiles%/DeepSeek Harness/DeepSeek Harness.exe",
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
    try:
        comtypes.CoInitialize()
    except Exception:  # noqa: BLE001 - 线程已初始化 COM 时不要影响启动
        return []
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
    except Exception:  # noqa: BLE001 - 读开始菜单失败时退回常见安装位置
        return []
    finally:
        try:
            comtypes.CoUninitialize()
        except Exception:  # noqa: BLE001
            pass
    return targets


def detect_open_apps(kinds=None) -> dict[str, str]:
    """返回能确认存在的本地程序路径；找不到的不放。kinds 为空时检测全部。"""
    if kinds is None:
        kinds = tuple(BASENAMES)
    elif isinstance(kinds, str):
        kinds = (kinds,)
    else:
        kinds = tuple(kinds)
    found: dict[str, str] = {}
    missing: dict[str, tuple[str, ...]] = {}
    for kind in kinds:
        names = BASENAMES.get(kind)
        if not names:
            continue
        for raw in KNOWN.get(kind, ()):
            path = Path(expand(raw))
            if path.is_file():
                found[kind] = path.as_posix()
                break
        else:
            missing[kind] = names
    if not missing:
        return found
    # 只有常见位置都没找到时才读开始菜单，避免每次启动都遍历快捷方式。
    menu = _start_menu_targets()
    for kind, names in missing.items():
        for target in menu:
            if Path(target).name.casefold() in names:
                found[kind] = Path(target).as_posix()
                break
    return found


_SEED_ATTEMPTED = False


def seed_open_apps() -> int:
    """每次 wisland 启动时，为 notify.open 中仍为空的项检测一次。

    只填空白项，不覆盖用户填写的路径；同一进程内只执行一次，避免重复读取。
    这样关闭应用再打开、重启电脑后自启时都会重新检测。
    """
    global _SEED_ATTEMPTED
    if _SEED_ATTEMPTED:
        return 0
    _SEED_ATTEMPTED = True
    current = cfg.get("notify.open")
    if not isinstance(current, dict):
        current = {}
    empty = [kind for kind in BASENAMES if not str(current.get(kind) or "").strip()]
    # 所有路径都已填好时不需要读取任何本地程序。
    if not empty:
        return 0
    try:
        found = detect_open_apps(empty)
    except Exception as e:  # noqa: BLE001
        print("[notify] 读取本地程序路径失败：", e)
        return 0
    added = 0
    for kind, path in found.items():
        if not str(current.get(kind) or "").strip():
            current[kind] = path
            added += 1
    if added:
        try:
            cfg.set("notify.open", current)
        except OSError as e:
            print("[notify] 保存本地程序路径失败：", e)
            return 0
        print(f"[notify] 已自动配置 {added} 个打开应用路径")
    return added
