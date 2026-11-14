# -*- coding: utf-8 -*-
"""日译中：DeepSeek（OpenAI 兼容）流式接口。参考 译声 jpsub/core/translate.py。

信号
  delta(int, str)     (请求 id, 新增片段)
  finished(int, str)  (请求 id, 完整译文)
  failed(int, str)    (请求 id, 友好错误信息)
新请求会让上一个在途请求作废（流读取循环中协作检查，之后不再发出它的信号）。
"""
from __future__ import annotations

import json
import os
import subprocess
import threading
import time

import httpx
from PySide6 import QtCore

from wisland.core.bg import launch, run_bg, ui
from wisland.core.config import DATA_DIR, cfg, expand

HISTORY_PATH = DATA_DIR / "translate_history.json"
HISTORY_MAX = 30
DEFAULT_BASE_URL = "https://api.deepseek.com"
TOTAL_DEADLINE_S = 60.0

SYSTEM_PROMPT = (
    "你是专业的日译中翻译。把用户给出的文本翻译成简体中文。\n"
    "要求：\n"
    "1. 只输出译文，不加解释、原文、引号或任何前后缀。\n"
    "2. 译文自然、简洁，符合中文表达习惯；保留原文的语气、情绪、敬语层次、时态、否定和疑问。\n"
    "3. 保留原文的段落与换行结构；专有名词、作品名、人名使用中文圈通行译法，没有通行译法时音译或保留原文。\n"
    "4. 网络用语与俚语按语境意译（如 草→笑死，エモい→戳心，推し→本命，やばい 按褒贬译为「绝了/不妙」）。\n"
    "5. 原文省略主语时不擅自补出；句子没说完时保留未完成的语气。\n"
    "6. 如果输入不是日语，也照样翻译成简体中文。\n"
    "7. 如果输入只是一个日语单词或短词组：第一行给出中文释义；第二行写「读音：假名」并附一句简短的补充释义（词性或用法）。"
)


class _Cancelled(Exception):
    pass


def friendly_error(e: Exception) -> str:
    if isinstance(e, httpx.HTTPStatusError):
        code = e.response.status_code
        hints = {400: "请求格式错误", 401: "API Key 无效", 402: "DeepSeek 余额不足", 403: "无权限",
                 404: "模型名不对", 422: "请求参数错误", 429: "请求过于频繁", 500: "服务端出错", 503: "服务繁忙"}
        return hints.get(code, f"HTTP {code}")
    if isinstance(e, httpx.TimeoutException):
        return "请求超时"
    if isinstance(e, httpx.ConnectError):
        return "无法连接 DeepSeek"
    if isinstance(e, TimeoutError):
        return "翻译超时"
    return str(e)[:160] or e.__class__.__name__


class TranslateService(QtCore.QObject):
    delta = QtCore.Signal(int, str)
    finished = QtCore.Signal(int, str)
    failed = QtCore.Signal(int, str)

    def __init__(self, parent=None):
        super().__init__(parent)
        ui(lambda: None)  # 确保 bg 回调 invoker 在主线程创建
        self._client: httpx.Client | None = None
        self._client_lock = threading.Lock()
        self._seq = 0
        self._current = 0
        self._imported: dict | None = None
        self.history: list[dict] = self._load_history()

    def start(self):
        pass

    def stop(self):
        self._current = -1  # 作废在途请求
        with self._client_lock:
            if self._client is not None:
                try:
                    self._client.close()
                except Exception:  # noqa: BLE001
                    pass
                self._client = None

    # ---------- settings ----------
    def _settings(self) -> tuple[str, str, str]:
        key = str(cfg.get("translate.api_key") or "").strip()
        base = str(cfg.get("translate.base_url") or "").strip()
        model = str(cfg.get("translate.model") or "").strip()
        if not key:
            imp = self._import_settings()
            key = imp.get("api_key", "")
            base = imp.get("base_url") or base
            model = imp.get("model") or model
        return key, (base or DEFAULT_BASE_URL).rstrip("/"), model or "deepseek-flash"

    def _import_settings(self) -> dict:
        """从 译声 的 config.json 读取 translate.*（仅内存，不写回我们的配置）。"""
        if self._imported is not None:
            return self._imported
        out: dict = {}
        path = expand(cfg.get("translate.import_from") or "")
        if path and os.path.exists(path):
            try:
                with open(path, encoding="utf-8") as f:
                    t = (json.load(f) or {}).get("translate") or {}
                out = {"api_key": str(t.get("api_key") or "").strip(),
                       "base_url": str(t.get("base_url") or "").strip(),
                       "model": str(t.get("model") or "").strip()}
            except Exception:  # noqa: BLE001
                out = {}
        if out.get("api_key"):
            self._imported = out  # 找到 key 才缓存；没找到时下次再读（用户可能稍后配置）
        return out

    def has_key(self) -> bool:
        return bool(self._settings()[0])

    def _http(self) -> httpx.Client:
        with self._client_lock:
            if self._client is None or self._client.is_closed:
                self._client = httpx.Client(
                    timeout=httpx.Timeout(30.0, connect=8.0, read=20.0), follow_redirects=True,
                    limits=httpx.Limits(max_connections=4, max_keepalive_connections=4, keepalive_expiry=120))
            return self._client

    # ---------- translate ----------
    def translate(self, text: str) -> int:
        self._seq += 1
        rid = self._seq
        self._current = rid
        text = (text or "").strip()
        if not text:
            QtCore.QTimer.singleShot(0, lambda: self.failed.emit(rid, "没有要翻译的文字"))
            return rid
        run_bg(self._work, rid, text)
        return rid

    def cancel(self):
        self._current = 0

    def _alive(self, rid: int) -> bool:
        return self._current == rid

    def _work(self, rid: int, text: str):
        if not self._alive(rid):
            return  # 还没开始就已被更新的请求取代
        try:
            key, base, model = self._settings()
            if not key:
                raise RuntimeError("未设置 DeepSeek API Key（右键 → 设置… → 翻译与单词本）")
            payload = {
                "model": model,
                "messages": [{"role": "system", "content": SYSTEM_PROMPT},
                             {"role": "user", "content": text}],
                "temperature": 1.0,
                "thinking": {"type": "disabled"},
                "max_tokens": max(256, min(4000, len(text) * 3 + 200)),
                "stream": True,
            }
            deadline = time.monotonic() + TOTAL_DEADLINE_S
            parts: list[str] = []
            with self._http().stream("POST", base + "/chat/completions", json=payload,
                                     headers={"Authorization": f"Bearer {key}"}) as r:
                if r.status_code >= 400:
                    r.read()
                    r.raise_for_status()
                for line in r.iter_lines():
                    if not self._alive(rid):
                        raise _Cancelled()
                    if time.monotonic() > deadline:
                        raise TimeoutError()
                    if not line.startswith("data:"):
                        continue
                    data = line[5:].strip()
                    if data == "[DONE]":
                        break
                    try:
                        obj = json.loads(data)
                    except ValueError:
                        continue
                    choices = obj.get("choices") or []
                    piece = str((choices[0].get("delta") or {}).get("content") or "") if choices else ""
                    if piece:
                        parts.append(piece)
                        self.delta.emit(rid, piece)
            out = "".join(parts).strip()
            if not self._alive(rid):
                return
            if not out:
                raise RuntimeError("DeepSeek 未返回译文")
            self.finished.emit(rid, out)
            ui(lambda: self._add_history(text, out))
        except _Cancelled:
            return
        except Exception as e:  # noqa: BLE001
            if self._alive(rid):
                self.failed.emit(rid, friendly_error(e))

    # ---------- history ----------
    def _load_history(self) -> list[dict]:
        try:
            h = json.loads(HISTORY_PATH.read_text(encoding="utf-8"))
            return [x for x in h if isinstance(x, dict)][:HISTORY_MAX]
        except Exception:  # noqa: BLE001
            return []

    def _add_history(self, src: str, dst: str):
        self.history = [h for h in self.history if h.get("src") != src]
        self.history.insert(0, {"src": src[:2000], "dst": dst[:4000], "time": time.time()})
        del self.history[HISTORY_MAX:]
        try:
            tmp = HISTORY_PATH.with_suffix(".tmp")
            tmp.write_text(json.dumps(self.history, ensure_ascii=False, indent=1), encoding="utf-8")
            os.replace(tmp, HISTORY_PATH)
        except Exception:  # noqa: BLE001
            pass

    def clear_history(self):
        self.history = []
        try:
            HISTORY_PATH.unlink(missing_ok=True)
        except Exception:  # noqa: BLE001
            pass

    # ---------- live subtitle app ----------
    def launch_live(self) -> bool:
        """启动 译声（实时字幕）并显示主窗口。

        译声默认启动到托盘（没有可见窗口），必须带 --show；已在运行时它的单实例逻辑会把窗口带到前台。
        """
        folder = expand(cfg.get("translate.live_app") or "")
        if not folder or not os.path.isdir(folder):
            return False
        pyw = os.path.join(folder, ".venv", "Scripts", "pythonw.exe")
        if os.path.exists(pyw):
            try:
                subprocess.Popen([pyw, "-X", "utf8", "-m", "jpsub.main", "--show"], cwd=folder,
                                 creationflags=0x00000008 | 0x00000200)
                return True
            except OSError:
                pass
        for name in os.listdir(folder):
            if name.lower().endswith(".exe") and os.path.isfile(os.path.join(folder, name)):
                return launch(os.path.join(folder, name), ["--show"])
        return False
