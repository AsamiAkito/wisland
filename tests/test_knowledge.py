"""Run: .venv/Scripts/python.exe -m unittest discover -s tests -v"""
import hashlib
import json
import os
import tempfile
import threading
import time
import unittest
import zipfile
import base64
import contextlib
import io
from pathlib import Path
from unittest.mock import patch

import httpx

from wisland.knowledge.core import CaptureStore, VaultConflict, inside, sync_one, validate_server, write_note
from wisland.knowledge.core import editor_document, save_document
from server.app import Jobs, Worker, make_server
from server.processing import DeepSeekProcessor, QuotaExceeded, public_url, index_excerpt


def result():
    return {'status': 'processed', 'title': 'OpenFeign 重试机制', 'summary': '只有满足重试条件才重试。',
            'points': ['保留重试次数和异常条件'], 'tags': ['Java'], 'original': '可检索的完整提取正文', 'warning': ''}


def index_result():
    return dict(result(), category='技术开发', topic='Spring与服务调用', sufficient=True,
                purpose='查阅服务调用与重试条件', reason='')


class CoreTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.store = CaptureStore(self.root / 'queue')
        self.vault = self.root / 'vault'
        (self.vault / '.obsidian').mkdir(parents=True)

    def tearDown(self):
        self.store.db.close()
        self.temp.cleanup()

    def test_paged_search_filters_before_limiting_and_preserves_literal_matching(self):
        for number in range(65):
            self.store.capture(text=f'普通材料 {number}')
        task, _ = self.store.capture(text='Straße 100%_ 完整正文')
        self.store.save_metadata(task['id'], ['开发'], '定位异常条件')
        self.store.update(task['id'], result=json.dumps({'tags': [' JAVA '], 'purpose': '用途说明'}))
        expected = self.store.rows()
        pages = self.store.rows(limit=60) + self.store.rows(limit=60, offset=60)
        self.assertEqual([r['id'] for r in pages], [r['id'] for r in expected])
        self.assertEqual(len({r['id'] for r in pages}), 66)
        self.assertEqual(self.store.rows('STRASSE 100%_', kind='text', tag='JAVA', limit=1)[0]['id'], task['id'])
        self.assertEqual(self.store.rows('100xx'), [])
        self.assertEqual(len(self.store.rows(tag='')), 65)
        self.assertEqual(dict(self.store.tag_options()), {'java': 'JAVA', '开发': '开发'})
        self.store.update(task['id'], result='')
        self.assertEqual(self.store.rows('用途说明'), [])
        self.assertEqual(self.store.rows(tag='java'), [])
        self.store.disposition(task['id'], 'trash')
        self.assertEqual(self.store.rows('STRASSE'), [])
        self.assertEqual(self.store.rows('STRASSE', trash=True)[0]['id'], task['id'])

    def test_existing_database_backfills_search_once_and_upload_clears_old_result(self):
        task, _ = self.store.capture(text='旧版材料')
        self.store.update(task['id'], result=json.dumps({'summary': '旧摘要', 'tags': ['旧标签']}), sent=1)
        self.store.db.execute('ALTER TABLE tasks DROP COLUMN search_text')
        self.store.db.execute('ALTER TABLE tasks DROP COLUMN search_tags')
        self.store.db.commit()
        self.store.db.close()
        self.store = CaptureStore(self.root / 'queue')
        self.assertEqual(self.store.rows('旧摘要', tag='旧标签')[0]['id'], task['id'])
        new_id = self.store.queue_upload(task['id'])
        self.assertNotEqual(new_id, task['id'])
        self.assertEqual(self.store.rows('旧摘要'), [])
        self.assertEqual(self.store.rows(tag='旧标签'), [])
        self.assertEqual(self.store.rows('旧版材料')[0]['id'], new_id)

    def test_sync_selection_is_bounded_and_offline_still_writes_received_notes(self):
        for number in range(12):
            self.store.capture(text=f'排队材料 {number}')
        task, _ = self.store.capture(text='已返回的材料')
        self.store.update(task['id'], state='received', result=json.dumps(result(), ensure_ascii=False))
        cancelled, _ = self.store.capture(text='待取消的材料')
        self.store.update(cancelled['id'], cancel_pending=1, disposition='local')
        self.assertLessEqual(len(self.store.sync_rows()), 4)
        self.assertEqual([row['id'] for row in self.store.sync_rows(network=False)], [task['id']])
        sync_one(self.store, {'vault': str(self.vault)})
        self.assertEqual(self.store.get(task['id'])['state'], 'done')

    def test_capture_survives_restart_and_deduplicates(self):
        first, existed = self.store.capture(text='一个需要保存的知识点')
        self.assertFalse(existed)
        self.assertEqual(Path(first['path']).read_text(encoding='utf-8'), '一个需要保存的知识点')
        self.store.db.close()
        self.store = CaptureStore(self.root / 'queue')
        second, existed = self.store.capture(text='一个需要保存的知识点')
        self.assertTrue(existed)
        self.assertEqual(first['id'], second['id'])
        self.assertEqual(len(list((self.root / 'queue').glob('*.txt'))), 1)
        long, _ = self.store.capture(text='长标题' * 30 + '\n隐藏在正文后面的关键字')
        self.assertEqual(self.store.rows('正文后面')[0]['id'], long['id'])

    def test_copy_keeps_original_and_rejects_bad_input(self):
        original = self.root / '资料.pdf'
        original.write_bytes(b'%PDF-test')
        task, _ = self.store.capture(source=original)
        self.assertEqual(original.read_bytes(), Path(task['path']).read_bytes())
        with self.assertRaises(ValueError):
            self.store.capture(source=self.root)
        with self.assertRaises(ValueError):
            self.store.capture(text='')
        with self.assertRaises(ValueError):
            self.store.capture(source=original, max_mb=0)
        exe = self.root / 'tool.exe'
        exe.write_bytes(b'not executed')
        with self.assertRaises(ValueError):
            self.store.capture(source=exe)

    def test_failed_copy_never_acknowledged(self):
        file = self.root / 'file.txt'
        file.write_bytes(b'original')
        with patch('os.fsync', side_effect=OSError('disk full')):
            with self.assertRaises(OSError):
                self.store.capture(source=file)
        self.assertEqual(self.store.rows(), [])
        self.assertFalse(list((self.root / 'queue').glob('*.tmp')))

    def test_write_idempotent_and_user_edit_protected(self):
        task, _ = self.store.capture(text='原文不能丢失')
        self.store.update(task['id'], result=json.dumps(result(), ensure_ascii=False), state='received')
        task = self.store.get(task['id'])
        note = write_note(self.store, task, self.vault)
        original_bytes = note.read_bytes()
        self.assertIn('原文不能丢失', original_bytes.decode('utf-8'))
        self.assertEqual(note, write_note(self.store, task, self.vault))
        self.assertEqual(len(list(self.vault.rglob('*.md'))), 1)
        note.write_text('用户自己补充的重要内容', encoding='utf-8')
        with self.assertRaises(VaultConflict):
            write_note(self.store, self.store.get(task['id']), self.vault)
        self.assertEqual(note.read_text(encoding='utf-8'), '用户自己补充的重要内容')

    def test_path_traversal_and_server_address(self):
        with self.assertRaises(ValueError):
            inside(self.vault, '../outside.md')
        for address in ['http://example.com', 'https://user:secret@example.com', 'https://example.com/?token=x']:
            with self.assertRaises(ValueError):
                validate_server(address)
        validate_server('http://127.0.0.1:8766')
        validate_server('https://knowledge.example.com')

    def test_offline_is_saved_and_network_failure_backs_off(self):
        task, _ = self.store.capture(text='断网也能收集')
        self.assertFalse(sync_one(self.store, {}))
        def offline(_request):
            raise httpx.ConnectError('offline')
        sync_one(self.store, {'server_url': 'http://localhost', 'token': 'test'}, httpx.MockTransport(offline))
        row = self.store.get(task['id'])
        self.assertEqual(row['state'], 'retry')
        self.assertGreater(row['retry_at'], time.time())
        self.assertTrue(Path(row['path']).is_file())

    def test_saved_result_writes_without_server(self):
        task, _ = self.store.capture(text='本地结果')
        self.store.update(task['id'], result=json.dumps(result()), state='received')
        sync_one(self.store, {'vault': str(self.vault)})
        self.assertEqual(self.store.get(task['id'])['state'], 'done')
        self.assertEqual(len(list(self.vault.rglob('*.md'))), 1)

    def test_source_only_goes_to_inbox(self):
        task, _ = self.store.capture(text='https://example.com/private')
        data = dict(result(), status='source_only', warning='未能提取正文')
        self.store.update(task['id'], result=json.dumps(data))
        note = write_note(self.store, self.store.get(task['id']), self.vault)
        self.assertIn('00 Inbox收件箱', str(note))
        self.assertEqual(self.store.get(task['id'])['state'], 'source_only')

    def test_invalid_cloud_result_does_not_write(self):
        task, _ = self.store.capture(text='正文')
        self.store.update(task['id'], remote=task['id'], state='remote')
        def invalid(_req):
            return httpx.Response(200, json={'state': 'ready', 'result': {'status': 'processed', 'points': 'bad'}})
        sync_one(self.store, {'server_url': 'http://localhost', 'token': 'test'}, httpx.MockTransport(invalid))
        self.assertEqual(self.store.get(task['id'])['state'], 'retry')
        self.assertEqual(list(self.vault.rglob('*.md')), [])

    def test_grace_window_and_local_only_never_upload(self):
        task, _ = self.store.capture(text='十秒撤销窗口', delay_s=10)
        with patch('httpx.Client') as client:
            self.assertFalse(sync_one(self.store, {'server_url': 'http://localhost', 'token': 'test'}))
            client.assert_not_called()
        self.store.disposition(task['id'], 'local')
        self.store.update(task['id'], retry_at=0)
        self.assertFalse(sync_one(self.store, {'server_url': 'http://localhost', 'token': 'test'}))

    def test_trash_restore_cleanup_preserve_source_and_vault(self):
        original = self.root / '原始文件.txt'
        original.write_text('重要内容', encoding='utf-8')
        task, _ = self.store.capture(source=original)
        self.store.update(task['id'], result=json.dumps(result()), state='received')
        note = write_note(self.store, self.store.get(task['id']), self.vault)
        self.store.disposition(task['id'], 'trash')
        self.assertEqual(self.store.rows(), [])
        self.assertEqual(len(self.store.rows(trash=True)), 1)
        self.assertTrue(note.exists())
        self.store.disposition(task['id'], 'local')
        self.assertEqual(self.store.get(task['id'])['disposition'], 'local')
        self.store.disposition(task['id'], 'trash')
        self.store.update(task['id'], removed_at=time.time() - 8 * 86400)
        self.store.cleanup_trash()
        self.assertIsNone(self.store.get(task['id']))
        self.assertTrue(original.exists())
        self.assertTrue(note.exists())

    def test_late_result_cannot_resurrect_removed_task(self):
        task, _ = self.store.capture(text='误投递材料')
        self.store.update(task['id'], remote=task['id'], sent=1, state='remote')
        def respond(_request):
            self.store.disposition(task['id'], 'trash')
            return httpx.Response(200, json={'state': 'ready', 'result': result()})
        sync_one(self.store, {'server_url': 'http://localhost', 'token': 'test'}, httpx.MockTransport(respond))
        row = self.store.get(task['id'])
        self.assertEqual(row['disposition'], 'trash')
        self.assertEqual(row['result'], '')
        self.assertEqual(row['cancel_pending'], 1)
        self.assertEqual(list(self.vault.rglob('*.md')), [])

    def test_search_is_literal_and_does_not_match_json_field_names(self):
        task, _ = self.store.capture(text='Java 重试机制')
        self.store.update(task['id'], result=json.dumps(result(), ensure_ascii=False))
        self.assertEqual(len(self.store.rows('java 重试')), 1)
        self.assertEqual(self.store.rows('source_url'), [])
        self.assertEqual(self.store.rows('自动恢复失败请求'), [])

    def test_edit_metadata_preserves_source_and_is_searchable(self):
        task, _ = self.store.capture(text='原始知识点')
        original = Path(task['path']).read_bytes()
        self.store.update(task['id'], editing=1)
        self.store.save_metadata(task['id'], ['Java', '我创建的标签'], '重点保留重试条件和代码示例')
        row = self.store.get(task['id'])
        self.assertEqual(row['editing'], 0)
        self.assertEqual(row['metadata_version'], 1)
        self.assertEqual(Path(task['path']).read_bytes(), original)
        self.assertEqual(len(self.store.rows('我创建的标签 代码示例')), 1)
        self.store.update(task['id'], result=json.dumps(dict(result(), metadata_version=1)), state='received')
        note = write_note(self.store, self.store.get(task['id']), self.vault).read_text(encoding='utf-8')
        self.assertIn('我的补充说明', note)
        self.assertIn('user_tags:', note)
        self.assertIn('我创建的标签', note)

    def test_editing_pauses_upload_and_enforces_metadata_limits(self):
        task, _ = self.store.capture(text='可选编辑')
        self.store.update(task['id'], editing=1)
        with patch('httpx.Client') as client:
            self.assertFalse(sync_one(self.store, {'server_url': 'http://localhost', 'token': 'test'}))
            client.assert_not_called()
        with self.assertRaises(ValueError):
            self.store.save_metadata(task['id'], ['tag'] * 11, '')
        with self.assertRaises(ValueError):
            self.store.save_metadata(task['id'], [], '字' * 1001)

    def test_edit_body_keeps_original_and_updates_search(self):
        task, _ = self.store.capture(text='原始正文')
        document = editor_document(task, self.vault)
        save_document(self.store, task['id'], self.vault, document, '改过的正文', ['我的标签'], '整理重点')
        row = self.store.get(task['id'])
        self.assertEqual(Path(task['path']).read_text(encoding='utf-8'), '原始正文')
        self.assertEqual(Path(row['edited_path']).read_text(encoding='utf-8'), '改过的正文')
        self.assertEqual(len(self.store.rows('改过的正文')), 1)

    def test_edit_real_note_and_protect_against_ai_overwrite(self):
        task, _ = self.store.capture(text='原始正文')
        self.store.update(task['id'], result=json.dumps(result()), state='received')
        note = write_note(self.store, self.store.get(task['id']), self.vault)
        document = editor_document(self.store.get(task['id']), self.vault)
        self.assertIn('## 摘要', document['content'])
        save_document(self.store, task['id'], self.vault, document, '# 我的笔记\n\n我自己的正文', [], '')
        saved = note.read_text(encoding='utf-8')
        self.assertTrue(saved.startswith('---\n'))
        self.assertTrue(saved.endswith('# 我的笔记\n\n我自己的正文'))
        self.assertTrue(list((self.store.root / 'edit-backups').glob('*.md')))
        with self.assertRaises(VaultConflict):
            write_note(self.store, self.store.get(task['id']), self.vault)

    def test_concurrent_note_edit_keeps_both_external_content_and_draft(self):
        task, _ = self.store.capture(text='正文')
        self.store.update(task['id'], result=json.dumps(result()), state='received')
        note = write_note(self.store, self.store.get(task['id']), self.vault)
        document = editor_document(self.store.get(task['id']), self.vault)
        note.write_text('外部修改的新内容', encoding='utf-8')
        with self.assertRaises(VaultConflict):
            save_document(self.store, task['id'], self.vault, document, '我的草稿', [], '')
        self.assertEqual(note.read_text(encoding='utf-8'), '外部修改的新内容')
        self.assertIsNone(self.store.get(task['id'])['edited_body'])


class ServerTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.jobs = Jobs(self.root / 'server')
        self.store = CaptureStore(self.root / 'client')
        self.token = 'test-token-not-a-secret-' + 'x' * 24
        self.server = make_server(self.jobs, self.token, ('127.0.0.1', 0))
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.url = f'http://127.0.0.1:{self.server.server_address[1]}'
        self.vault = self.root / 'vault'
        (self.vault / '.obsidian').mkdir(parents=True)
        self.config = {'server_url': self.url, 'token': self.token, 'vault': str(self.vault)}

    def tearDown(self):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(3)
        self.store.db.close()
        self.jobs.db.close()
        self.temp.cleanup()

    def test_real_http_roundtrip_retry_and_ack(self):
        task, _ = self.store.capture(text='从拖入到入库的测试材料')
        sync_one(self.store, self.config)
        self.assertEqual(self.store.get(task['id'])['state'], 'remote')
        # Simulate a lost upload response: same task and bytes are accepted once.
        self.store.update(task['id'], remote='', state='queued')
        sync_one(self.store, self.config)
        with self.jobs.lock:
            self.assertEqual(self.jobs.db.execute('SELECT COUNT(*) FROM jobs').fetchone()[0], 1)
        worker = Worker(self.jobs, lambda _task: result())
        self.assertTrue(worker.step())
        sync_one(self.store, self.config)
        self.assertEqual(self.store.get(task['id'])['state'], 'received')
        sync_one(self.store, self.config)
        sync_one(self.store, self.config)
        self.assertEqual(self.store.get(task['id'])['state'], 'done')
        self.assertEqual(self.store.get(task['id'])['ack'], 1)
        self.assertGreater(self.jobs.get(task['id'])['ack_at'], 0)
        self.assertEqual(len(list(self.vault.rglob('*.md'))), 1)
        self.assertEqual(len(self.store.rows('完整提取正文')), 1)

    def test_auth_hash_and_upload_limit(self):
        with httpx.Client(base_url=self.url, trust_env=False) as client:
            self.assertEqual(client.get('/health').status_code, 401)
            self.assertEqual(client.get('/health', headers={'Authorization': 'Bearer ' + self.token}).status_code, 200)
            import uuid
            path = '/v1/tasks/' + str(uuid.uuid4())
            response = client.post(path, params={'kind': 'text', 'name': '../bad.txt', 'digest': '0' * 64},
                                   content=b'not correct', headers={'Authorization': 'Bearer ' + self.token})
            self.assertEqual(response.status_code, 400)
        self.assertFalse(list((self.root / 'server').glob('*.upload')))
        self.assertIsNone(self.jobs.next())

    def test_processing_mode_persists_and_deep_does_not_reuse_index(self):
        import uuid
        task,_=self.store.capture(text='同一份资料的两种处理方式')
        sync_one(self.store,self.config)
        self.assertEqual(self.jobs.get(task['id'])['processing_mode'],'index')
        data=Path(task['path']).read_bytes();new_id=str(uuid.uuid4())
        with httpx.Client(base_url=self.url,trust_env=False,headers={'Authorization':'Bearer '+self.token}) as client:
            params={'name':task['name'],'kind':task['kind'],'digest':task['digest'],'mode':'deep'}
            response=client.post('/v1/tasks/'+new_id,params=params,content=data)
            self.assertEqual(response.status_code,201)
            deep=self.jobs.get(new_id)
            self.assertEqual(deep['processing_mode'],'deep')
            self.assertNotEqual(deep['digest'],self.jobs.get(task['id'])['digest'])
            params['mode']='invalid'
            self.assertEqual(client.post('/v1/tasks/'+str(uuid.uuid4()),params=params,content=data).status_code,400)


    def test_worker_quota_then_explicit_continue(self):
        task, _ = self.store.capture(text='模拟长视频')
        sync_one(self.store, self.config)
        def process(row):
            if not row['unlimited']:
                raise QuotaExceeded('超出 60 分钟')
            return result()
        worker = Worker(self.jobs, process)
        worker.step()
        sync_one(self.store, self.config)
        self.assertEqual(self.store.get(task['id'])['state'], 'quota')
        with httpx.Client(base_url=self.url, trust_env=False) as client:
            response = client.post(f'/v1/tasks/{task["id"]}/retry?unlimited=1', headers={'Authorization': 'Bearer ' + self.token})
            self.assertEqual(response.status_code, 200)
        worker.step()
        self.assertEqual(self.jobs.get(task['id'])['state'], 'ready')

    def test_expired_job_can_be_reuploaded(self):
        task, _ = self.store.capture(text='过期后仍有本地副本')
        sync_one(self.store, self.config)
        with self.jobs.lock, self.jobs.db:
            self.jobs.db.execute('UPDATE jobs SET created=? WHERE id=?', (time.time() - 31 * 86400, task['id']))
        self.jobs.cleanup()
        self.assertEqual(self.jobs.get(task['id'])['state'], 'expired')
        sync_one(self.store, self.config)
        self.assertEqual(self.store.get(task['id'])['state'], 'queued')
        sync_one(self.store, self.config)
        self.assertEqual(self.jobs.get(task['id'])['state'], 'queued')

    def test_cancelled_upload_tombstone_and_cleanup(self):
        task, _ = self.store.capture(text='需要撤回的资料')
        sync_one(self.store, self.config)
        self.store.disposition(task['id'], 'trash')
        sync_one(self.store, self.config)
        self.assertEqual(self.store.get(task['id'])['cancel_pending'], 0)
        self.assertEqual(self.jobs.get(task['id'])['state'], 'cancelled')
        self.assertFalse(Path(self.jobs.get(task['id'])['path']).exists())
        self.assertFalse(Worker(self.jobs, lambda _task: self.fail('Cancelled task must not run')).step())
        import uuid
        new_id = str(uuid.uuid4())
        with httpx.Client(base_url=self.url, trust_env=False, headers={'Authorization': 'Bearer ' + self.token}) as client:
            self.assertEqual(client.post(f'/v1/tasks/{new_id}/cancel').json()['state'], 'cancelled')
            response = client.post(f'/v1/tasks/{new_id}', params={'name': 'x.txt', 'kind': 'text', 'digest': '0' * 64}, content=b'x')
            self.assertEqual(response.status_code, 410)

    def test_cancel_during_processing_discards_late_output(self):
        task, _ = self.store.capture(text='正在处理时取消')
        sync_one(self.store, self.config)
        def processor(row):
            self.assertEqual(self.jobs.cancel(row['id']), 'cancelling')
            return result()
        Worker(self.jobs, processor).step()
        row = self.jobs.get(task['id'])
        self.assertEqual(row['state'], 'cancelled')
        self.assertEqual(row['result'], '')
        self.assertFalse(Path(row['path']).exists())

    def test_explicit_upload_after_cancel_uses_new_id(self):
        import uuid
        task, _ = self.store.capture(text='恢复后重新上传')
        sync_one(self.store, self.config)
        self.store.disposition(task['id'], 'local')
        sync_one(self.store, self.config)
        self.assertEqual(self.store.get(task['id'])['cancel_pending'], 0)
        self.store.disposition(task['id'], 'trash')
        self.store.disposition(task['id'], 'local')
        self.assertEqual(self.store.get(task['id'])['cancel_pending'], 0)
        new_id = str(uuid.uuid4())
        with self.store.lock, self.store.db:
            self.store.db.execute("UPDATE tasks SET id=?, disposition='active', sent=0, remote='', state='queued', retry_at=0 WHERE id=?", (new_id, task['id']))
        sync_one(self.store, self.config)
        self.assertEqual(self.jobs.get(new_id)['state'], 'queued')
        self.assertTrue(self.jobs.cancelled(task['id']))

    def test_restart_finishes_pending_cancellation(self):
        task, _ = self.store.capture(text='取消中重启')
        sync_one(self.store, self.config)
        self.jobs.update(task['id'], state='processing')
        self.jobs.cancel(task['id'])
        self.jobs.db.close()
        self.jobs = Jobs(self.root / 'server')
        row = self.jobs.get(task['id'])
        self.assertEqual(row['state'], 'cancelled')
        self.assertFalse(Path(row['path']).exists())

    def test_user_metadata_reaches_worker_and_vault(self):
        task, _ = self.store.capture(text='需要按我的说明整理')
        self.store.save_metadata(task['id'], ['学习重点', '自定义'], '重点保留条件，保留代码示例')
        self.store.update(task['id'], retry_at=0)
        sync_one(self.store, self.config)
        remote = self.jobs.get(task['id'])
        self.assertEqual(json.loads(remote['user_tags']), ['学习重点', '自定义'])
        self.assertEqual(remote['instructions'], '重点保留条件，保留代码示例')
        self.assertEqual(remote['metadata_version'], 1)
        Worker(self.jobs, lambda _task: result()).step()
        sync_one(self.store, self.config)
        sync_one(self.store, self.config)
        row = self.store.get(task['id'])
        self.assertEqual(row['state'], 'done')
        self.assertEqual(row['sent_version'], row['metadata_version'])
        self.assertIn('重点保留条件', (self.vault / row['note_path']).read_text(encoding='utf-8'))

    def test_bad_metadata_is_rejected_before_queueing(self):
        import base64,uuid
        metadata = base64.b64encode(json.dumps({'tags': 'not a list', 'instructions': '', 'version': 1}).encode()).decode()
        with httpx.Client(base_url=self.url, trust_env=False) as client:
            response = client.post('/v1/tasks/' + str(uuid.uuid4()), params={'kind': 'text', 'name': 'x.txt', 'digest': '0' * 64},
                                   content=b'x', headers={'Authorization': 'Bearer ' + self.token, 'X-Capture-Metadata': metadata})
            self.assertEqual(response.status_code, 400)
        self.assertIsNone(self.jobs.next())

    def test_edited_body_is_used_as_ai_input_without_overwriting_original(self):
        task, _ = self.store.capture(text='收集时的原文')
        document = editor_document(task, self.vault)
        save_document(self.store, task['id'], self.vault, document, '用户改好的正文', [], '')
        self.store.update(task['id'], retry_at=0)
        sync_one(self.store, self.config)
        remote = self.jobs.get(task['id'])
        self.assertEqual(Path(remote['path']).read_text(encoding='utf-8'), '用户改好的正文')
        self.assertEqual(remote['kind'], 'text')
        self.assertEqual(Path(task['path']).read_text(encoding='utf-8'), '收集时的原文')


class ProcessingTests(unittest.TestCase):
    def test_default_index_uses_bounded_excerpts_and_one_call(self):
        processor = DeepSeekProcessor('fake')
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp) / 'large.txt'
            text = '开头的资料标题' + '原始文字'*25000 + '结尾的使用范围'
            path.write_text(text, encoding='utf-8')
            before = path.read_bytes()
            with patch.object(processor, 'chat', return_value=json.dumps(index_result())) as chat, \
                    patch.object(processor, 'summarize', side_effect=AssertionError('Full summary requested')):
                value = processor({'path':str(path),'kind':'text','name':'large.txt'})
            self.assertTrue(value['index_only'])
            self.assertEqual(value['original'], '')
            self.assertEqual(value['processing_mode'], 'index')
            chat.assert_called_once()
            content = chat.call_args.args[0][-1]['text']
            self.assertLess(len(content), 6200)
            self.assertIn('开头的资料标题', content)
            self.assertIn('结尾的使用范围', content)
            self.assertTrue(chat.call_args.kwargs['indexing'])
            self.assertEqual(path.read_bytes(), before)

    def test_large_pdf_indexes_five_pages_instead_of_full_document(self):
        from pypdf import PdfWriter
        processor = DeepSeekProcessor('fake')
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp)/'book.pdf'
            writer = PdfWriter()
            for _ in range(300):
                writer.add_blank_page(width=100,height=100)
            writer.write(path)
            with patch('pypdf._page.PageObject.extract_text', return_value='可读的章节概览') as extract, \
                    patch.object(processor,'chat',return_value=json.dumps(index_result())) as chat, \
                    patch.object(processor,'_provider_document',side_effect=AssertionError('Full PDF parsing')):
                value=processor({'path':str(path),'kind':'document','name':'book.pdf'})
            self.assertEqual(extract.call_count,5)
            self.assertEqual(value['coverage']['total_pages'],300)
            self.assertEqual(value['coverage']['pages'],[1,2,3,151,300])
            chat.assert_called_once()

    def test_long_image_previews_preserve_original_and_fit_vision_limits(self):
        from PIL import Image
        processor=DeepSeekProcessor('fake')
        with tempfile.TemporaryDirectory() as temp:
            path=Path(temp)/'long.png'
            Image.new('RGB',(100,12000),'white').save(path)
            before=path.read_bytes()
            with patch.object(processor,'chat',return_value=json.dumps(index_result())) as chat:
                value=processor({'path':str(path),'kind':'image','name':'long.png'})
            self.assertEqual(value['coverage']['dimensions'],[100,12000])
            images=[c for c in chat.call_args.args[0] if c['type']=='image_url']
            self.assertEqual(len(images),4)
            for block in images:
                data=base64.b64decode(block['image_url']['url'].split(',',1)[1])
                with Image.open(io.BytesIO(data)) as preview:
                    self.assertLessEqual(max(preview.size),1536)
            self.assertEqual(path.read_bytes(),before)

    def test_scanned_pdf_uploads_only_selected_preview_pages(self):
        from pypdf import PdfWriter,PdfReader
        from pypdf.generic import NameObject,DecodedStreamObject
        processor=DeepSeekProcessor('fake')
        with tempfile.TemporaryDirectory() as temp:
            path=Path(temp)/'scan.pdf';writer=PdfWriter()
            for _ in range(10):
                page=writer.add_blank_page(width=100,height=100)
                stream=DecodedStreamObject();stream.set_data(b'0 0 m 10 10 l S')
                page[NameObject('/Contents')]=writer._add_object(stream)
            writer.write(path)
            @contextlib.contextmanager
            def uploaded(preview,purpose):
                self.assertEqual(len(PdfReader(preview).pages),5)
                self.assertEqual(purpose,'rich_layout_recognition')
                yield 'file-api-preview'
            with patch.object(processor,'uploaded_file',side_effect=uploaded), \
                    patch.object(processor,'chat',return_value=json.dumps(index_result())) as chat:
                value=processor({'path':str(path),'kind':'document','name':'scan.pdf'})
            self.assertEqual(value['coverage']['pages'],[1,2,3,6,10])
            self.assertIn({'type':'file','file_id':'file-api-preview'},chat.call_args.args[0])

    def test_explicit_deep_mode_preserves_full_processing(self):
        processor=DeepSeekProcessor('fake')
        with patch.object(processor,'deep',return_value=result()) as deep, \
                patch.object(processor,'light_index',side_effect=AssertionError('Unexpected index')):
            value=processor({'path':'unused','kind':'text','name':'x.txt','processing_mode':'deep'})
        deep.assert_called_once()
        self.assertFalse(value['index_only'])
        self.assertEqual(value['original'],result()['original'])

    def test_blank_pdf_index_does_not_call_model(self):
        from pypdf import PdfWriter
        processor=DeepSeekProcessor('fake')
        with tempfile.TemporaryDirectory() as temp:
            path=Path(temp)/'blank.pdf';writer=PdfWriter();writer.add_blank_page(width=100,height=100);writer.write(path)
            with patch.object(processor,'chat',side_effect=AssertionError('Blank PDF sent to model')):
                value=processor({'path':str(path),'kind':'document','name':'blank.pdf'})
        self.assertFalse(value['sufficient'])
        self.assertEqual(value['status'],'source_only')

    def test_private_url_rejected(self):
        for url in ['http://127.0.0.1/', 'http://169.254.169.254/latest/', 'file:///etc/passwd', 'https://user:password@example.com']:
            with self.assertRaises(ValueError):
                public_url(url)

    def test_transcript_segments_and_video_frames(self):
        p = DeepSeekProcessor('fake')
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp) / 'sample.mp4'
            path.write_bytes(b'fake')
            calls = []
            def media_command(args, timeout=240):
                if args[0] == 'ffprobe':
                    return b'{"format":{"duration":"225"}}'
                if '-map' in args:
                    raise ValueError('no subtitles')
                calls.append(args)
                Path(args[-1]).write_bytes(b'fake')
                return b''
            class Response:
                def raise_for_status(self): pass
                def json(self): return {'text': '片段原文'}
            class Client:
                def __enter__(self): return self
                def __exit__(self, *_args): pass
                def post(self, *_args, **_kwargs): return Response()
            with patch.object(p, 'run_media', side_effect=media_command), patch.object(p, 'client', return_value=Client()), patch.object(p, 'image', return_value='画面文字'):
                text = p.media(path, True)
            self.assertIn('[00:00—01:50]', text)
            self.assertIn('[03:40—03:45]', text)
            self.assertEqual(len([c for c in calls if '-c:a' in c]), 3)
            self.assertEqual(text.count('关键帧'), 6)

    def test_model_json_validation(self):
        with self.assertRaises(ValueError):
            DeepSeekProcessor.validate({'title': '题目', 'summary': '摘要', 'points': '错误格式', 'tags': []})

    def test_official_chat_contract_and_truncation(self):
        processor = DeepSeekProcessor('fake')
        def respond(request):
            body = json.loads(request.content)
            self.assertEqual(body['model'], 'deepseek-flash')
            self.assertEqual(body['response_format']['type'], 'json_object')
            return httpx.Response(200, json={'choices': [{'finish_reason': 'length', 'message': {'content': '{'}}]})
        def client():
            return httpx.Client(base_url='https://api.deepseek.com/', transport=httpx.MockTransport(respond))
        with patch.object(processor, 'client', side_effect=client):
            with self.assertRaises(ValueError):
                processor.chat('资料', structured=True)

    def test_office_text_is_extracted_without_cloud_ocr(self):
        processor = DeepSeekProcessor('fake')
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp) / 'notes.docx'
            with zipfile.ZipFile(path, 'w') as archive:
                archive.writestr('word/document.xml', '<w:document xmlns:w="urn:word"><w:p><w:r><w:t>可直接读取的正文</w:t></w:r></w:p></w:document>')
            with patch.object(processor, '_provider_document', side_effect=AssertionError('Unexpected OCR')):
                self.assertIn('可直接读取的正文', processor.document(path))
                self.assertEqual(processor.document_method, 'local_text')
            path = Path(temp) / 'table.xlsx'
            with zipfile.ZipFile(path, 'w') as archive:
                archive.writestr('xl/sharedStrings.xml', '<sst xmlns="urn:x"><si><t>商品名称</t></si></sst>')
                archive.writestr('xl/worksheets/sheet1.xml', '<worksheet xmlns="urn:x"><sheetData><row><c r="A1" t="s"><v>0</v></c><c r="B1"><v>123</v></c></row></sheetData></worksheet>')
            with patch.object(processor, '_provider_document', side_effect=AssertionError('Unexpected OCR')):
                text = processor.document(path)
                self.assertIn('A1: 商品名称', text)
                self.assertIn('B1: 123', text)

    def test_pdf_extracts_text_layer_and_only_recognizes_scanned_page(self):
        from pypdf import PdfWriter
        from pypdf.generic import DictionaryObject, NameObject, DecodedStreamObject
        processor = DeepSeekProcessor('fake')
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp) / 'mixed.pdf'
            writer = PdfWriter()
            page = writer.add_blank_page(width=300, height=300)
            font = DictionaryObject({NameObject('/Type'): NameObject('/Font'), NameObject('/Subtype'): NameObject('/Type1'), NameObject('/BaseFont'): NameObject('/Helvetica')})
            page[NameObject('/Resources')] = DictionaryObject({NameObject('/Font'): DictionaryObject({NameObject('/F1'): font})})
            text = DecodedStreamObject(); text.set_data(b'BT /F1 12 Tf 20 20 Td (Readable PDF text) Tj ET')
            page[NameObject('/Contents')] = writer._add_object(text)
            scanned = writer.add_blank_page(width=300, height=300)
            drawing = DecodedStreamObject(); drawing.set_data(b'0 0 m 100 100 l S')
            scanned[NameObject('/Contents')] = writer._add_object(drawing)
            writer.add_blank_page(width=300, height=300)
            writer.write(path)
            with patch.object(processor, '_provider_document', return_value='扫描页识别正文') as cloud:
                result = processor.document(path)
                self.assertIn('Readable PDF text', result)
                self.assertIn('扫描页识别正文', result)
                cloud.assert_called_once()
                self.assertEqual(processor.document_method, 'mixed_pdf')

    def test_blank_documents_do_not_request_ocr(self):
        from pypdf import PdfWriter
        processor = DeepSeekProcessor('fake')
        with tempfile.TemporaryDirectory() as temp:
            pdf = Path(temp) / 'blank.pdf'
            writer = PdfWriter(); writer.add_blank_page(width=100, height=100); writer.write(pdf)
            word = Path(temp) / 'blank.docx'
            with zipfile.ZipFile(word, 'w') as archive:
                archive.writestr('word/document.xml', '<document><p/></document>')
            with patch.object(processor, '_provider_document', side_effect=AssertionError('Blank document sent to OCR')):
                self.assertEqual(processor.document(pdf), '')
                self.assertEqual(processor.document(word), '')

    def test_silent_video_uses_frames_without_asr(self):
        processor = DeepSeekProcessor('fake')
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp) / 'silent.mp4'
            path.write_bytes(b'fake')
            def run(args, timeout=240):
                if args[0] == 'ffprobe':
                    return b'{"format":{"duration":"12"},"streams":[{"codec_type":"video"}]}'
                if '-map' in args:
                    raise ValueError('no subtitles')
                self.assertNotIn('-c:a', args)
                Path(args[-1]).write_bytes(b'frame')
                return b''
            with patch.object(processor, 'run_media', side_effect=run), patch.object(processor, 'image', return_value='画面内容'):
                text = processor.media(path, True)
            self.assertEqual(text.count('关键帧'), 6)

    def test_user_instructions_are_used_for_summary_not_raw_extraction(self):
        processor = DeepSeekProcessor('fake')
        processor.user_context = {'tags': ['我的课程'], 'instructions': '重点保留代码示例'}
        prompts = []
        def respond(request):
            prompts.append(json.loads(request.content)['messages'][0]['content'])
            return httpx.Response(200, json={'choices': [{'finish_reason': 'stop', 'message': {'content': json.dumps(result())}}]})
        def client():
            return httpx.Client(base_url='https://api.deepseek.com/', transport=httpx.MockTransport(respond))
        with patch.object(processor, 'client', side_effect=client):
            processor.chat('资料', structured=True)
            processor.chat('完整提取资料', structured=False)
        self.assertIn('重点保留代码示例', prompts[0])
        self.assertIn('我的课程', prompts[0])
        self.assertNotIn('重点保留代码示例', prompts[1])


if __name__ == '__main__':
    unittest.main()
