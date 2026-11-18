# -*- coding: utf-8 -*-
"""知识服务：协调本地收集、后台同步和 Qt 界面通知。"""

from __future__ import annotations

import os
import threading
import time
from pathlib import Path

import httpx
from PySide6 import QtCore, QtGui, QtWidgets

from ..core.bg import run_bg
from ..core.config import DATA_DIR, cfg
from .core import CaptureStore, sync_one, validate_server
from .tunnel import SshTunnel

DEFAULTS = {
    "vault": "",
    "server_url": "",
    "token": "",
    "max_upload_mb": 256,
    "auto_upload": False,
    "upload_delay_s": 10,
    "ssh_enabled": False,
    "organizer_enabled": False,
    "ssh_host": "",
    "ssh_user": "root",
    "ssh_port": 22,
    "ssh_local_port": 18767,
    "ssh_key": str(Path.home() / ".ssh/wisland_deploy_ed25519"),
    "ssh_project_dir": "/opt/wisland-knowledge-server",
    "ssh_service": "knowledge",
}


def settings():
    return dict(DEFAULTS, **(cfg.get("knowledge", {}) or {}))


class KnowledgeService(QtCore.QObject):
    changed = QtCore.Signal()
    notice = QtCore.Signal(str)
    stopped = QtCore.Signal()

    def __init__(self, parent=None, store=None):
        super().__init__(parent)
        self.store = store or CaptureStore(DATA_DIR / "knowledge")
        self.store.reset_editing()
        self._busy = False
        self._stopped = False
        self._last_error = 0
        self._last_cleanup = 0
        self._organizer_busy = False
        self._organizer_next = 0
        self.tunnel = SshTunnel()
        self._tunnel_lock = threading.Lock()
        self.timer = QtCore.QTimer(self, interval=2500, timeout=self._tick)

    def start(self):
        self.timer.start()
        self._tick()

    def stop(self):
        self._stopped = True
        self.timer.stop()
        self.stopped.emit()
        with self._tunnel_lock:
            self.tunnel.stop()

    @staticmethod
    def payload(mime):
        """Detach MIME data before the drop event/clipboard owner disappears."""
        items = []
        if mime.hasUrls() and any(url.isLocalFile() for url in mime.urls()):
            for url in mime.urls():
                if url.isLocalFile():
                    items.append({"source": url.toLocalFile()})
                elif url.scheme() in {"https", "http"}:
                    items.append({"text": url.toString()})
        elif mime.hasImage():
            image = QtGui.QImage(mime.imageData())
            if not image.isNull():
                # Capture the implicitly-shared image now; encoding happens in background.
                items.append({"image": image.copy()})
        elif mime.hasUrls():
            items.extend({"text": url.toString()} for url in mime.urls() if url.scheme() in {"https", "http"})
        elif mime.hasText():
            text = mime.text()
            if text.strip():
                items.append({"text": text})
        return items

    def capture_clipboard(self):
        self.capture(self.payload(QtWidgets.QApplication.clipboard().mimeData()))

    def capture(self, items):
        if not items:
            self.notice.emit("没有可收集的内容；请拖入文字、链接或普通文件")
            return
        self.notice.emit("正在保存材料…")
        limit = int(settings()["max_upload_mb"])
        config = settings()

        def save():
            saved, duplicate, errors = 0, 0, []
            for item in items:
                try:
                    item = dict(item)
                    if "image" in item:
                        image = item.pop("image")
                        buffer = QtCore.QBuffer()
                        buffer.open(QtCore.QIODevice.OpenModeFlag.WriteOnly)
                        if not image.save(buffer, "PNG"):
                            raise ValueError("图片保存失败")
                        item.update(data=bytes(buffer.data()), name="截图.png")
                    _task, existed = self.store.capture(
                        **item,
                        max_mb=limit,
                        delay_s=int(config["upload_delay_s"]),
                        local_only=not config["auto_upload"],
                    )
                    if existed:
                        duplicate += 1
                    else:
                        saved += 1
                except Exception as exc:
                    errors.append(str(exc)[:100])
            return saved, duplicate, errors

        def done(result):
            saved, duplicate, errors = result
            if errors:
                message = f"已保存 {saved} 项，重复 {duplicate} 项，失败 {len(errors)} 项：{errors[0]}"
            elif saved:
                message = f"已收下 ✓ · {saved} 项" + (f"，{duplicate} 项已收藏" if duplicate else "")
            else:
                message = "已收藏，无需重复保存"
            self.notice.emit(message)
            self.changed.emit()
            self._tick()

        run_bg(save, on_done=done, on_error=lambda _e: self.notice.emit("本地保存失败，请检查磁盘空间"))

    def _tick(self):
        if not self._stopped:
            config = settings()
            if (
                config.get("organizer_enabled")
                and config.get("ssh_enabled")
                and not self._organizer_busy
                and time.monotonic() >= self._organizer_next
            ):
                from .organizer import maintain_once

                self._organizer_busy = True
                self._organizer_next = time.monotonic() + 30
                run_bg(
                    lambda: maintain_once(
                        self.store,
                        config,
                        should_stop=lambda: self._stopped or not settings().get("organizer_enabled"),
                    ),
                    on_done=self._organized,
                    on_error=self._organizer_failed,
                )
        if self._busy or self._stopped:
            return
        self._busy = True
        run_bg(self._sync, on_done=self._synced, on_error=self._sync_failed)

    def _sync(self) -> bool:
        """清理与同步都在工作线程执行，沿用同一把同步忙锁。"""
        with self._tunnel_lock:
            if self._stopped:
                return False
            self.tunnel.ensure(settings())
        if self._stopped:
            return False
        cleaned = False
        if time.monotonic() - self._last_cleanup > 3600:
            cleaned = bool(self.store.cleanup_trash())
            self._last_cleanup = time.monotonic()
        changed = sync_one(self.store, settings())
        return cleaned or changed

    def _organized(self, changed):
        self._organizer_busy = False
        if changed and not self._stopped:
            self.changed.emit()

    def _organizer_failed(self, _error):
        self._organizer_busy = False
        self._organizer_next = time.monotonic() + 300

    def keep_local(self, task_id):
        self.store.disposition(task_id, "local")
        self.changed.emit()
        self.notice.emit("已设为仅保留本机；上传过的材料将请求云端清理")

    def remove(self, task_id):
        self.store.disposition(task_id, "trash")
        self.changed.emit()
        self.notice.emit("已移到回收站，7 天内可恢复；原文件和 Obsidian 笔记保留")

    def restore(self, task_id):
        self.store.disposition(task_id, "local")
        self.changed.emit()
        self.notice.emit("已恢复为仅保留本机，不会自动上传")

    def upload(self, task_id):
        with self.store.lock:
            row = self.store.get(task_id)
            if not row or row["disposition"] == "trash":
                return
            if row["editing"] or row["draft"]:
                self.notice.emit("请先保存或关闭这条材料的编辑窗口")
                return
            if row["cancel_pending"]:
                self.notice.emit("云端清理尚未完成，完成后可重新上传")
                return
            self.store.queue_upload(task_id)
        self.changed.emit()
        self.notice.emit("将在 10 秒后上传，期间可移除或改为仅本地")
        self._tick()

    def _synced(self, changed):
        self._busy = False
        if self._stopped:
            return
        if changed:
            self.changed.emit()
        for task_id in self.store.pending_reprocess_ids():
            self.upload(task_id)

    def reprocess(self, task_id, *, processing_mode=None):
        row = self.store.get(task_id)
        if not row or row["disposition"] == "trash":
            return
        if row["editing"] or row["draft"]:
            self.notice.emit("请先保存或关闭编辑窗口，再重新整理")
            return
        if processing_mode is not None:
            if processing_mode not in {"index", "deep"}:
                raise ValueError("处理模式无效")
            self.store.update(task_id, processing_mode=processing_mode)
        if not row["sent"]:
            self.upload(task_id)
            return
        self.store.disposition(task_id, "local")
        self.store.update(task_id, reprocess_pending=1)
        self.changed.emit()
        self.notice.emit("正在取消旧任务，完成后自动使用新标签和说明重新整理")
        self._tick()

    def save_metadata(self, task_id, tags, instructions):
        self.store.save_metadata(task_id, tags, instructions)
        self.changed.emit()
        row = self.store.get(task_id)
        self.notice.emit(
            "标签和说明已保存；已发送的任务需点击“重新整理”应用新说明"
            if row["sent"]
            else "标签和说明已保存，上传前会附带给 AI"
        )

    def save_document(self, task_id, document, content, tags, instructions, *, quiet=False):
        from .core import save_document

        save_document(self.store, task_id, settings()["vault"], document, content, tags, instructions)
        self.changed.emit()
        if not quiet:
            self.notice.emit("正文、标签和说明已保存；原始附件保留")

    def _sync_failed(self, _exc):
        self._busy = False
        if time.monotonic() - self._last_error > 300:
            self.notice.emit("知识服务设置有误，请打开知识页检查")
            self._last_error = time.monotonic()

    def retry(self, task_id, *, unlimited=False, save_copy=False):
        if self._busy:
            self.notice.emit("正在同步，请稍后重试")
            return
        task = self.store.get(task_id)
        if not task:
            return
        if task["disposition"] != "active":
            self.notice.emit("该材料已暂停或移除，请使用“上传整理”重新提交")
            return
        if task["state"] == "conflict" and not save_copy:
            self.notice.emit("笔记已有修改，请先打开查看，或选择另存")
            return
        self._busy = True

        def retry():
            if save_copy:
                # Keep the user-edited note; next write selects a different filename.
                relative = task["note_path"]
                if relative:
                    path = Path(relative)
                    new_path = path.with_stem(path.stem + "-另存-" + str(int(time.time())))
                    self.store.update(
                        task_id, note_path=new_path.as_posix(), note_hash="", state="received", error=""
                    )
                    return
            if task["result"] and task["state"] != "source_only":
                self.store.update(task_id, state="received", error="", retry_at=0)
                return
            config = settings()
            if task["remote"] and task["state"] in {"failed", "quota", "source_only"}:
                validate_server(config["server_url"])
                with httpx.Client(
                    timeout=15, trust_env=False, headers={"Authorization": "Bearer " + config["token"]}
                ) as client:
                    response = client.post(
                        config["server_url"].rstrip("/") + f"/v1/tasks/{task['remote']}/retry",
                        params={
                            "unlimited": "1" if unlimited else "0",
                            "reprocess": "1" if task["state"] == "source_only" else "0",
                            "mode": task["processing_mode"],
                        },
                    )
                    response.raise_for_status()
            self.store.update(
                task_id,
                state="remote" if task["remote"] else "queued",
                error="",
                attempts=0,
                retry_at=0,
                unlimited=int(unlimited),
                result="",
                ack=0,
            )

        def done(_result):
            self._busy = False
            self.changed.emit()
            self._tick()

        def failed(_error):
            self._busy = False
            self.notice.emit("重试未提交，请检查服务器连接和令牌")

        run_bg(retry, on_done=done, on_error=failed)

    def open_note(self, task_id):
        from .core import inside

        task = self.store.get(task_id)
        if task and task["note_path"]:
            try:
                path = inside(settings()["vault"], task["note_path"])
                if not path.is_file():
                    raise ValueError("笔记不存在")
                url = QtCore.QUrl("obsidian://open")
                query = QtCore.QUrlQuery()
                query.addQueryItem("path", str(path))
                url.setQuery(query)
                QtGui.QDesktopServices.openUrl(url)
            except (OSError, ValueError):
                self.notice.emit("无法打开笔记，请检查知识库路径")

    def open_material(self, task_id):
        task = self.store.get(task_id)
        if task:
            os.startfile(str(self.store.root))  # Open storage folder, never execute captured files.
