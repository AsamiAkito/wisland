# -*- coding: utf-8 -*-
"""知识收集页：剪贴板式双列卡片，搜索与设置共用一条工具栏。"""

from __future__ import annotations

import json
import math
import time
from pathlib import Path

import httpx
from PySide6 import QtCore, QtGui, QtWidgets

from ...core.bg import run_bg
from ...core.config import cfg
from ...knowledge.catalog import KINDS, labels
from ...knowledge.core import STATES, VaultConflict, editor_document, validate_server
from ...knowledge.service import settings
from ..theme import G
from ..widgets import IconButton, Label, LineEdit, PillButton, ScrollArea, hbox, vbox
from .base import Page
from .knowledge_cards import KnowledgeCard, read_thumbnail
from .knowledge_dialogs import make_dialog, open_editor, show_dialog
from .knowledge_edit import InlineEditor
from .system import styled_menu


class KnowledgePage(Page):
    key, glyph, title, height = "knowledge", G.BOLT, "知识", 400
    PAGE_SIZE = 10

    def __init__(self, ctx):
        super().__init__(ctx)
        self.service = ctx.knowledge
        self._settings_dialog = None
        self._inline_edits = {}
        self._cards = {}
        self._thumbs = {}
        self._thumb_pending = set()
        self._offset = 0
        self._query_key = None
        self._layout_ids = None
        self.trash_mode = False
        self.kind = None
        self.tag = None
        self.search = LineEdit("搜索知识、标签或说明")
        self.search.setAccessibleName("搜索已收集材料")
        self.search.focus_wanted.connect(self.want_focus)
        self.search.textChanged.connect(self._schedule_refresh)
        self._search_timer = QtCore.QTimer(self, singleShot=True, interval=180, timeout=self.refresh)
        paste = IconButton(G.CLIP, tip="收集剪贴板")
        files = IconButton(G.ADD, tip="选择文件")
        self.trash_button = IconButton(G.TRASH, tip="回收站")
        self.filter_button = IconButton(G.SLIDERS, tip="按类型、标签筛选")
        config = IconButton(G.GEAR, tip="知识设置")
        for button, name, callback in (
            (paste, "收集剪贴板", self.service.capture_clipboard),
            (files, "选择文件", self._files),
            (self.trash_button, "回收站", self._toggle_trash),
            (self.filter_button, "筛选资料", self._filters),
            (config, "知识设置", self._settings),
        ):
            button.setAccessibleName(name)
            button.clicked.connect(callback)
        self.sc = ScrollArea()
        self.list = QtWidgets.QGridLayout(self.sc.inner)
        self.list.setContentsMargins(36, 2, 40, 10)
        self.list.setHorizontalSpacing(22)
        self.list.setVerticalSpacing(20)
        self.list.setColumnStretch(0, 1)
        self.list.setColumnStretch(1, 1)
        self._previous = PillButton("上一页")
        self._previous.setAccessibleName("查看上一页资料")
        self._previous.clicked.connect(self._previous_page)
        self._more = PillButton("下一页")
        self._more.setAccessibleName("查看下一页资料")
        self._more.clicked.connect(self._next_page)
        self._empty = Label("", 13)
        self._more.hide()
        self._previous.hide()
        self._empty.hide()
        self.setLayout(
            vbox(
                hbox(
                    paste,
                    files,
                    self.trash_button,
                    self.filter_button,
                    (self.search, 1),
                    config,
                    margins=(14, 0, 14, 0),
                ),
                (self.sc, 1),
                spacing=10,
            )
        )
        self.service.changed.connect(self._changed)
        self._progress_timer = QtCore.QTimer(self, interval=500, timeout=self._update_progress)
        self.service.stopped.connect(self._progress_timer.stop)
        self.service.stopped.connect(self._search_timer.stop)
        self.service.stopped.connect(self._flush_edits)
        self._paste = QtGui.QShortcut(QtGui.QKeySequence("Ctrl+V"), self)
        self._paste.setContext(QtCore.Qt.ShortcutContext.WidgetShortcut)
        self._paste.activated.connect(self.service.capture_clipboard)

    def _schedule_refresh(self):
        self._search_timer.start()

    def _changed(self):
        if self.active and not self.service._stopped:
            self.refresh()

    def _flush_edits(self):
        for task_id in list(self._inline_edits):
            self._save_edit(task_id, automatic=True)
            if task_id in self._inline_edits:
                self._inline_edits[task_id]["editor"].finish()

    def on_show(self):
        super().on_show()
        self._progress_timer.start()
        self.refresh()

    def on_hide(self) -> None:
        super().on_hide()
        self._progress_timer.stop()

    def _toggle_trash(self):
        if self._inline_edits:
            self.service.notice.emit("请先保存或取消当前编辑")
            return
        self.trash_mode = not self.trash_mode
        self.trash_button.set_active(self.trash_mode)
        self.search.setPlaceholderText("搜索回收站" if self.trash_mode else "搜索知识、标签或说明")
        self.refresh()

    def _popup(self, menu):
        if self.island:
            self.island.locks.add("menu")
            menu.aboutToHide.connect(lambda: self.island.locks.discard("menu"))
        menu.popup(QtGui.QCursor.pos())

    def _filters(self):
        menu = styled_menu(self)
        types = styled_menu(menu)
        types.setTitle("资料类型")
        for key, title in [(None, "全部类型"), *KINDS.items()]:
            action = types.addAction(("✓  " if self.kind == key else "    ") + title)
            action.triggered.connect(lambda _=False, key=key: self.set_filter(kind=key))
        menu.addMenu(types)
        tags = styled_menu(menu)
        tags.setTitle("标签 / AI 主题")
        options = [
            (None, "全部标签"),
            ("", "尚无标签"),
            *self.service.store.tag_options(trash=self.trash_mode),
        ]
        for key, title in options:
            action = tags.addAction(("✓  " if self.tag == key else "    ") + title)
            action.triggered.connect(lambda _=False, key=key: self.set_filter(tag=key))
        menu.addMenu(tags)
        menu.addSeparator()
        menu.addAction("清除筛选").triggered.connect(self._clear_filters)
        self._popup(menu)

    def set_filter(self, **fields):
        if "kind" in fields:
            self.kind = fields["kind"]
        if "tag" in fields:
            self.tag = fields["tag"]
        self.filter_button.set_active(self.kind is not None or self.tag is not None)
        self.refresh()

    def _clear_filters(self):
        self.set_filter(kind=None, tag=None)

    def _menu(self, task_id):
        row = self.service.store.get(task_id)
        if not row:
            return
        meta = labels(row)
        result = json.loads(row["result"]) if row["result"] else {}
        mode = (
            "轻量索引"
            if result.get("index_only")
            else ("深度整理" if result or row.get("processing_mode") == "deep" else "轻量索引")
        )
        menu = styled_menu(self)
        for text in (
            "类型：" + meta["kind"],
            "我的标签：" + ("、".join(meta["mine"]) or "未添加"),
            "处理：" + mode,
            "AI 主题：" + ("、".join(meta["ai"]) or "尚未生成"),
            "位置：" + meta["folder"],
            self.task_progress(row, bool(settings()["server_url"]))[0],
        ):
            menu.addAction(text[:95]).setEnabled(False)
        menu.addSeparator()
        if row["disposition"] == "trash":
            menu.addAction("恢复到本机").triggered.connect(lambda: self.service.restore(task_id))
        else:
            menu.addAction("编辑标签与说明").triggered.connect(lambda: open_editor(self, task_id))
            if row["note_path"]:
                menu.addAction("打开笔记").triggered.connect(lambda: self.service.open_note(task_id))
            menu.addAction("深度整理（全文提取，较慢）").triggered.connect(
                lambda: self.service.reprocess(task_id, processing_mode="deep")
            )
            if row.get("processing_mode") == "deep":
                menu.addAction("改为轻量索引").triggered.connect(
                    lambda: self.service.reprocess(task_id, processing_mode="index")
                )
            if row["disposition"] == "local":
                menu.addAction("上传整理").triggered.connect(lambda: self.service.upload(task_id))
            else:
                menu.addAction("仅保留本机").triggered.connect(lambda: self.service.keep_local(task_id))
            dirty = row["sent"] and row["metadata_version"] > row["sent_version"]
            if dirty:
                menu.addAction("使用新说明重新整理").triggered.connect(
                    lambda: self.service.reprocess(task_id)
                )
            elif row["state"] in {"failed", "retry", "quota", "conflict", "source_only"}:
                title = {"quota": "继续处理", "conflict": "另存笔记"}.get(row["state"], "重试")
                menu.addAction(title).triggered.connect(
                    lambda: self.service.retry(
                        task_id, unlimited=row["state"] == "quota", save_copy=row["state"] == "conflict"
                    )
                )
            menu.addAction("移到回收站").triggered.connect(lambda: self.service.remove(task_id))
        menu.addAction("查看原始材料").triggered.connect(lambda: self.service.open_material(task_id))
        self._popup(menu)

    @staticmethod
    def task_progress(row, configured):
        if row["disposition"] == "trash":
            return (
                "回收站 · " + ("云端清理待完成" if row["cancel_pending"] else "可恢复，7 天后清理副本"),
                0,
            )
        if row["disposition"] == "local":
            return ("仅保留本机" + (" · 云端清理待完成" if row["cancel_pending"] else ""), 0)
        if row.get("draft"):
            return ("有未完成草稿，点铅笔继续", 0)
        if row.get("editing") and not row["sent"]:
            return ("编辑中，上传已暂停", 0)
        if row["state"] in {"done", "source_only"}:
            if row["state"] == "done" and row.get("result") and json.loads(row["result"]).get("index_only"):
                return ("已收录 · 轻量索引", 100)
            return (STATES[row["state"]], 100)
        if row["state"] in {"failed", "conflict", "quota", "retry"}:
            return (STATES[row["state"]] + (" · " + row["error"] if row["error"] else ""), 0)
        if row["state"] == "queued":
            if not configured:
                return ("已本地保存，待配置服务器", 0)
            seconds = max(0, math.ceil(row["retry_at"] - time.time()))
            return ((f"{seconds} 秒后自动上传，可撤销" if seconds else "等待上传"), 0)
        if row["state"] == "uploading":
            return ("正在上传", max(0, min(100, row["progress"])))
        return (row["stage"] or STATES.get(row["state"], row["state"]), row["progress"])

    def _update_progress(self):
        if not self.active or self.service._stopped:
            return
        configured = bool(settings()["server_url"] and settings()["token"])
        for row in self.service.store.get_many(list(self._cards)):
            card = self._cards.get(row["id"])
            if card is not None:
                text, value = self.task_progress(row, configured)
                if row["sent"] and row["metadata_version"] > row["sent_version"]:
                    text += " · 新说明待重新整理"
                card.set_progress(text, value)

    def start_edit(self, task_id):
        if self._inline_edits:
            current = next(iter(self._inline_edits.values()))
            current["editor"].content.setFocus()
            return current["editor"]
        row = self.service.store.get(task_id)
        if not row or row["disposition"] == "trash":
            return None
        try:
            document = editor_document(row, settings()["vault"])
        except (OSError, ValueError) as exc:
            self.service.notice.emit(str(exc))
            return None
        card = self._cards.get(task_id)
        if card is None:
            self.refresh()
            card = self._cards.get(task_id)
        if card is None:
            return None
        self.service.store.update(task_id, editing=1)
        card.row = row
        editor = InlineEditor(row, document, self.want_focus)
        if row["draft"]:
            try:
                draft = json.loads(row["draft"])
                document = draft["document"]
                editor.content.setPlainText(draft["content"])
                editor._tags = draft["tags"]
                editor._render_tags()
                editor.tags.setText(draft["tag_input"])
                editor.instructions.setPlainText(draft["instructions"])
                editor.show_error("已恢复草稿；保存前会继续检查笔记冲突")
            except (ValueError, KeyError, TypeError):
                editor.show_error("草稿无法读取；原始记录保留，可取消后重新编辑")
        self._inline_edits[task_id] = {"card": card, "editor": editor, "document": document}
        card.set_editor(editor)
        editor.save_requested.connect(lambda: self._save_edit(task_id))
        editor.auto_save_requested.connect(lambda: self._save_edit(task_id, automatic=True))
        editor.cancel_requested.connect(lambda: self._finish_edit(task_id))
        self.want_focus()
        editor.content.setFocus()
        return editor

    def _save_edit(self, task_id, automatic=False):
        edit = self._inline_edits.get(task_id)
        if not edit:
            return
        try:
            self.service.save_document(task_id, edit["document"], *edit["editor"].values(), quiet=automatic)
        except (OSError, ValueError, VaultConflict) as exc:
            editor = edit["editor"]
            draft = dict(
                document=edit["document"],
                content=editor.content.toPlainText(),
                tags=editor._tags,
                tag_input=editor.tags.text(),
                instructions=editor.instructions.toPlainText(),
            )
            self.service.store.update(task_id, draft=json.dumps(draft, ensure_ascii=False))
            edit["editor"].show_error(str(exc))
            if automatic:
                self.service.notice.emit("自动保存未完成，草稿已保留：" + str(exc)[:70])
            return
        self._finish_edit(task_id)

    def _finish_edit(self, task_id):
        edit = self._inline_edits.pop(task_id, None)
        if edit:
            edit["card"].set_editor(None)
        self.service.store.update(task_id, editing=0, draft="")
        if not self.service._stopped:
            self.refresh()

    def _thumbnail(self, row):
        path = row["path"]
        if path in self._thumbs:
            self._cards[row["id"]].set_thumbnail(self._thumbs[path])
            return
        if path in self._thumb_pending:
            return
        self._thumb_pending.add(path)

        def ready(image):
            self._thumb_pending.discard(path)
            self._thumbs[path] = image
            if len(self._thumbs) > 80:
                self._thumbs.pop(next(iter(self._thumbs)))
            try:
                for card in self._cards.values():
                    if card.row["path"] == path:
                        card.set_thumbnail(image)
            except RuntimeError:
                pass  # 页面已关闭；后台解码结果无需更新。

        run_bg(read_thumbnail, path, on_done=ready, on_error=lambda _e: self._thumb_pending.discard(path))

    def _next_page(self) -> None:
        self._offset += self.PAGE_SIZE
        self.refresh()

    def _previous_page(self) -> None:
        self._offset = max(0, self._offset - self.PAGE_SIZE)
        self.refresh()

    def refresh(self) -> None:
        if self.service._stopped:
            return
        key = (self.search.text(), self.trash_mode, self.kind, self.tag)
        if key != self._query_key:
            self._query_key = key
            self._offset = 0
        rows = self.service.store.rows(
            key[0],
            trash=self.trash_mode,
            kind=self.kind,
            tag=self.tag,
            limit=self.PAGE_SIZE + 1,
            offset=self._offset,
        )
        if not rows and self._offset:
            self._offset = 0
            self.refresh()
            return
        has_more = len(rows) > self.PAGE_SIZE
        rows = rows[: self.PAGE_SIZE]
        ids = {row["id"] for row in rows}
        for task_id in self._inline_edits:
            if task_id not in ids:
                row = self.service.store.get(task_id)
                if row:
                    rows.insert(0, row)
        ids = {row["id"] for row in rows}
        for task_id in self._cards.keys() - ids:
            self._cards.pop(task_id).deleteLater()
        for row in rows:
            task_id = row["id"]
            if task_id in self._cards:
                self._cards[task_id].set_row(row)
                continue
            card = KnowledgeCard(row)
            self._cards[task_id] = card
            card.edit_requested.connect(lambda tid=task_id: open_editor(self, tid))
            card.menu_requested.connect(lambda tid=task_id: self._menu(tid))
            card.clicked.connect(lambda tid=task_id: self._menu(tid))
            card.save_requested.connect(lambda tid=task_id: self._save_edit(tid))
            card.cancel_requested.connect(lambda tid=task_id: self._finish_edit(tid))
            card.remove_requested.connect(
                lambda tid=task_id: self.service.restore(tid) if self.trash_mode else self.service.remove(tid)
            )
            if row["kind"] == "image":
                self._thumbnail(row)
        layout_ids = tuple(row["id"] for row in rows)
        if layout_ids != self._layout_ids:
            for index in range(self.list.rowCount()):
                self.list.setRowStretch(index, 0)
            while self.list.count():
                self.list.takeAt(0)
            for index, task_id in enumerate(layout_ids):
                self.list.addWidget(self._cards[task_id], index // 2, index % 2)
            footer = (len(rows) + 1) // 2
            self.list.addWidget(self._empty, footer, 0, 1, 2)
            self.list.addWidget(self._previous, footer + 1, 0, QtCore.Qt.AlignmentFlag.AlignHCenter)
            self.list.addWidget(self._more, footer + 1, 1, QtCore.Qt.AlignmentFlag.AlignHCenter)
            self.list.setRowStretch(footer + 2, 1)
            self._layout_ids = layout_ids
        self._more.setVisible(has_more)
        self._previous.setVisible(self._offset > 0)
        self._empty.setVisible(not rows)
        if not rows:
            text = (
                "没有匹配材料"
                if self.search.text() or self.kind or self.tag is not None
                else "拖入内容，或按 Ctrl+Alt+K 收集"
            )
            self._empty.setText(text)
        self._update_progress()

    def _files(self):
        if self.island:
            self.island.locks.add("knowledge-dialog")
        try:
            paths, _filter = QtWidgets.QFileDialog.getOpenFileNames(self, "收集资料（复制保存，原文件保留）")
            if paths:
                self.service.capture([{"source": path} for path in paths])
        finally:
            if self.island:
                self.island.locks.discard("knowledge-dialog")

    def _settings(self):
        if self._settings_dialog is not None:
            self._settings_dialog.show()
            self._settings_dialog.raise_()
            self._settings_dialog.activateWindow()
            return self._settings_dialog
        dialog = make_dialog("知识投递口设置", 460)
        self._settings_dialog = dialog
        form = QtWidgets.QFormLayout(dialog)
        config = settings()
        vault = QtWidgets.QLineEdit(config["vault"])
        url = QtWidgets.QLineEdit(config["server_url"])
        url.setPlaceholderText("https://你的知识服务域名")
        token = QtWidgets.QLineEdit(config["token"])
        token.setEchoMode(QtWidgets.QLineEdit.EchoMode.Password)
        limit = QtWidgets.QSpinBox()
        limit.setRange(1, 2048)
        limit.setSuffix(" MiB")
        limit.setValue(int(config["max_upload_mb"]))
        form.addRow("Obsidian 知识库", vault)
        form.addRow("服务器地址", url)
        form.addRow("访问令牌", token)
        form.addRow("单文件收集上限", limit)
        ssh_enabled = QtWidgets.QCheckBox("随灵动岛启动后台 SSH，无需保持 PowerShell 窗口")
        ssh_enabled.setChecked(bool(config["ssh_enabled"]))
        form.addRow(ssh_enabled)

        def ssh_changed(enabled):
            if enabled:
                url.setText(f"http://127.0.0.1:{config['ssh_local_port']}")

        ssh_enabled.toggled.connect(ssh_changed)
        organizer_enabled = QtWidgets.QCheckBox("自动归类并维护专题（使用后台 SSH）")
        organizer_enabled.setChecked(bool(config.get("organizer_enabled")))
        organizer_enabled.setEnabled(ssh_enabled.isChecked())
        ssh_enabled.toggled.connect(organizer_enabled.setEnabled)
        form.addRow(organizer_enabled)
        auto = QtWidgets.QCheckBox("新材料保存 10 秒后自动上传（取消勾选则仅保留本机）")
        auto.setChecked(bool(config["auto_upload"]))
        form.addRow(auto)
        pending = QtWidgets.QCheckBox("首次连接时，同时上传已有待上传材料")
        pending.setChecked(False)
        form.addRow(pending)
        info = QtWidgets.QLabel(
            "DeepSeek API Key 在服务器配置。未连接服务器时，材料先保存在本机。\n服务器的音视频时长上限默认 60 分钟。"
        )
        info.setWordWrap(True)
        form.addRow(info)
        test = QtWidgets.QPushButton("检查服务器连接")
        form.addRow(test)

        def check():
            try:
                validate_server(url.text().strip())
            except ValueError as exc:
                info.setText(str(exc))
                return
            base, secret = url.text().strip().rstrip("/"), token.text().strip()
            test.setEnabled(False)
            info.setText("正在检查连接…")

            def request():
                with httpx.Client(timeout=10, trust_env=False) as client:
                    response = client.get(base + "/health", headers={"Authorization": "Bearer " + secret})
                    response.raise_for_status()
                    if response.json().get("api_version") != 3:
                        raise ValueError("服务版本不匹配")

            def done(_result):
                if self._settings_dialog is dialog and dialog.isVisible():
                    info.setText("连接正常。保存后按自动上传设置处理；首次连接时可选择是否上传已有材料。")
                    test.setEnabled(True)

            def failed(_exc):
                if self._settings_dialog is dialog and dialog.isVisible():
                    info.setText("连接失败，请检查 HTTPS 地址、访问令牌及服务状态。")
                    test.setEnabled(True)

            run_bg(request, on_done=done, on_error=failed)

        test.clicked.connect(check)
        buttons = QtWidgets.QDialogButtonBox(
            QtWidgets.QDialogButtonBox.StandardButton.Save | QtWidgets.QDialogButtonBox.StandardButton.Cancel
        )
        buttons.button(QtWidgets.QDialogButtonBox.StandardButton.Save).setText("保存")
        buttons.button(QtWidgets.QDialogButtonBox.StandardButton.Cancel).setText("取消")
        form.addRow(buttons)

        def save():
            root = Path(vault.text().strip())
            if not (root / ".obsidian").is_dir():
                info.setText("请选择实际知识库根目录，需要包含 .obsidian 文件夹。")
                return
            address = url.text().strip().rstrip("/")
            secret = token.text().strip()
            try:
                if address:
                    validate_server(address)
                    if len(secret) < 24:
                        raise ValueError("服务器访问令牌至少需要 24 个字符")
                cfg.set(
                    "knowledge",
                    dict(
                        config,
                        vault=str(root),
                        server_url=address,
                        token=secret,
                        max_upload_mb=limit.value(),
                        auto_upload=auto.isChecked(),
                        ssh_enabled=ssh_enabled.isChecked(),
                        organizer_enabled=organizer_enabled.isChecked(),
                    ),
                )
                if address and not config["server_url"] and not pending.isChecked():
                    for row in self.service.store.rows():
                        if not row["sent"] and row["disposition"] == "active":
                            self.service.store.disposition(row["id"], "local")
            except (ValueError, OSError) as exc:
                info.setText(str(exc))
                return
            dialog.accept()
            self.refresh()
            self.service.tunnel.retry()
            self.service._tick()

        buttons.accepted.connect(save)
        buttons.rejected.connect(dialog.reject)

        def closed(_result):
            self._settings_dialog = None
            dialog.deleteLater()

        dialog.finished.connect(closed)
        return show_dialog(self, dialog)
