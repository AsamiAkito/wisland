# -*- coding: utf-8 -*-
"""翻译：DeepSeek API（OpenAI 兼容，流式输出）。"""
from __future__ import annotations

import json
import time
from typing import Callable, Optional

import httpx

from .http_util import make_client

OnDelta = Optional[Callable[[str], None]]

DEFAULT_BASE_URL = "https://api.deepseek.com"
MODELS = {"deepseek-flash": "Flash", "deepseek-v4-pro": "Pro"}
# 高峰价，美元 / 百万 token：(缓存命中输入, 未命中输入, 输出)。非高峰为半价。
PRICES = {"deepseek-flash": (0.006, 0.30, 1.20), "deepseek-v4-pro": (0.044, 1.32, 3.96)}


def usage_cost(model: str, usage: dict) -> float:
    """按高峰价估算一次请求的费用（美元）。"""
    hit, miss, out = PRICES.get(model, PRICES["deepseek-flash"])
    h = int(usage.get("prompt_cache_hit_tokens", 0) or 0)
    m = int(usage.get("prompt_cache_miss_tokens", 0) or 0)
    if not h and not m:
        m = int(usage.get("prompt_tokens", 0) or 0)
    o = int(usage.get("completion_tokens", 0) or 0)
    return (h * hit + m * miss + o * out) / 1e6


class TranslationCancelled(Exception):
    """过时的草稿被取消。"""


class TranslationDeadlineExceeded(TimeoutError):
    pass


class TranslationStream:
    """协作取消 + 总时限。"""
    def __init__(self, callback, cancelled, seconds=45.0, read_timeout=15.0):
        self.callback = callback
        self.cancelled = cancelled
        self.deadline = time.monotonic() + seconds
        self.read_timeout = read_timeout

    def checkpoint(self):
        if self.cancelled():
            raise TranslationCancelled()
        if time.monotonic() >= self.deadline:
            raise TranslationDeadlineExceeded("翻译超时")

    def __call__(self, piece):
        self.checkpoint()
        self.callback(piece)


def _checkpoint(callback):
    if hasattr(callback, "checkpoint"):
        callback.checkpoint()


def _timeout(callback, default):
    read = min(default, float(getattr(callback, "read_timeout", default)))
    return httpx.Timeout(default, connect=min(8.0, default), read=read)


# 固定的系统提示放在最前面，DeepSeek 会命中前缀缓存（输入价格约为原价 2%）
SYSTEM_PROMPT = (
    "你是日本动画的日译中字幕翻译。把用户给出的一句日语台词译成简体中文字幕。\n"
    "要求：\n"
    "1. 只输出译文，不加解释、注音、原文、引号或说话人。\n"
    "2. 口语化、简洁，符合中文字幕习惯；保留语气、情绪、时态、否定和疑问。\n"
    "3. 保留角色口癖与人设语气（如「〜にゃ」→喵、「〜ですわ」大小姐腔、「〜のじゃ」老派口吻、「〜っス」后辈腔），用自然的中文表达，不要抹平。\n"
    "4. 名台词、作品专有名词、招式名使用中文圈通行译法（如「全集中・水の呼吸」→全集中·水之呼吸，「知らない天井だ」→陌生的天花板）。\n"
    "5. 网络用语与俚语按语境意译：草→笑死，エモい→戳心/好有感觉，推し→本命/推，尊い→太神了，ワンチャン→有戏，陰キャ→阴角，"
    "やばい按褒贬选「绝了/不妙」。\n"
    "6. 谐音梗、双关尽量用中文谐音或保留笑点，实在不行就意译。\n"
    "7. 原文省略主语时不擅自补出，句子没说完时保留未完成的语气。\n"
    "8. 上文仅用于理解指代和语气，不要翻译上文。"
)


def build_messages(text: str, context: list[str]) -> list[dict]:
    parts = []
    ctx = [c for c in context if c]
    if ctx:
        parts.append("【上文】\n" + "\n".join(ctx[-3:]))
    parts.append("【台词】\n" + text)
    return [{"role": "system", "content": SYSTEM_PROMPT},
            {"role": "user", "content": "\n\n".join(parts)}]


def friendly_error(e: Exception) -> str:
    if isinstance(e, httpx.HTTPStatusError):
        code = e.response.status_code
        hints = {401: "API Key 无效", 402: "余额不足", 403: "无权限", 404: "模型名不对",
                 422: "请求参数错误", 429: "请求过于频繁", 500: "服务端出错", 503: "服务繁忙"}
        return hints.get(code, f"HTTP {code}")
    if isinstance(e, httpx.TimeoutException):
        return "请求超时"
    if isinstance(e, httpx.ConnectError):
        return "无法连接 DeepSeek"
    return str(e)[:160]


_CLIENTS: dict[str, httpx.Client] = {}
_CLIENTS_LOCK = __import__("threading").Lock()


def _client(base: str) -> httpx.Client:
    """复用长连接：省掉每句一次的 TLS 握手。"""
    with _CLIENTS_LOCK:
        c = _CLIENTS.get(base)
        if c is None or c.is_closed:
            c = make_client(base, timeout=30.0,
                            limits=httpx.Limits(max_connections=8, max_keepalive_connections=8,
                                                keepalive_expiry=120))
            _CLIENTS[base] = c
        return c


class Translator:
    def __init__(self, config):
        self.config = config
        self.last_provider = "deepseek"
        self.last_error = ""
        self.last_usage: dict = {}

    @property
    def model(self) -> str:
        return str(self.config.get("translate.model", "deepseek-flash") or "deepseek-flash")

    def translate(self, text: str, context: list[str], on_delta: OnDelta = None, **_ignored) -> str:
        text = (text or "").strip()
        if not text:
            return ""
        key = str(self.config.get("translate.api_key", "") or "").strip()
        if not key:
            raise RuntimeError("未设置 DeepSeek API Key")
        base = str(self.config.get("translate.base_url", "") or DEFAULT_BASE_URL).rstrip("/")
        payload = {
            "model": self.model,
            "messages": build_messages(text, context),
            "temperature": 1.0,
            "thinking": {"type": "disabled"},   # 字幕翻译不需要推理：更快、更省
            "max_tokens": 200,
            "stream": True,
            "stream_options": {"include_usage": True},
        }
        headers = {"Authorization": f"Bearer {key}"}
        parts: list[str] = []
        try:
            client = _client(base)
            with client.stream("POST", base + "/chat/completions", json=payload, headers=headers,
                               timeout=_timeout(on_delta, 30)) as r:
                if r.status_code >= 400:
                    r.read()
                    r.raise_for_status()
                for line in r.iter_lines():
                    _checkpoint(on_delta)
                    if not line.startswith("data:"):
                        continue
                    data = line[5:].strip()
                    if data == "[DONE]":
                        break
                    obj = json.loads(data)
                    if obj.get("usage"):
                        self.last_usage = obj["usage"]
                    choices = obj.get("choices") or []
                    piece = str((choices[0].get("delta") or {}).get("content") or "") if choices else ""
                    if piece:
                        parts.append(piece)
                        if on_delta:
                            on_delta(piece)
        except (TranslationCancelled, TranslationDeadlineExceeded):
            raise
        except Exception as e:  # noqa: BLE001
            self.last_error = friendly_error(e)
            raise RuntimeError(self.last_error) from e
        out = "".join(parts).strip()
        if not out:
            raise RuntimeError("DeepSeek 未返回译文")
        self.last_error = ""
        return out
