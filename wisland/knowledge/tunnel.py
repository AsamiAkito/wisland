# -*- coding: utf-8 -*-
"""由灵动岛管理无窗口 SSH 隧道；仅密钥认证，不保存服务器密码。"""

from __future__ import annotations

import os
import re
import shutil
import socket
import subprocess
import time
from pathlib import Path


def tunnel_command(config):
    host = str(config.get("ssh_host", ""))
    user = str(config.get("ssh_user", "root"))
    if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9.:-]*", host) or not re.fullmatch(
        r"[A-Za-z_][A-Za-z0-9_.-]*", user
    ):
        raise ValueError("SSH 地址或用户名格式不正确")
    port = int(config.get("ssh_port", 22))
    local = int(config.get("ssh_local_port", 18767))
    if not 1 <= port <= 65535 or not 1 <= local <= 65535:
        raise ValueError("SSH 端口范围不正确")
    key = Path(config.get("ssh_key") or Path.home() / ".ssh/wisland_deploy_ed25519").expanduser()
    if not key.is_file():
        raise ValueError("没有找到部署密钥，请先完成密钥授权")
    executable = shutil.which("ssh")
    if not executable:
        raise ValueError("没有找到系统 OpenSSH 客户端")
    return [
        executable,
        "-NT",
        "-o",
        "BatchMode=yes",
        "-o",
        "IdentitiesOnly=yes",
        "-o",
        "StrictHostKeyChecking=yes",
        "-o",
        "ExitOnForwardFailure=yes",
        "-o",
        "LogLevel=ERROR",
        "-o",
        "ConnectTimeout=8",
        "-o",
        "ServerAliveInterval=30",
        "-o",
        "ServerAliveCountMax=3",
        "-i",
        str(key),
        "-p",
        str(port),
        "-L",
        f"127.0.0.1:{local}:127.0.0.1:8766",
        f"{user}@{host}",
    ]


def port_open(port):
    try:
        with socket.create_connection(("127.0.0.1", port), timeout=0.15):
            return True
    except OSError:
        return False


class SshTunnel:
    def __init__(self):
        self.process = None
        self.state = "未启用"
        self._command = None
        self._retry_at = 0
        self._attempts = 0
        self._auth_failed = False

    def stop(self):
        if self.process:
            if self.process.poll() is None:
                self.process.terminate()
                try:
                    self.process.wait(timeout=2)
                except subprocess.TimeoutExpired:
                    self.process.kill()
                    self.process.wait(timeout=2)
            if self.process.stderr:
                self.process.stderr.close()
            self.process = None
        self.state = "未启用"

    def retry(self):
        self._auth_failed = False
        self._retry_at = 0
        self._attempts = 0

    def ensure(self, config):
        if not config.get("ssh_enabled"):
            self.stop()
            self._command = None
            return self.state
        try:
            command = tunnel_command(config)
        except (ValueError, OSError) as exc:
            self.state = str(exc)
            return self.state
        if command != self._command:
            self.stop()
            self._command = command
            self._retry_at = 0
            self._auth_failed = False
            self._attempts = 0
        port = int(config.get("ssh_local_port", 18767))
        if self.process and self.process.poll() is not None:
            error = self.process.stderr.read().decode("utf-8", errors="replace").lower()
            self.stop()
            self._attempts += 1
            self._retry_at = time.monotonic() + min(60, 5 * 2 ** min(self._attempts, 4))
            if "permission denied" in error or "host key verification failed" in error:
                self._auth_failed = True
        if self._auth_failed:
            self.state = "SSH 认证失败，请检查密钥授权或主机指纹"
        elif self.process:
            self.state = "后台隧道已连接" if port_open(port) else "正在建立后台隧道"
        elif port_open(port):
            self.state = "正在使用已有本机连接；关闭后自动接管"
        elif time.monotonic() < self._retry_at:
            self.state = "连接中断，正在等待重连"
        else:
            try:
                self.process = subprocess.Popen(
                    command,
                    stdin=subprocess.DEVNULL,
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.PIPE,
                    creationflags=0x08000000 if os.name == "nt" else 0,
                    close_fds=True,
                )
                self.state = "正在建立后台隧道"
            except OSError:
                self._retry_at = time.monotonic() + 30
                self.state = "SSH 客户端启动失败，稍后重试"
        return self.state
