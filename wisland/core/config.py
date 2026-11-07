# -*- coding: utf-8 -*-
"""配置：项目根目录下的 config.json，缺省值见 DEFAULTS。点号路径读写：cfg.get("weather.city")。"""
from __future__ import annotations

import copy
import json
import os
import threading
from pathlib import Path

from PySide6 import QtCore

ROOT = Path(__file__).resolve().parents[2]
CONFIG_PATH = ROOT / "config.json"
DATA_DIR = ROOT / "data"
DATA_DIR.mkdir(exist_ok=True)

DEFAULTS: dict = {
    "island": {
        "edge": "top",              # top / bottom / left / right
        "pos": 0.5,                 # 沿边位置 0..1
        "screen": 0,
        "theme": "auto",            # auto / light / dark
        "hide_fullscreen": False,      # True = 前台全屏（游戏/视频）时隐藏灵动岛
        "hover_expand_ms": 120,
        "collapse_delay_ms": 320,
        "exclude_from_capture": False,  # False = 灵动岛出现在截图 / 录屏里（取背景时只临时排除自己）
        "blur_fps": 8,
        "scale": 1.0,
        "default_tab": "home",
        "expand_mode": "hover",     # hover = 悬停展开 / always = 常驻展开 / never = 保持收起
        "pinned": False,            # 旧版"保持展开"（兼容）
        "compact_view": "clock",    # 收起态视图：clock / pet / music（滚轮切换）
        "auto_cycle_s": 30,         # 时间天气与小形象自动轮换的间隔（秒），0 = 不轮换
        "passthrough": False,       # 可穿透：鼠标穿过灵动岛，只响应右键（不影响展开 / 收起）
    },
    "hotkeys": {
        "toggle": "Ctrl+Alt+I",
        "translate": "Ctrl+Alt+J",
        "clipboard": "Ctrl+Alt+V",
    },
    "media": {
        "auto_show": True,           # 开始播放时收起态自动切到音乐（暂停 8 秒后切回）
        "lyrics": True,              # 收起态显示歌词（网易云公开歌词接口）
        "netease_log": True,         # 读取网易云本地日志获得播放状态与进度（网易云默认不接入系统媒体控件）
        "priority": ["cloudmusic", "bilibili", "chrome", "zen", "msedge", "firefox"],
    },
    "notify": {
        # wpndatabase 中的 NotificationHandler.PrimaryId 前缀 -> 显示名 / 类型
        "toast_apps": {
            "QQ": {"name": "QQ", "kind": "qq"},
            "ThunderbirdToast": {"name": "邮件", "kind": "mail"},
            "Weixin": {"name": "微信", "kind": "wechat"},
        },
        # 任务栏闪烁（FlashWindow）的进程 -> 显示名 / 类型
        "flash_processes": {
            "weixin.exe": {"name": "微信", "kind": "wechat"},
            "wechat.exe": {"name": "微信", "kind": "wechat"},
            "qq.exe": {"name": "QQ", "kind": "qq"},
            "thunderbird.exe": {"name": "邮件", "kind": "mail"},
        },
        "open": {
            "qq": "",          # 例：C:/Program Files/Tencent/QQNT/QQ.exe
            "wechat": "",      # 例：C:/Program Files/Tencent/Weixin/Weixin.exe
            "mail": "",        # 例：C:/Program Files/Mozilla Thunderbird/thunderbird.exe
        },
    },
    "stocks": {
        # 自选（腾讯财经代码：sh/sz/bj + 6 位，hk + 5 位，us + 代码）；也可在股票页点 + 搜索添加、右键删除
        "watchlist": ["sh000001", "sz399001", "sz399006", "hk00700", "usAAPL", "usNVDA", "usQQQ"],
        "interval": 5,
        "pct_alert": 3.0,            # 涨跌幅绝对值超过它时提醒（每只每天每个方向一次）
        "alerts": {},                # {"sh688256": {"above": 1500, "below": 1200}}
    },
    "weather": {
        "api_key": "",         # 留空 = 使用 Open-Meteo（免费、无需 Key）；填 OpenWeather Key 则改用 OpenWeather
        "city": "上海",
        "lat": None,
        "lon": None,
        "units": "metric",
        "lang": "zh_cn",
        "interval_min": 15,
        "cities": [],          # [{"name": "上海", "lat": 31.23, "lon": 121.47}]，第一个显示在收起态
    },
    "brightness": {"twinkle": ""},   # 留空 = 直接用 DDC/CI / WMI（便携版 Twinkle 每次调用都会弹"安装中"）
    "clash": {
        "config_dir": "%APPDATA%/io.github.clash-verge-rev.clash-verge-rev",
        "proxy_port": 7897,
        "app": "",                   # clash-verge.exe 路径（「打开 Clash Verge」按钮用）
    },
    "translate": {
        "api_key": "",
        "base_url": "https://api.deepseek.com",
        "model": "deepseek-flash",
        "import_from": "",           # 可选：从另一个 JSON 配置的 translate.api_key 读取 Key
    },
    "apps": {
        # 托盘里的应用（窗口被隐藏）：发送应用自己的"显示窗口"热键让它恢复（微信默认 Ctrl+Alt+W）
        "restore_hotkey": {
            "weixin.exe": "Ctrl+Alt+W",
            "wechat.exe": "Ctrl+Alt+W",
        },
    },
    "mail": {
        "interval_s": 120,
        # IMAP 账户（首次运行自动从 Thunderbird 读取服务器和用户名）；填入授权码 / 应用专用密码后才会检查
        "imap": [],
    },
    "live": {
        "models_dir": "models",     # parakeet-ja-0.6b-int8/ 与 silero_vad.onnx 所在目录
        "audio": {"source": "loopback", "device": "", "block_ms": 80},
        "vad": {"threshold": 0.4, "min_silence_ms": 350, "min_speech_ms": 200, "max_segment_s": 10},
        "asr": {"partial": True, "draft_mode": "rolling", "draft_interval_ms": 500, "draft_threads": 4,
                "final_engine": "parakeet", "final_threads": 6, "max_segment_s": 10, "context_prompt": False},
        "translate": {"enabled": True, "live": True, "live_min_interval_ms": 900, "live_min_new_chars": 4,
                      "context_lines": 3},
    },
    "learn": {
        "deck": "日语::生词",          # Anki 牌组
        "ankiconnect_url": "",        # 留空 = 本机 127.0.0.1:8765；AnkiConnect Android 填 手机IP:8765
        "auto_sync": False,           # 加词后 / 每 20 秒自动推送到 Anki（Anki 开着时）
        "screenshot": False,          # 加词时截取播放器画面作为卡片配图
        "player_title": "",           # 播放器窗口标题关键字（截图用），如 "Kazumi"
        "source": "",
    },
    "clipboard": {"max": 10},      # 只保留最近 10 条（置顶的不计）
    "launcher": [
        {"name": "文件资源管理器", "path": "C:/Windows/explorer.exe"},
    ],
}


def _merge(base: dict, over: dict) -> dict:
    out = copy.deepcopy(base)
    for k, v in (over or {}).items():
        if isinstance(v, dict) and isinstance(out.get(k), dict) and k not in ("alerts",):
            out[k] = _merge(out[k], v)
        else:
            out[k] = v
    return out


def expand(path: str) -> str:
    return os.path.expandvars(os.path.expanduser(str(path or "")))


class Config(QtCore.QObject):
    changed = QtCore.Signal()

    def __init__(self):
        super().__init__()
        self._lock = threading.RLock()
        self.data: dict = copy.deepcopy(DEFAULTS)
        self.load()

    def load(self):
        user = {}
        if CONFIG_PATH.exists():
            try:
                user = json.loads(CONFIG_PATH.read_text(encoding="utf-8"))
            except Exception as e:  # noqa: BLE001
                print("config.json 解析失败，使用默认值：", e)
        with self._lock:
            self.data = _merge(DEFAULTS, user)
        if not CONFIG_PATH.exists():
            self.save()

    def save(self):
        with self._lock:
            text = json.dumps(self.data, ensure_ascii=False, indent=2)
        tmp = CONFIG_PATH.with_suffix(".tmp")
        tmp.write_text(text, encoding="utf-8")
        os.replace(tmp, CONFIG_PATH)

    def get(self, key: str, default=None):
        with self._lock:
            cur = self.data
            for part in key.split("."):
                if not isinstance(cur, dict) or part not in cur:
                    return default
                cur = cur[part]
            return cur

    def set(self, key: str, value, save: bool = True):
        with self._lock:
            parts = key.split(".")
            cur = self.data
            for part in parts[:-1]:
                cur = cur.setdefault(part, {})
            cur[parts[-1]] = value
        if save:
            self.save()


cfg = Config()
