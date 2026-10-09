"""Source binding, publication recovery and correction across the real receiver."""
import copy
import hashlib
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from app import demands, voice_intake


class VoiceDemandIntakeTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.data = self.root / 'private'
        demands.initialize(self.data / 'demands.json')
        self.raw = self.root / 'raw.json'
        self.raw.write_text(json.dumps({'status': 'success', 'source_sha256': 'a' * 64,
                                       'response': {'text': '阅读语言模型原理'}}))
        self.item = {'event_id': 'voice_test1', 'project_id': 'life-projects', 'operation': 'demand',
                     'quote': '阅读语言模型原理', 'nature': 'other_project', 'ai_draft': '学习语言模型',
                     'sources': [{'path': str(self.raw), 'sha256': voice_intake.sha(self.raw),
                                  'audio_sha256': 'a' * 64, 'audio_path': '/audio/one.m4a',
                                  'locator': '/response/text', 'raw_text': '阅读语言模型原理'}],
                     'demand': {'title': '学习语言模型', 'outcome': '理解语言模型如何阅读', 'kind': 'learning'}}

    def receive(self, item=None):
        return voice_intake.receive(self.data, [item or self.item])[0]

    def test_receipt_and_duplicate_rename_preserve_one_demand(self):
        first = self.receive()
        self.assertEqual(first['status'], 'accepted')
        manifest = self.data / first['receipt']['manifest']['path']
        self.assertEqual(voice_intake.sha(manifest), first['receipt']['manifest']['sha256'])
        renamed = copy.deepcopy(self.item)
        renamed['sources'][0]['audio_path'] = '/audio/renamed.m4a'
        self.assertEqual(self.receive(renamed)['status'], 'duplicate')
        self.assertEqual(len(demands.read(self.data / 'demands.json')['demands']), 1)
        changed = copy.deepcopy(self.item)
        changed['demand']['outcome'] = '另一件事'
        self.assertEqual(self.receive(changed)['status'], 'rejected')

    def test_raw_tampering_and_unconfirmed_source_rejected(self):
        self.raw.write_text('{}')
        self.assertIn('hash_mismatch', self.receive()['reason'])
        self.assertFalse(demands.read(self.data / 'demands.json')['demands'])

    def test_ledger_commit_crash_only_exact_retry_repairs_publication(self):
        with patch.object(voice_intake, 'immutable_json', side_effect=OSError('simulated disk failure')):
            self.assertEqual(self.receive()['status'], 'rejected')
        self.assertEqual(len(demands.read(self.data / 'demands.json')['demands']), 1)
        self.assertFalse((self.data / 'receipts').exists())
        changed = copy.deepcopy(self.item)
        changed['demand']['outcome'] = '被替换的内容'
        self.assertIn('changed_content_after_ledger_commit', self.receive(changed)['reason'])
        self.assertEqual(self.receive()['status'], 'accepted')
        self.assertEqual(self.receive()['status'], 'duplicate')

    def correction_item(self):
        item = copy.deepcopy(self.item)
        item['event_id'] = 'voice_correction'
        item['supersedes_event_id'] = self.item['event_id']
        item['demand']['outcome'] = '重点理解 GPT 如何用于阅读'
        evidence = self.root / 'correction.json'
        evidence.write_text(json.dumps({'kind': 'user_message', 'message_ref': 'isolated-test',
            'known_at': '2026-10-08', 'text': '重点理解 GPT 如何用于阅读',
            'supersedes_event_id': self.item['event_id'], 'demand_confirmation': item['demand']}))
        item['correction_evidence'] = {'path': str(evidence), 'sha256': voice_intake.sha(evidence)}
        return item

    def test_source_bound_correction_keeps_history(self):
        first = self.receive()
        correction = self.correction_item()
        result = self.receive(correction)
        self.assertEqual(result['status'], 'accepted', result)
        self.assertEqual(first['demand_id'], result['demand_id'])
        state = demands.read(self.data / 'demands.json')['demands'][first['demand_id']]
        self.assertEqual(len(state['sources']), 2)
        self.assertTrue(state['history'])
        self.assertIsNone(state['decision'])
        self.assertEqual(self.receive()['status'], 'duplicate')

    def test_correction_rejects_changed_prior_artifact(self):
        first = self.receive()
        old = self.data / first['receipt']['artifact']['path']
        artifact = json.loads(old.read_text())
        artifact['item']['demand']['outcome'] = '伪造旧内容'
        old.write_text(json.dumps(artifact))
        result = self.receive(self.correction_item())
        self.assertEqual(result['status'], 'rejected')
        self.assertIn('superseded_receipt', result['reason'])

    def test_missing_data_store_never_silently_initializes(self):
        missing = self.root / 'offline-volume'
        with self.assertRaises(ValueError):
            voice_intake.receive(missing, [self.item])
        self.assertFalse(missing.exists())


if __name__ == '__main__':
    unittest.main()
