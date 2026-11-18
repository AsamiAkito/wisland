import hashlib
import json
import tempfile
import unittest
from pathlib import Path

from wisland.knowledge.core import CaptureStore, write_note
from wisland.knowledge.organizer import Organizer, file_hash, maintain_once


def analysis():
    return {'title': '重试策略', 'summary': '保留条件与次数', 'points': ['重试需要明确边界'], 'tags': ['Java'],
            'category': '技术开发', 'topic': 'Spring与服务调用', 'sufficient': True,
            'original': '来源正文', 'reason': '', 'attachments': []}


class OrganizerTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.vault = self.root / 'vault'
        (self.vault / '.obsidian').mkdir(parents=True)
        self.store = CaptureStore(self.root / 'queue')
        self.agent = Organizer(self.store, self.vault)

    def tearDown(self):
        self.store.db.close()
        self.temp.cleanup()

    def test_routing_and_archive_preserve_original_text(self):
        raw = self.vault / '00 Inbox收件箱/source.md'
        raw.parent.mkdir()
        raw.write_text('用户原始记录与备注', encoding='utf-8')
        checksum = file_hash(raw)
        task, _ = self.store.capture(text='原文')
        archive = self.agent.archive('00 Inbox收件箱/source.md', checksum)
        entry = self.agent.apply(task, analysis(), [archive])
        self.assertTrue(entry['path'].startswith('05 Resources/技术开发/'))
        self.assertEqual((self.vault / archive).read_text(encoding='utf-8'), '用户原始记录与备注')
        self.assertIn('原始收集记录', (self.vault / entry['path']).read_text(encoding='utf-8'))

    def test_insufficient_material_stays_pending_and_manual_changes_protected(self):
        task, _ = self.store.capture(text='git 知识')
        value = dict(analysis(), sufficient=False, title='Git 知识：待补充原文')
        entry = self.agent.apply(task, value)
        self.assertIn('00 Inbox收件箱/待补充/', entry['path'])
        note = self.vault / entry['path']
        note.write_text('人工修订', encoding='utf-8')
        with self.assertRaises(ValueError):
            self.agent.apply(self.store.get(task['id']), value)
        self.assertEqual(note.read_text(encoding='utf-8'), '人工修订')

    def test_topics_use_real_sources_and_do_not_overwrite_user_edits(self):
        for text in ['第一次来源', '第二次来源']:
            task, _ = self.store.capture(text=text)
            self.agent.apply(task, analysis())
        self.agent.update_topics({}, cloud=lambda *_: {'body': '## 条件\n\n两份资料均强调边界。[S1][S2]'})
        topic = self.vault / '04 Knowledge/AI专题/Spring与服务调用.md'
        self.assertIn('AI 综合草稿', topic.read_text(encoding='utf-8'))
        self.assertNotIn('[S1]', topic.read_text(encoding='utf-8'))
        self.assertIn('[[05 Resources/', topic.read_text(encoding='utf-8'))
        topic.write_text('我的专题修改', encoding='utf-8')
        self.agent.update_topics({}, cloud=lambda *_: self.fail('Should not rewrite user-edited topic'))
        self.assertEqual(topic.read_text(encoding='utf-8'), '我的专题修改')

    def test_changed_source_is_not_archived(self):
        source = self.vault / 'source.md'
        source.write_text('原文', encoding='utf-8')
        checksum = file_hash(source)
        source.write_text('后来的修改', encoding='utf-8')
        with self.assertRaises(ValueError):
            self.agent.archive('source.md', checksum)
        self.assertTrue(source.exists())

    def test_existing_attachment_is_referenced_without_copying(self):
        asset = self.vault / '05 Resources/Attachments/source.png'
        asset.parent.mkdir(parents=True)
        asset.write_bytes(b'fake png')
        task, _ = self.store.capture(data=b'fake png', name='source.png')
        entry = self.agent.apply(task, dict(analysis(), attachments=['05 Resources/Attachments/source.png']))
        self.assertIn('[[05 Resources/Attachments/source.png]]', (self.vault / entry['path']).read_text(encoding='utf-8'))
        self.assertEqual(len(list(self.vault.rglob('*.png'))), 1)

    def completed_capture(self, text='新收集的原文'):
        row, _ = self.store.capture(text=text)
        self.store.update(row['id'], result=json.dumps(dict(analysis(), status='processed')), sent=1)
        write_note(self.store, self.store.get(row['id']), self.vault)
        return self.store.get(row['id'])

    def config(self):
        return {'vault': str(self.vault), 'organizer_enabled': True, 'ssh_enabled': True}

    def test_incremental_organizes_new_capture_once_and_archives_source(self):
        row = self.completed_capture()
        before = (self.vault / row['note_path']).read_bytes()
        calls = []
        def cloud(_, payload):
            calls.append(payload['mode'])
            return analysis()
        self.assertTrue(maintain_once(self.store, self.config(), cloud))
        current = self.store.get(row['id'])
        self.assertTrue(current['note_path'].startswith('05 Resources/技术开发/'))
        self.assertEqual(current['state'], 'done')
        self.assertEqual(next((self.vault / '09 Archive/Agent原始收集').glob('*.md')).read_bytes(), before)
        self.assertFalse(maintain_once(self.store, self.config(), cloud))
        self.assertEqual(calls, ['classify'])

    def test_paused_editing_draft_and_disabled_never_call_cloud(self):
        for state in ['local', 'editing', 'draft', 'disabled']:
            with self.subTest(state=state):
                row = self.completed_capture(state)
                if state == 'local':
                    self.store.disposition(row['id'], 'local')
                elif state in {'editing', 'draft'}:
                    self.store.update(row['id'], **{state: 1 if state == 'editing' else '{}'})
                config = self.config()
                config['organizer_enabled'] = state != 'disabled'
                self.assertFalse(maintain_once(self.store, config, lambda *_: self.fail('Private or editing data sent')))
                self.store.disposition(row['id'], 'local')

    def test_manual_note_is_analyzed_without_overwriting_user_body(self):
        row = self.completed_capture()
        note = self.vault / row['note_path']
        note.write_text('我的手动修订：只有满足条件才重试', encoding='utf-8')
        before = note.read_bytes()
        def cloud(_, payload):
            self.assertIn('我的手动修订', payload['text'])
            return analysis()
        self.assertTrue(maintain_once(self.store, self.config(), cloud))
        self.assertEqual(note.read_bytes(), before)
        self.assertEqual(self.store.get(row['id'])['note_path'], row['note_path'])
        self.assertFalse(maintain_once(self.store, self.config(), lambda *_: self.fail('Unchanged manual note reprocessed')))

    def test_cancel_during_classification_prevents_file_writes(self):
        row = self.completed_capture()
        before = (self.vault / row['note_path']).read_bytes()
        def cloud(*_):
            self.store.disposition(row['id'], 'local')
            return analysis()
        self.assertFalse(maintain_once(self.store, self.config(), cloud))
        self.assertEqual((self.vault / row['note_path']).read_bytes(), before)
        self.assertFalse((self.vault / '09 Archive').exists())

    def test_incomplete_capture_keeps_source_url_and_pending_state(self):
        row = self.completed_capture()
        result = json.loads(row['result'])
        result['source_url'] = 'https://example.com/missing-content'
        self.store.update(row['id'], result=json.dumps(result))
        self.assertTrue(maintain_once(self.store, self.config(), lambda *_: dict(analysis(), sufficient=False)))
        row = self.store.get(row['id'])
        self.assertEqual(row['state'], 'source_only')
        self.assertIn('00 Inbox收件箱/待补充/', row['note_path'])
        self.assertEqual(json.loads(row['result'])['source_url'], result['source_url'])

    def test_backlinks_do_not_cause_repeated_topic_generation(self):
        for text in ['一份原文', '另一份原文']:
            self.completed_capture(text)
        calls = []
        def cloud(_, payload):
            calls.append(payload['mode'])
            return analysis() if payload['mode'] == 'classify' else {'body': '条件相近。[S1][S2]'}
        for _ in range(4):
            maintain_once(self.store, self.config(), cloud)
        self.assertEqual(calls.count('classify'), 2)
        self.assertEqual(calls.count('synthesize'), 1)
        for entry in Organizer(self.store, self.vault).ledger['notes'].values():
            self.assertIn('[[04 Knowledge/AI专题/', (self.vault / entry['path']).read_text(encoding='utf-8'))

    def test_topic_citations_are_short_and_idle_pass_does_not_touch_files(self):
        for text in ['原文一', '原文二']:
            row, _ = self.store.capture(text=text)
            self.agent.apply(row, analysis())
        self.agent.update_topics({}, cloud=lambda *_: {'body': '# 重复标题\n## 条件\nS1 的条件明确。[S1][S2]'})
        topic = self.vault / '04 Knowledge/AI专题/Spring与服务调用.md'
        text = topic.read_text(encoding='utf-8')
        self.assertEqual(text.count('\n# '), 1)
        self.assertIn('|〔1〕]]', text)
        self.assertNotIn('S1', text)
        paths = [topic, self.agent.path, self.vault / '10 Knowledge Hub/Agent导航.md']
        before = [p.stat().st_mtime_ns for p in paths]
        self.agent.update_topics({}, cloud=lambda *_: self.fail('Idle synthesis'))
        self.assertEqual(before, [p.stat().st_mtime_ns for p in paths])

    def test_url_note_retains_extracted_page_body_and_review_survives_updates(self):
        row, _ = self.store.capture(text='https://example.com/article')
        self.store.update(row['id'], result=json.dumps(dict(analysis(), original='网页中提取的完整正文', status='processed')))
        note = write_note(self.store, self.store.get(row['id']), self.vault)
        self.assertIn('网页中提取的完整正文', note.read_text(encoding='utf-8'))
        row = self.store.get(row['id'])
        entry = self.agent.apply(row, dict(analysis(), review_notes=['已核对官方文档，保留版本前提']))
        self.assertIn('校验补充', (self.vault / entry['path']).read_text(encoding='utf-8'))
        entry = self.agent.apply(self.store.get(row['id']), analysis())
        self.assertEqual(entry['review_notes'], ['已核对官方文档，保留版本前提'])

    def test_unknown_topic_citation_falls_back_to_source_list(self):
        for text in ['甲', '乙']:
            row, _ = self.store.capture(text=text)
            self.agent.apply(row, analysis())
        self.agent.update_topics({}, cloud=lambda *_: {'body': '没有提供的结论。[S9]'})
        topic = self.vault / '04 Knowledge/AI专题/Spring与服务调用.md'
        text = topic.read_text(encoding='utf-8')
        self.assertNotIn('没有提供的结论', text)
        self.assertIn('资料速览', text)

    def test_semantic_edges_are_symmetric_and_unknown_nodes_are_rejected(self):
        for text in ['来源甲', '来源乙', '来源丙']:
            row, _ = self.store.capture(text=text)
            self.agent.apply(row, analysis())
        self.agent.update_relations({}, cloud=lambda *_: {'edges': [{'a': 'N1', 'b': 'N2', 'reason': '同一重试机制的条件与边界'}]})
        self.agent.connect_topics()
        entries = list(self.agent.ledger['notes'].values())
        self.assertEqual(entries[0]['related_notes'][0]['path'], entries[1]['path'])
        self.assertEqual(entries[1]['related_notes'][0]['path'], entries[0]['path'])
        self.assertEqual(entries[2]['related_notes'], [])
        for entry in entries[:2]:
            self.assertIn('相关资料', (self.vault / entry['path']).read_text(encoding='utf-8'))
        self.agent.ledger.pop('relations')
        self.agent.update_relations({}, cloud=lambda *_: {'edges': [{'a': 'N1', 'b': 'FAKE', 'reason': '不存在'}]})
        self.assertFalse(self.agent.ledger['relations']['done'])
        self.assertEqual(entries[0]['related_notes'][0]['path'], entries[1]['path'])

    def test_readable_names_handle_collisions_without_overwriting(self):
        paths = []
        for text in ['第一份材料', '第二份材料']:
            row, _ = self.store.capture(text=text)
            entry = self.agent.apply(row, analysis())
            paths.append(entry['path'])
            self.assertNotIn(row['id'], entry['path'])
            self.assertNotIn(row['digest'][:8], entry['path'])
        self.assertNotEqual(*paths)
        self.assertTrue(all((self.vault / path).exists() for path in paths))

    def test_pending_material_does_not_link_to_completed_knowledge_topic(self):
        for text in ['已读来源一', '已读来源二']:
            row, _ = self.store.capture(text=text)
            self.agent.apply(row, analysis())
        row, _ = self.store.capture(text='只有标题')
        pending = self.agent.apply(row, dict(analysis(), sufficient=False))
        self.agent.update_topics({}, cloud=lambda *_: {'body': '已有资料的条件。[S1][S2]'})
        self.assertNotIn('## 所属专题', (self.vault / pending['path']).read_text(encoding='utf-8'))

    def test_unchanged_reclassification_keeps_cached_relationships(self):
        for text in ['已整理甲', '已整理乙']:
            row, _ = self.store.capture(text=text)
            self.agent.apply(row, analysis())
        self.agent.update_relations({}, cloud=lambda *_: {'edges': [{'a': 'N1', 'b': 'N2', 'reason': '条件互补'}]})
        first = next(iter(self.agent.ledger['notes'].values()))
        self.agent.apply(self.store.get(first['id']), analysis())
        self.agent.update_relations({}, cloud=lambda *_: self.fail('Unchanged input sent again'))
        self.assertEqual(len(self.agent.ledger['notes'][self.store.get(first['id'])['digest']]['related_notes']), 1)

    def test_light_index_reuses_classification_without_extra_model_call(self):
        row,_=self.store.capture(text='应保留在原件中的文字')
        value=dict(analysis(),status='processed',original='',index_only=True,processing_mode='index',metadata_version=0,
                   purpose='用于查找重试条件',coverage={'label':'仅预览第1页片段'})
        self.store.update(row['id'],result=json.dumps(value,ensure_ascii=False),sent=1)
        path=write_note(self.store,self.store.get(row['id']),self.vault)
        self.assertTrue(maintain_once(self.store,self.config(),lambda *_:self.fail('Repeated index classification')))
        row=self.store.get(row['id']);text=(self.vault/row['note_path']).read_text(encoding='utf-8')
        self.assertEqual((self.vault/row['note_path']).resolve(),path.resolve())
        self.assertIn('仅预览第1页片段',text)
        self.assertIn('资料用途',text)
        self.assertNotIn('## 提取正文',text)
        self.assertNotIn('应保留在原件中的文字',text)
        self.assertEqual(Path(row['path']).read_text(encoding='utf-8'),'应保留在原件中的文字')
        self.assertEqual(self.store.rows('用于查找重试条件')[0]['id'],row['id'])
        self.assertFalse((self.vault/'09 Archive').exists())

    def test_reprocessed_result_keeps_prior_verified_context(self):
        row,_=self.store.capture(text='原始资料')
        entry=self.agent.apply(row,dict(analysis(),review_notes=['已核对官方文档，保留版本前提']))
        value=dict(analysis(),status='processed',index_only=True,metadata_version=0,original='',purpose='资料用途')
        self.store.update(row['id'],result=json.dumps(value),state='done',sent=1)
        def cloud(_,payload):
            self.assertEqual(payload['mode'],'classify')
            self.assertIn('已核对官方文档',payload['remarks'])
            return analysis()
        self.assertTrue(maintain_once(self.store,self.config(),cloud))
        value=json.loads(self.store.get(row['id'])['result'])
        self.assertEqual(value['review_notes'],['已核对官方文档，保留版本前提'])
