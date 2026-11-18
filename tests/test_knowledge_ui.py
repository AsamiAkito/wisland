"""Offscreen Qt verification; no interaction with the user's running apps."""
import os
os.environ.setdefault('QT_QPA_PLATFORM', 'offscreen')

import tempfile
import unittest
from unittest.mock import patch
from pathlib import Path
from types import SimpleNamespace

from PySide6 import QtCore, QtGui, QtWidgets
from wisland.core import bg
from wisland.knowledge.core import CaptureStore
from wisland.knowledge.service import KnowledgeService, DEFAULTS
from wisland.knowledge.integration import DropAdapter
from wisland.ui.pages.knowledge import KnowledgePage
from wisland.ui.pages.knowledge_dialogs import open_editor


class UITests(unittest.TestCase):
    def test_upload_is_opt_in_and_existing_preferences_survive(self):
        from wisland.knowledge.service import settings

        with patch('wisland.knowledge.service.cfg.get', return_value={}):
            self.assertFalse(settings()['auto_upload'])
            self.assertFalse(settings()['ssh_enabled'])
            self.assertFalse(settings()['organizer_enabled'])
            with patch('wisland.knowledge.service.run_bg', side_effect=lambda fn, **kw: fn()):
                self.service.capture([{'text': '默认只保存本机'}])
            self.assertEqual(self.service.store.rows()[0]['disposition'], 'local')
        with patch('wisland.knowledge.service.cfg.get', return_value={'auto_upload': True}):
            self.assertTrue(settings()['auto_upload'])
            with patch('wisland.knowledge.service.run_bg', side_effect=lambda fn, **kw: fn()):
                self.service.capture([{'text': '已有用户允许上传'}])
            self.assertEqual(self.service.store.rows()[0]['disposition'], 'active')

    @classmethod
    def setUpClass(cls):
        cls.app = QtWidgets.QApplication.instance() or QtWidgets.QApplication([])
        bg.init()

    def setUp(self):
        for module in ('wisland.ui.pages.knowledge',):
            mock = patch(module + '.settings', return_value=dict(DEFAULTS))
            mock.start()
            self.addCleanup(mock.stop)
        self.temp = tempfile.TemporaryDirectory()
        self.service = KnowledgeService(store=CaptureStore(self.temp.name))

    def tearDown(self):
        self.service.stop()
        self.service.store.db.close()
        self.temp.cleanup()

    def test_pagination_bounds_card_count_and_search_reaches_older_material(self):
        tasks = [self.service.store.capture(text=f'材料-{number:04d}')[0] for number in range(25)]
        page = KnowledgePage(SimpleNamespace(knowledge=self.service))
        with patch.object(self.service.store, 'rows', wraps=self.service.store.rows) as rows:
            page.on_show()
            self.assertEqual(rows.call_args.kwargs['limit'], 11)
            self.assertEqual(rows.call_args.kwargs['offset'], 0)
        self.assertEqual(len(page._cards), 10)
        self.assertFalse(page._more.isHidden())
        cards = dict(page._cards)
        for _ in range(100):
            page.refresh()
        self.assertEqual(page._cards, cards)
        first_page = set(cards)
        page._more.clicked.emit()
        self.assertEqual(len(page._cards), 10)
        self.assertEqual(page._offset, 10)
        self.assertFalse(first_page & page._cards.keys())
        page._more.clicked.emit()
        self.assertEqual(len(page._cards), 5)
        self.assertTrue(page._more.isHidden())
        page._previous.clicked.emit()
        self.assertEqual(len(page._cards), 10)
        page._previous.clicked.emit()
        self.assertEqual(set(page._cards), first_page)
        page.search.setText('材料-0000')
        page.refresh()
        self.assertEqual(set(page._cards), {tasks[0]['id']})
        page.search.clear()
        page.refresh()
        self.assertEqual(len(page._cards), 10)
        page.on_hide()
        self.assertFalse(page._progress_timer.isActive())
        page.deleteLater()
        self.app.processEvents()

    def test_repeated_editing_connects_card_save_only_once(self):
        task, _ = self.service.store.capture(text='反复编辑')
        page = KnowledgePage(SimpleNamespace(knowledge=self.service))
        page.on_show()
        card = page._cards[task['id']]
        for _ in range(8):
            page.start_edit(task['id'])
            page._finish_edit(task['id'])
        page.start_edit(task['id'])
        with patch.object(page, '_save_edit', wraps=page._save_edit) as save:
            card.save_requested.emit()
            save.assert_called_once_with(task['id'])
        self.assertIs(page._cards[task['id']], card)
        page.deleteLater()
        self.app.processEvents()

    def test_cleanup_runs_in_background_and_tick_does_not_duplicate_it(self):
        jobs = []
        def schedule(fn, *args, **callbacks):
            jobs.append((fn, args, callbacks))
        with patch('wisland.knowledge.service.run_bg', side_effect=schedule), \
                patch('wisland.knowledge.service.settings', return_value=dict(DEFAULTS)), \
                patch.object(self.service.tunnel, 'ensure'), \
                patch.object(self.service.store, 'cleanup_trash', return_value=1) as cleanup, \
                patch('wisland.knowledge.service.sync_one', return_value=False):
            self.service._tick()
            self.service._tick()
            cleanup.assert_not_called()
            self.assertEqual(len(jobs), 1)
            fn, args, callbacks = jobs[0]
            callbacks['on_done'](fn(*args))
            cleanup.assert_called_once()
            self.assertFalse(self.service._busy)

    def test_tunnel_probe_is_deferred_and_stopped_service_never_restarts_it(self):
        jobs = []
        with patch('wisland.knowledge.service.settings', return_value=dict(DEFAULTS)), \
                patch('wisland.knowledge.service.run_bg', side_effect=lambda fn, **kw: jobs.append(fn)), \
                patch.object(self.service.tunnel, 'ensure') as ensure, \
                patch('wisland.knowledge.service.sync_one', return_value=False):
            self.service._tick()
            ensure.assert_not_called()
            jobs[0]()
            ensure.assert_called_once()
            self.service.stop()
            self.service._sync()
            ensure.assert_called_once()

    def test_temporary_menu_releases_submenus_on_close(self):
        import shiboken6
        from wisland.ui.pages.system import styled_menu
        page = QtWidgets.QWidget()
        for _ in range(10):
            menu = styled_menu(page)
            submenu = styled_menu(menu)
            submenu.addAction('测试操作')
            menu.addMenu(submenu)
            menu.popup(QtCore.QPoint(20, 20))
            menu.hide()
            QtCore.QCoreApplication.sendPostedEvents(None, QtCore.QEvent.Type.DeferredDelete)
            self.assertFalse(shiboken6.isValid(menu))
            self.assertFalse(shiboken6.isValid(submenu))
        self.assertEqual(page.findChildren(QtWidgets.QMenu), [])
        page.deleteLater()
        self.app.processEvents()

    def test_mime_text_image_urls_and_files(self):
        mime = QtCore.QMimeData()
        mime.setText('主动保存的文字')
        self.assertEqual(self.service.payload(mime), [{'text': '主动保存的文字'}])
        mime = QtCore.QMimeData()
        mime.setUrls([QtCore.QUrl('https://example.com/article'), QtCore.QUrl.fromLocalFile('C:/sample.pdf')])
        payload = self.service.payload(mime)
        self.assertEqual(payload[0]['text'], 'https://example.com/article')
        self.assertEqual(payload[1]['source'], 'C:/sample.pdf')
        image = QtGui.QImage(4, 4, QtGui.QImage.Format.Format_RGB32)
        image.fill(QtGui.QColor('red'))
        mime = QtCore.QMimeData()
        mime.setImageData(image)
        self.assertFalse(self.service.payload(mime)[0]['image'].isNull())

    def test_page_renders_empty_and_populated(self):
        page = KnowledgePage(SimpleNamespace(knowledge=self.service))
        page.resize(612, 390)
        page.on_show()
        self.assertEqual(page.list.columnCount(), 2)
        self.service.store.capture(text='Java OpenFeign 重试：保存后稍后整理')
        page.refresh()
        page.show()
        self.app.processEvents()
        image = page.grab().toImage()
        self.assertFalse(image.isNull())
        page.close()
        page.deleteLater()
        self.app.processEvents()

    def test_drop_uses_copy_and_restores_lock(self):
        island = QtWidgets.QWidget()
        island.body = QtCore.QRectF(0, 0, 240, 48)
        island.locks = set()
        island.poll = QtCore.QTimer()
        island.passthrough = lambda: False
        island.resize(240, 48)
        adapter = DropAdapter(island, self.service)
        mime = QtCore.QMimeData()
        mime.setText('测试拖放')
        event = QtGui.QDragEnterEvent(QtCore.QPoint(20, 20), QtCore.Qt.DropAction.CopyAction | QtCore.Qt.DropAction.MoveAction,
                                     mime, QtCore.Qt.MouseButton.LeftButton, QtCore.Qt.KeyboardModifier.NoModifier)
        adapter.eventFilter(island, event)
        self.assertTrue(event.isAccepted())
        self.assertEqual(event.dropAction(), QtCore.Qt.DropAction.CopyAction)
        self.assertIn('knowledge-drop', island.locks)
        with unittest.mock.patch.object(self.service, 'capture') as capture:
            drop = QtGui.QDropEvent(QtCore.QPointF(20, 20), QtCore.Qt.DropAction.CopyAction, mime,
                                   QtCore.Qt.MouseButton.LeftButton, QtCore.Qt.KeyboardModifier.NoModifier)
            adapter.eventFilter(island, drop)
            capture.assert_called_once_with([{'text': '测试拖放'}])
        self.assertNotIn('knowledge-drop', island.locks)
        self.app.removeEventFilter(adapter)
        island.deleteLater()
        self.app.processEvents()

    def test_retry_network_error_only_resumes_polling(self):
        task, _ = self.service.store.capture(text='网络错误不会重新收费')
        self.service.store.update(task['id'], remote=task['id'], state='retry', attempts=2)
        def immediate(fn, on_done=None, on_error=None, **_kwargs):
            try:
                value = fn()
            except Exception as exc:
                if on_error:
                    on_error(exc)
                else:
                    raise
            else:
                if on_done:
                    on_done(value)
        with patch('wisland.knowledge.service.run_bg', side_effect=immediate), patch.object(self.service, '_tick'), patch('httpx.Client') as client:
            self.service.retry(task['id'])
            client.assert_not_called()
        self.assertEqual(self.service.store.get(task['id'])['state'], 'remote')

    def test_organizer_schedules_with_existing_background_runner(self):
        config = dict(DEFAULTS, organizer_enabled=True, ssh_enabled=True)
        jobs = []
        def schedule(fn, *args, on_done=None, on_error=None):
            jobs.append((fn, args, on_done))
        with patch('wisland.knowledge.service.settings', return_value=config), \
                patch.object(self.service.tunnel, 'ensure'), \
                patch('wisland.knowledge.service.run_bg', side_effect=schedule), \
                patch('wisland.knowledge.organizer.maintain_once', return_value=False) as maintain:
            self.service._tick()
            self.assertTrue(self.service._organizer_busy)
            fn, args, done = jobs[0]
            done(fn(*args))
            maintain.assert_called_once()
            self.assertFalse(self.service._organizer_busy)

    def test_explicit_deep_processing_changes_only_selected_task(self):
        row,_=self.service.store.capture(text='需要深读的资料')
        other,_=self.service.store.capture(text='默认轻量资料')
        with patch.object(self.service,'upload') as upload:
            self.service.reprocess(row['id'],processing_mode='deep')
            upload.assert_called_once_with(row['id'])
        self.assertEqual(self.service.store.get(row['id'])['processing_mode'],'deep')
        self.assertEqual(self.service.store.get(other['id'])['processing_mode'],'index')

    def test_settings_independent_modeless_and_does_not_lock_island(self):
        island = QtWidgets.QWidget()
        island.locks = {'focus'}
        island.release_focus = lambda: island.locks.discard('focus')
        page = KnowledgePage(SimpleNamespace(knowledge=self.service))
        page.setParent(island)
        page.island = island
        island.show()
        dialog = page._settings()
        self.assertIsNone(dialog.parentWidget())
        self.assertFalse(dialog.isModal())
        self.assertTrue(dialog.windowFlags() & QtCore.Qt.WindowType.WindowStaysOnTopHint)
        self.assertEqual(island.locks, set())
        island.hide()
        self.app.processEvents()
        self.assertTrue(dialog.isVisible())
        self.assertIs(page._settings(), dialog)
        dialog.reject()
        self.assertIsNone(page._settings_dialog)
        page.deleteLater()
        island.deleteLater()
        self.app.processEvents()

    def test_settings_save_persists_and_reloads_without_losing_other_fields(self):
        import json
        from wisland.core.config import Config
        config_path = Path(self.temp.name) / 'config.json'
        config_path.write_text(json.dumps({'unrelated': {'keep': True}}), encoding='utf-8')
        vault = Path(self.temp.name) / 'vault'
        (vault / '.obsidian').mkdir(parents=True)
        with patch('wisland.core.config.CONFIG_PATH', config_path):
            saved_config = Config()
            with patch('wisland.ui.pages.knowledge.cfg', saved_config), patch.object(self.service, '_tick'):
                page = KnowledgePage(SimpleNamespace(knowledge=self.service))
                dialog = page._settings()
                fields = dialog.findChildren(QtWidgets.QLineEdit)
                fields[0].setText(str(vault))
                fields[1].setText('http://127.0.0.1:18767')
                fields[2].setText('test-access-token-' + 'x' * 32)
                for checkbox in dialog.findChildren(QtWidgets.QCheckBox):
                    if checkbox.text().startswith('随灵动岛'):
                        checkbox.setChecked(True)
                dialog.findChild(QtWidgets.QDialogButtonBox).button(QtWidgets.QDialogButtonBox.StandardButton.Save).click()
                self.assertIsNone(page._settings_dialog)
                reloaded = Config()
                self.assertEqual(reloaded.get('knowledge.vault'), str(vault))
                self.assertEqual(reloaded.get('knowledge.server_url'), 'http://127.0.0.1:18767')
                self.assertTrue(reloaded.get('knowledge.ssh_enabled'))
                self.assertTrue(reloaded.get('unrelated.keep'))
                page.deleteLater()
                self.app.processEvents()
    def test_scroll_area_tooltip_keeps_readable_background(self):
        from wisland.ui.widgets import ScrollArea
        for effect in (QtCore.Qt.UIEffect.UI_AnimateTooltip, QtCore.Qt.UIEffect.UI_FadeTooltip):
            self.app.setEffectEnabled(effect, False)
        host = QtWidgets.QWidget()
        host.resize(320, 220)
        area = ScrollArea(host)
        area.resize(320, 220)
        child = QtWidgets.QWidget(area.inner)
        child.resize(220, 90)
        child.setToolTip('滚动区域中的提示文字')
        host.show()
        child.show()
        self.app.processEvents()
        try:
            event = QtGui.QHelpEvent(QtCore.QEvent.Type.ToolTip, QtCore.QPoint(10, 10),
                                    child.mapToGlobal(QtCore.QPoint(10, 10)))
            self.app.sendEvent(child, event)
            self.app.processEvents()
            tips = [w for w in self.app.topLevelWidgets()
                    if w.metaObject().className() == 'QTipLabel' and w.isVisible()]
            self.assertTrue(tips)
            palette = tips[0].palette()
            foreground = palette.color(QtGui.QPalette.ColorRole.ToolTipText)
            background = palette.color(QtGui.QPalette.ColorRole.ToolTipBase)
            self.assertNotEqual(foreground.rgb(), background.rgb())
            self.assertEqual(background.alpha(), 255)
        finally:
            QtWidgets.QToolTip.hideText()
            host.close()
            host.deleteLater()
            self.app.processEvents()

    def test_card_metadata_tooltip_disabled_while_editing(self):
        task, _ = self.service.store.capture(text='卡片悬停测试')
        page = KnowledgePage(SimpleNamespace(knowledge=self.service))
        page.on_show()
        card = page._cards[task['id']]
        self.assertTrue(card.toolTip())
        editor = open_editor(page, task['id'])
        self.assertEqual(card.toolTip(), '')
        editor.cancel_requested.emit()
        self.assertTrue(page._cards[task['id']].toolTip())
        page.deleteLater()
        self.app.processEvents()

    def test_progress_and_trash_actions(self):
        task, _ = self.service.store.capture(text='进度显示')
        page = KnowledgePage(SimpleNamespace(knowledge=self.service))
        page.on_show()
        self.service.store.update(task['id'], state='uploading', progress=37)
        page._update_progress()
        card = page._cards[task['id']]
        self.assertEqual(card.status, '正在上传')
        self.assertEqual(card.progress, 37)
        self.service.store.update(task['id'], state='remote', stage='AI 提炼', progress=-1)
        page._update_progress()
        self.assertEqual(card.progress, -1)
        self.assertEqual(card.status, 'AI 提炼')
        card.remove_requested.emit()
        self.assertEqual(self.service.store.rows(), [])
        page._toggle_trash()
        self.assertEqual(len(self.service.store.rows(trash=True)), 1)
        self.service.restore(task['id'])
        self.assertEqual(self.service.store.get(task['id'])['disposition'], 'local')
        page.deleteLater()
        self.app.processEvents()

    def test_type_and_tag_filters_show_real_classification(self):
        import json
        image, _ = self.service.store.capture(data=b'fake png', name='示例.png')
        self.service.store.save_metadata(image['id'], ['网络请求'], '先看条件')
        text, _ = self.service.store.capture(text='Java 示例')
        self.service.store.update(text['id'], result=json.dumps({'title': 'Java', 'tags': ['Java'], 'summary': '', 'original': ''}), state='done')
        page = KnowledgePage(SimpleNamespace(knowledge=self.service))
        page.on_show()
        self.assertIn('网络请求', page._cards[image['id']].toolTip())
        page.set_filter(kind='image')
        self.assertEqual(set(page._cards), {image['id']})
        page.set_filter(tag='java')
        self.assertEqual(page._cards, {})
        page.set_filter(kind=None)
        self.assertEqual(set(page._cards), {text['id']})
        page.deleteLater(); self.app.processEvents()

    def test_inline_editor_saves_body_tags_and_notes(self):
        import json
        task, _ = self.service.store.capture(text='用户材料')
        page = KnowledgePage(SimpleNamespace(knowledge=self.service))
        page.on_show()
        page.resize(612, 400)
        page.show()
        self.app.processEvents()
        card = page._cards[task['id']]
        before = card.height()
        editor = open_editor(page, task['id'])
        self.assertIs(editor.parentWidget(), card)
        self.assertFalse(editor.isWindow())
        self.assertEqual(card.height(), before)
        self.assertEqual(self.service.store.get(task['id'])['editing'], 1)
        editor.content.setPlainText('编辑后的正文')
        editor.tags.setText('新标签, Java')
        editor.instructions.setPlainText('保留异常条件')
        page.refresh()
        self.assertIs(page._cards[task['id']], card)
        self.assertEqual(editor.content.toPlainText(), '编辑后的正文')
        editor.save_requested.emit()
        row = self.service.store.get(task['id'])
        self.assertEqual(json.loads(row['user_tags']), ['新标签', 'Java'])
        self.assertEqual(row['instructions'], '保留异常条件')
        self.assertEqual(row['edited_body'], '编辑后的正文')
        self.assertEqual(row['editing'], 0)
        self.assertEqual(page._inline_edits, {})
        page.deleteLater(); self.app.processEvents()

    def test_inline_cancel_discards_changes(self):
        task, _ = self.service.store.capture(text='原始正文')
        page = KnowledgePage(SimpleNamespace(knowledge=self.service))
        page.on_show()
        editor = open_editor(page, task['id'])
        editor.content.setPlainText('不要保存')
        editor.cancel_requested.emit()
        row = self.service.store.get(task['id'])
        self.assertIsNone(row['edited_body'])
        self.assertEqual(row['body'], '原始正文')
        self.assertEqual(row['editing'], 0)
        page.deleteLater(); self.app.processEvents()

    def test_compact_tags_add_remove_and_validate(self):
        task, _ = self.service.store.capture(text='标签测试')
        page = KnowledgePage(SimpleNamespace(knowledge=self.service))
        page.on_show()
        editor = open_editor(page, task['id'])
        self.assertEqual(editor.stack.count(), 2)
        self.assertTrue(editor.error.isHidden())
        editor.tags.setText('Java, 新建标签')
        editor.tags.returnPressed.emit()
        self.assertEqual(editor.values()[1], ['Java', '新建标签'])
        editor.remove_tag('Java')
        self.assertEqual(editor.values()[1], ['新建标签'])
        editor.tags.setText('字' * 33)
        with self.assertRaises(ValueError):
            editor.values()
        self.assertEqual(editor.tags.text(), '字' * 33)
        self.assertEqual(self.service.store.get(task['id'])['editing'], 1)
        editor.cancel_requested.emit()
        page.deleteLater(); self.app.processEvents()

    def test_focus_leaving_editor_saves_but_internal_focus_does_not(self):
        task, _ = self.service.store.capture(text='自动保存测试')
        page = KnowledgePage(SimpleNamespace(knowledge=self.service))
        page.on_show()
        editor = open_editor(page, task['id'])
        editor.content.setPlainText('离开时保存的新正文')
        editor._focus_changed(editor.content, editor.tags)
        self.assertFalse(editor._blur.isActive())
        self.assertIsNone(self.service.store.get(task['id'])['edited_body'])
        editor.eventFilter(editor.tags, QtCore.QEvent(QtCore.QEvent.Type.MouseButtonPress))
        editor._focus_changed(editor.tags, editor.parentWidget())
        with patch.object(editor, 'isVisible', return_value=True), patch.object(self.app, 'applicationState', return_value=QtCore.Qt.ApplicationState.ApplicationActive):
            editor._save_if_outside()
        self.assertIn(task['id'], page._inline_edits)
        outside = QtWidgets.QLineEdit()
        editor.eventFilter(outside, QtCore.QEvent(QtCore.QEvent.Type.MouseButtonPress))
        editor._focus_changed(editor.content, outside)
        self.assertTrue(editor._blur.isActive())
        editor._save_if_outside()
        self.assertEqual(self.service.store.get(task['id'])['edited_body'], '离开时保存的新正文')
        self.assertEqual(page._inline_edits, {})
        page.deleteLater(); outside.deleteLater(); self.app.processEvents()

    def test_real_mouse_tab_switch_keeps_editor_and_draft(self):
        from PySide6 import QtTest
        from wisland.ui.widgets import Segmented
        task, _ = self.service.store.capture(text='页签切换测试')
        page = KnowledgePage(SimpleNamespace(knowledge=self.service))
        page.resize(612, 400)
        page.on_show()
        page.show()
        page.activateWindow()
        self.app.processEvents()
        editor = open_editor(page, task['id'])
        editor.content.setPlainText('切换时不要提前保存的草稿')
        tabs = editor.findChild(Segmented)
        try:
            with patch.object(self.app, 'applicationState', return_value=QtCore.Qt.ApplicationState.ApplicationActive):
                QtTest.QTest.mouseClick(tabs, QtCore.Qt.MouseButton.LeftButton,
                                       pos=QtCore.QPoint(tabs.width() * 3 // 4, tabs.height() // 2))
                QtTest.QTest.qWait(300)
                self.assertIn(task['id'], page._inline_edits)
                self.assertEqual(editor.stack.currentIndex(), 1)
                self.assertTrue(editor.tags.isVisible())
                self.assertTrue(editor.instructions.isVisible())
                self.assertIsNone(self.service.store.get(task['id'])['edited_body'])
                editor.tags.setText('切换测试标签')
                QtTest.QTest.mouseClick(editor.instructions.viewport(), QtCore.Qt.MouseButton.LeftButton,
                                       pos=QtCore.QPoint(10, 10))
                QtTest.QTest.qWait(250)
                self.assertIn(task['id'], page._inline_edits)
                self.assertEqual(editor.values()[1], ['切换测试标签'])
                QtTest.QTest.mouseClick(tabs, QtCore.Qt.MouseButton.LeftButton,
                                       pos=QtCore.QPoint(tabs.width() // 4, tabs.height() // 2))
                QtTest.QTest.qWait(300)
                self.assertIn(task['id'], page._inline_edits)
                self.assertEqual(editor.stack.currentIndex(), 0)
                self.assertEqual(editor.content.toPlainText(), '切换时不要提前保存的草稿')
                QtTest.QTest.mouseClick(page.search, QtCore.Qt.MouseButton.LeftButton,
                                       pos=QtCore.QPoint(10, 10))
                QtTest.QTest.qWait(300)
                self.assertNotIn(task['id'], page._inline_edits)
                self.assertEqual(self.service.store.get(task['id'])['edited_body'], '切换时不要提前保存的草稿')
        finally:
            if task['id'] in page._inline_edits:
                page._finish_edit(task['id'])
            page.close()
            page.deleteLater()
            self.app.processEvents()

    def test_failed_autosave_persists_draft_and_can_be_recovered(self):
        import json
        task, _ = self.service.store.capture(text='保存失败测试')
        page = KnowledgePage(SimpleNamespace(knowledge=self.service))
        page.on_show()
        editor = open_editor(page, task['id'])
        editor.content.setPlainText('需要保留的草稿')
        editor.tags.setText('字' * 33)
        page._save_edit(task['id'], automatic=True)
        row = self.service.store.get(task['id'])
        self.assertEqual(json.loads(row['draft'])['content'], '需要保留的草稿')
        editor.finish()
        page._inline_edits.clear()
        self.service.store.update(task['id'], editing=0)
        page.refresh()
        restored = open_editor(page, task['id'])
        self.assertEqual(restored.content.toPlainText(), '需要保留的草稿')
        self.assertEqual(restored.tags.text(), '字' * 33)
        restored.cancel_requested.emit()
        page.deleteLater(); self.app.processEvents()

    def test_service_stop_flushes_edit_before_exit(self):
        task, _ = self.service.store.capture(text='退出前保存')
        page = KnowledgePage(SimpleNamespace(knowledge=self.service))
        page.on_show()
        editor = open_editor(page, task['id'])
        editor.content.setPlainText('退出时自动保存')
        self.service.stop()
        self.assertEqual(self.service.store.get(task['id'])['edited_body'], '退出时自动保存')
        self.assertEqual(page._inline_edits, {})
        page.deleteLater(); self.app.processEvents()

    def test_compact_toolbar_has_no_guide_or_native_filters(self):
        page = KnowledgePage(SimpleNamespace(knowledge=self.service))
        self.assertEqual(page.findChildren(QtWidgets.QComboBox), [])
        self.assertFalse(hasattr(page, '_help_dialog'))
        toolbar = page.layout().itemAt(0).layout()
        self.assertIs(toolbar.itemAt(toolbar.count() - 2).widget(), page.search)
        self.assertEqual(toolbar.itemAt(toolbar.count() - 1).widget().accessibleName(), '知识设置')
        page.deleteLater(); self.app.processEvents()

    def test_grid_and_image_thumbnail(self):
        from wisland.ui.pages.knowledge_cards import read_thumbnail
        image = QtGui.QImage(400, 240, QtGui.QImage.Format.Format_RGB32)
        image.fill(QtGui.QColor('blue'))
        file = Path(self.temp.name) / 'sample.png'
        image.save(str(file))
        task, _ = self.service.store.capture(source=file)
        self.service.store.capture(text='第二条材料')
        page = KnowledgePage(SimpleNamespace(knowledge=self.service))
        page._thumbs[task['path']] = read_thumbnail(task['path'])
        page.on_show()
        self.assertEqual(page.list.itemAtPosition(0, 0).widget(), list(page._cards.values())[0])
        self.assertIsNotNone(page.list.itemAtPosition(0, 1))
        self.assertIsNotNone(page._cards[task['id']].pm)
        page.deleteLater(); self.app.processEvents()

    def test_reprocess_waits_for_cancel_and_preserves_user_metadata(self):
        import json
        task, _ = self.service.store.capture(text='已发送的材料')
        self.service.store.update(task['id'], sent=1, sent_version=0, remote=task['id'], state='remote')
        self.service.store.save_metadata(task['id'], ['我的标签'], '改成重点整理示例')
        with patch.object(self.service, '_tick'):
            self.service.reprocess(task['id'])
            self.service._synced(False)
            self.assertEqual(self.service.store.get(task['id'])['reprocess_pending'], 1)
            self.service.store.update(task['id'], cancel_pending=0)
            self.service._synced(True)
        self.assertIsNone(self.service.store.get(task['id']))
        row = self.service.store.rows()[0]
        self.assertEqual(json.loads(row['user_tags']), ['我的标签'])
        self.assertEqual(row['instructions'], '改成重点整理示例')
        self.assertEqual(row['sent_version'], -1)


if __name__ == '__main__':
    unittest.main()
