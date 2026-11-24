"""Provider-reported usage. Only aggregate DeepSeek counts are stored locally."""
from __future__ import annotations

import json
import os
import shutil
import sqlite3
import subprocess
import threading
from contextlib import closing
from datetime import datetime
from pathlib import Path

import httpx
import psutil

from wisland.core.config import DATA_DIR

DATABASE = DATA_DIR / "usage.sqlite3"
DEEPSEEK = "https://api.deepseek.com"


def _official(base):
    return base.rstrip("/") in (DEEPSEEK, DEEPSEEK + "/v1")


def record_usage(base, usage, database=DATABASE):
    if not _official(base) or not isinstance(usage, dict):
        return
    counts = (usage.get("prompt_tokens"), usage.get("completion_tokens"))
    if not all(type(value) is int and value >= 0 for value in counts):
        return
    try:
        with closing(sqlite3.connect(database)) as conn, conn:
            conn.execute("CREATE TABLE IF NOT EXISTS totals (id INTEGER PRIMARY KEY, input INTEGER, output INTEGER)")
            conn.execute("""INSERT INTO totals VALUES (1, ?, ?) ON CONFLICT(id)
                            DO UPDATE SET input=input+excluded.input, output=output+excluded.output""", counts)
    except sqlite3.Error:
        # Translation should still finish if the data directory is read-only.
        import logging
        logging.getLogger(__name__).warning("无法保存本次翻译的 token 用量")


def token_totals(database=DATABASE):
    if not database.exists():
        return 0, 0
    with closing(sqlite3.connect(database)) as conn:
        if not conn.execute("SELECT 1 FROM sqlite_master WHERE name='totals'").fetchone():
            return 0, 0
        return conn.execute("SELECT input, output FROM totals WHERE id=1").fetchone() or (0, 0)


def balance_text(key, base):
    if not _official(base):
        return "自定义服务地址，请到服务商后台查看余额"
    if not key:
        return "请先在 config.json 的 translate.api_key 填写 DeepSeek 密钥"
    response = httpx.get(DEEPSEEK + "/user/balance", headers={"Authorization": "Bearer " + key},
                         timeout=10, follow_redirects=False)
    response.raise_for_status()
    data = response.json()
    amounts = [f"{item['total_balance']} {item['currency']}" for item in data.get("balance_infos", [])
               if item.get("total_balance") is not None and item.get("currency")]
    return "账户余额  " + " · ".join(amounts) if amounts else "接口未返回账户余额"


def latest_session_tokens(sessions=None):
    if sessions is None:
        sessions = Path(os.getenv("CODEX_HOME") or Path.home() / ".codex") / "sessions"
    newest = max(sessions.rglob("rollout-*.jsonl"), key=lambda p: p.stat().st_mtime_ns, default=None)
    if newest is None:
        return None
    # Rollouts can be large. Read backwards and stop at the latest cumulative snapshot.
    with newest.open("rb") as stream:
        end = stream.seek(0, os.SEEK_END)
        tail = b""
        while end:
            start = max(0, end - 65536)
            stream.seek(start)
            lines = (stream.read(end - start) + tail).split(b"\n")
            tail = lines.pop(0) if start else b""
            end = start
            for line in reversed(lines):
                if b'"token_count"' not in line:
                    continue
                try:
                    event = json.loads(line)
                except ValueError:
                    continue
                payload = event.get("payload") or {}
                if event.get("type") == "event_msg" and payload.get("type") == "token_count":
                    count = ((payload.get("info") or {}).get("total_token_usage") or {}).get("total_tokens")
                    if isinstance(count, int):
                        return count
    return None


def _reset(timestamp):
    return datetime.fromtimestamp(timestamp).strftime(" · %m-%d %H:%M 重置") if timestamp else ""


def format_limits(result):
    buckets = result.get("rateLimitsByLimitId") or {"codex": result.get("rateLimits") or {}}
    rows = []
    if result.get("ordinaryUsageAllowed") is False:
        rows.append("当前账户的套餐用量受限")
    for name, limits in buckets.items():
        if not limits:
            continue
        prefix = limits.get("limitName") or name
        plan = limits.get("planType")
        rows.append(f"{prefix} · {plan}" if plan else prefix)
        details = []
        for slot, fallback in (("primary", "主窗口"), ("secondary", "次窗口")):
            window = limits.get(slot) or {}
            used = window.get("usedPercent")
            if used is None:
                continue
            minutes = window.get("windowDurationMins")
            label = fallback
            if minutes:
                label = (f"{minutes // 1440} 天" if minutes % 1440 == 0 else
                         f"{minutes // 60} 小时" if minutes % 60 == 0 else f"{minutes} 分钟")
            details.append(f"{label}额度剩余 {max(0, 100 - used):g}%" + _reset(window.get("resetsAt")))
        spend = limits.get("individualLimit") or {}
        if spend.get("remainingPercent") is not None:
            details.append(f"个人额度剩余 {spend['remainingPercent']:g}%" + _reset(spend.get("resetsAt")))
        credits = limits.get("credits") or {}
        if credits.get("unlimited"):
            details.append("点数不限")
        elif credits.get("balance") is not None:
            details.append(f"剩余点数 {credits['balance']}")
        rows.extend(details or ["账户未返回剩余额度"])
    return "\n".join(rows) or "账户未返回剩余额度"


def _codex_executable():
    exe = shutil.which("codex")
    if exe or os.name != "nt":
        return exe
    powershell = Path(os.environ["SystemRoot"]) / "System32/WindowsPowerShell/v1.0/powershell.exe"
    query = ("[Console]::OutputEncoding = [Text.Encoding]::UTF8; "
             "(Get-AppxPackage -Name OpenAI.Codex | Select-Object -First 1).InstallLocation")
    try:
        result = subprocess.run([str(powershell), "-NoProfile", "-NonInteractive", "-Command", query],
                                capture_output=True, encoding="utf-8-sig", errors="replace", timeout=8,
                                creationflags=subprocess.CREATE_NO_WINDOW)
    except (OSError, subprocess.TimeoutExpired):
        return None
    location = result.stdout.strip()
    if result.returncode == 0 and location:
        binary = Path(location) / "app/resources/codex.exe"
        if binary.is_file():
            return str(binary)
    return None


def codex_limits():
    exe = _codex_executable()
    if not exe:
        raise RuntimeError("找不到 Codex CLI，请安装并登录后刷新")
    with subprocess.Popen([exe, "app-server"], stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                          stderr=subprocess.DEVNULL, encoding="utf-8",
                          creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0)) as proc:
        def stop():
            if proc.poll() is not None:
                return
            # npm 的 .cmd 会启动 Node 和原生 CLI，两者都可能继承 stdout 管道。
            if os.name == "nt" and Path(exe).suffix.lower() in (".cmd", ".bat"):
                try:
                    children = psutil.Process(proc.pid).children(recursive=True)
                except psutil.NoSuchProcess:
                    children = []
                for child in reversed(children):
                    try:
                        child.kill()
                    except psutil.NoSuchProcess:
                        pass
            proc.kill()

        timeout = threading.Timer(20, stop)
        timeout.start()
        def send(message):
            proc.stdin.write(json.dumps(message) + "\n")
            proc.stdin.flush()
        try:
            send({"id": 1, "method": "initialize", "params": {"clientInfo": {"name": "wisland", "version": "1"}}})
            for line in proc.stdout:
                msg = json.loads(line)
                if msg.get("id") not in (1, 2):
                    continue
                if "error" in msg:
                    raise RuntimeError("额度查询失败，请检查 Codex 登录状态和网络")
                if msg["id"] == 2:
                    return format_limits(msg["result"])
                send({"method": "initialized"})
                send({"id": 2, "method": "account/rateLimits/read", "params": {}})
            raise RuntimeError("Codex 查询超时或进程已退出")
        finally:
            timeout.cancel()
            timeout.join()
            stop()
