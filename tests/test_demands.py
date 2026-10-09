"""Demand ledger boundaries using only isolated temporary private data."""
from concurrent.futures import ThreadPoolExecutor
import hashlib
import json
from pathlib import Path
import tempfile
import unittest

from app import demands, execplan_progress


def source(event='voice-1', **extra):
    return {'kind': 'voice', 'ref': '/private/raw.json', 'sha256': 'a' * 64,
            'event_id': event, 'delivery_sha256': 'b' * 64} | extra


class DemandsTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.path = self.root / 'private' / 'demands.json'
        self.propose = {'op': 'propose', 'id': 'demand-1', 'title': '实验耗材', 'outcome': '明确采购需求',
                        'kind': 'project', 'target_project': {'id': 'lab', 'path': str(self.root / 'lab')},
                        'constraints': ['先确认品类'], 'allowlist': ['docs/plan.md'],
                        'implementation': {'change': '更新实验耗材规划', 'acceptance': ['测试通过'],
                                           'stop_conditions': ['来源不可核验则停止'],
                                           'plan_path': str(self.root / 'plan.md')},
                        'sources': [source()]}

    def request(self, *operations, revision=None):
        return {'expected_revision': revision if revision is not None else demands.read(self.path)['revision'],
                'reason': 'isolated test', 'source_ref': 'test-case', 'operations': list(operations)}

    def apply(self, *operations, revision=None):
        return demands.apply(self.path, self.request(*operations, revision=revision))

    def evidence(self, content=b'Human approval in this isolated test'):
        path = self.root / 'user-message.txt'
        path.write_bytes(content)
        return {'kind': 'user_message', 'ref': str(path), 'sha256': hashlib.sha256(content).hexdigest()}

    def approve(self):
        current = demands.read(self.path)['demands']['demand-1']
        if current['analysis'] is None:
            self.apply({'op': 'assess', 'id': 'demand-1',
                        'analysis': {'capability': '已有记录', 'gap': '缺需求闭环', 'options': ['升级'],
                                     'recommendation': '更新项目计划', 'disposition': 'needs_upgrade',
                                     'evidence': [source('analysis-1', kind='analysis')]}})
            current = demands.read(self.path)['demands']['demand-1']
        op = {'op': 'decide', 'id': 'demand-1', 'decision': 'approved',
              'proposal_sha256': current['proposal_sha256'], 'evidence': self.evidence()}
        self.apply(op)
        return op

    def archivable_execution(self):
        project = self.root / 'lab'
        active = project / 'docs/exec-plans/active'
        archive = project / 'docs/exec-plans/archive'
        active.mkdir(parents=True)
        archive.mkdir(parents=True)
        original = active / 'plan.md'
        proposal = self.propose | {'implementation': self.propose['implementation'] |
                                  {'plan_path': str(original)}}
        demands.initialize(self.path)
        self.apply(proposal)
        self.approve()
        handoff = demands.prepare_handoff(self.path, 'demand-1')
        record = {'schema': 'execplan-progress/v1', 'plan_id': 'plan-1', 'project_id': 'lab',
                  'title': 'Archive test', 'objective': 'Keep identity after move', 'execution_status': 'closed',
                  'assessment': {'outcome': 'met', 'summary': 'Source reports complete', 'evidence': ['plan-report']},
                  'milestones': [], 'next_action': 'Archive', 'updated_at': '2026-10-08T12:00:00+08:00',
                  'published_sha256': ''}
        original.write_text(execplan_progress.publication('## Progress\n' + execplan_progress.block(record)))
        self.apply({'op': 'link_execution', 'id': 'demand-1', 'handoff_id': handoff['handoff_id'],
                    'thread_id': 'real-thread', 'goal_id': None, 'goal_status': 'not_created',
                    'plan_path': str(original), 'plan_id': 'plan-1'})
        demands.sync(self.path)
        return original, archive / 'renamed.md', record

    def test_explicit_init_missing_read_and_reinitialization_protection(self):
        with self.assertRaises(demands.DemandError):
            demands.read(self.path)
        self.assertFalse(self.path.exists())
        initialized = demands.initialize(self.path)
        self.assertEqual(initialized['revision'], 1)
        with self.assertRaises(demands.DemandError):
            demands.initialize(self.path)
        self.path.unlink()
        with self.assertRaises(demands.DemandError):
            demands.read(self.path)
        with self.assertRaises(demands.DemandError):
            demands.initialize(self.path)

    def test_propose_retry_conflict_and_same_event_correction(self):
        demands.initialize(self.path)
        multi = self.propose | {'sources': [source(), source(ref='/private/raw-2.json', sha256='c' * 64,
                                                          recording_id=None)]}
        self.assertTrue(self.apply(multi)['saved'])
        self.assertFalse(self.apply(multi)['saved'])
        altered = self.propose | {'title': '替换内容'}
        with self.assertRaises(demands.DemandError):
            self.apply(altered)
        with self.assertRaises(demands.DemandError):
            self.apply({'op': 'append_sources', 'id': 'demand-1', 'sources': [source(ref='/changed')]})
        self.apply({'op': 'append_sources', 'id': 'demand-1', 'sources': [source('voice-2', ref='/corrected')]})
        self.assertEqual(len(demands.read(self.path)['demands']['demand-1']['sources']), 3)
        with self.assertRaises(demands.DemandError):
            self.apply(self.propose | {'id': 'demand-2', 'sources': [source('voice-2', ref='/corrected')]})

    def test_verified_correction_keeps_history_and_invalidates_approval(self):
        demands.initialize(self.path)
        self.apply(self.propose)
        approval = self.approve()
        correction = {'op': 'revise', 'id': 'demand-1', 'changes': {'outcome': '修订采购范围'},
                      'sources': [source('voice-2', ref='/corrected')],
                      'supersedes_event_id': 'voice-1', 'correction_evidence': self.evidence()}
        self.apply(correction)
        demand = demands.read(self.path)['demands']['demand-1']
        self.assertIsNone(demand['decision'])
        self.assertEqual(demand['history'][0]['decision']['evidence'], approval['evidence'])
        self.assertEqual(len(demand['sources']), 2)
        self.assertFalse(self.apply(self.propose)['saved'])  # old event replay
        with self.assertRaises(demands.DemandError):
            demands.prepare_handoff(self.path, 'demand-1')
        with self.assertRaises(demands.DemandError):
            self.apply(approval)  # prior version cannot reapprove

    def test_acceptance_change_invalidates_old_approval(self):
        demands.initialize(self.path)
        self.apply(self.propose)
        self.approve()
        self.apply({'op': 'feedback', 'id': 'demand-1', 'dimension': 'technical', 'outcome': 'met',
                    'summary': '旧技术测试', 'evidence': [source('old-technical')]})
        before = demands.read(self.path)['demands']['demand-1']['proposal_sha256']
        changed_implementation = self.propose['implementation'] | {'acceptance': ['用户验收新范围']}
        self.apply({'op': 'revise_proposal', 'id': 'demand-1',
                    'changes': {'implementation': changed_implementation}})
        demand = demands.read(self.path)['demands']['demand-1']
        self.assertNotEqual(before, demand['proposal_sha256'])
        self.assertIsNone(demand['decision'])
        self.assertIsNone(demand['analysis'])
        snapshot = demand['history'][-1]
        self.assertEqual(snapshot['type'], 'proposal_revised')
        self.assertEqual(snapshot['feedback']['technical']['summary'], '旧技术测试')
        self.assertIn('closure', snapshot)
        with self.assertRaises(demands.DemandError):
            demands.prepare_handoff(self.path, 'demand-1')

    def test_replaced_analysis_decision_feedback_remain_in_history_and_retries_do_not_append(self):
        demands.initialize(self.path)
        self.apply(self.propose)
        old_analysis = {'capability': '已有能力', 'gap': '尚有缺口', 'options': ['升级'],
                        'recommendation': '先升级', 'disposition': 'needs_upgrade',
                        'evidence': [source('analysis-1')]}
        self.apply({'op': 'assess', 'id': 'demand-1', 'analysis': old_analysis})
        new_analysis = old_analysis | {'recommendation': '缩小升级范围'}
        self.apply({'op': 'assess', 'id': 'demand-1', 'analysis': new_analysis})
        demand = demands.read(self.path)['demands']['demand-1']
        self.assertEqual(demand['history'][-1]['analysis'], old_analysis)
        first_evidence = self.evidence()
        old_decision = {'op': 'decide', 'id': 'demand-1', 'decision': 'approved',
                        'proposal_sha256': demand['proposal_sha256'], 'evidence': first_evidence}
        self.apply(old_decision)
        second_path = self.root / 'second-user-message.txt'
        second_path.write_bytes(b'Human deferred this proposal')
        second_evidence = {'kind': 'user_message', 'ref': str(second_path),
                           'sha256': hashlib.sha256(second_path.read_bytes()).hexdigest()}
        new_decision = old_decision | {'decision': 'deferred', 'evidence': second_evidence}
        self.apply(new_decision)
        old_feedback = {'op': 'feedback', 'id': 'demand-1', 'dimension': 'technical',
                        'outcome': 'met', 'summary': '第一次测试', 'evidence': [source('test-1')]}
        self.apply(old_feedback)
        corrected_feedback = old_feedback | {'outcome': 'not_met', 'summary': '复查发现未通过',
                                             'evidence': [source('test-2')]}
        self.apply(corrected_feedback)
        demand = demands.read(self.path)['demands']['demand-1']
        entries = demand['history']
        self.assertEqual([item['type'] for item in entries],
                         ['analysis_replaced', 'decision_replaced', 'feedback_replaced'])
        self.assertEqual(entries[1]['decision']['evidence'], first_evidence)
        self.assertEqual(entries[2]['feedback']['summary'], '第一次测试')
        original_history_length = len(entries)
        self.assertFalse(self.apply({'op': 'assess', 'id': 'demand-1', 'analysis': new_analysis})['saved'])
        self.assertFalse(self.apply(new_decision)['saved'])
        self.assertFalse(self.apply(corrected_feedback)['saved'])
        self.assertEqual(len(demands.read(self.path)['demands']['demand-1']['history']), original_history_length)

    def test_use_existing_closes_with_independent_evidence_without_approval(self):
        demands.initialize(self.path)
        existing = self.propose | {'implementation': None, 'allowlist': []}
        self.apply(existing)
        self.apply({'op': 'assess', 'id': 'demand-1',
                    'analysis': {'capability': '功能已存在', 'gap': '用户尚未试用',
                                 'options': ['直接使用'], 'recommendation': '直接使用现有功能',
                                 'disposition': 'use_existing', 'evidence': [source('analysis-1')]}})
        with self.assertRaises(demands.DemandError):
            demands.prepare_handoff(self.path, 'demand-1')
        self.apply({'op': 'feedback', 'id': 'demand-1', 'dimension': 'technical', 'outcome': 'met',
                    'summary': '现有功能通过检查', 'evidence': [source('tech-1')]})
        with self.assertRaises(demands.DemandError):
            self.apply({'op': 'close', 'id': 'demand-1', 'resolution': 'completed'})
        self.apply({'op': 'feedback', 'id': 'demand-1', 'dimension': 'usage', 'outcome': 'met',
                    'summary': '实际使用达到目标', 'evidence': [source('usage-1')]})
        self.apply({'op': 'close', 'id': 'demand-1', 'resolution': 'completed'})
        demand = demands.read(self.path)['demands']['demand-1']
        self.assertIsNone(demand['decision'])
        self.assertIsNone(demand['handoff'])
        self.assertEqual(demand['closure']['resolution'], 'completed')

    def test_approval_hash_handoff_and_no_dispatch(self):
        demands.initialize(self.path)
        self.apply(self.propose)
        with self.assertRaises(demands.DemandError):
            demands.prepare_handoff(self.path, 'demand-1')
        approval = self.approve()
        prepared = demands.prepare_handoff(self.path, 'demand-1')
        self.assertTrue(prepared['saved'])
        self.assertEqual(demands.prepare_handoff(self.path, 'demand-1')['handoff_id'], prepared['handoff_id'])
        artifact = json.loads(Path(prepared['path']).read_text())
        self.assertEqual(artifact['allowlist'], ['docs/plan.md'])
        self.assertEqual(artifact['target_project']['id'], 'lab')
        self.assertEqual(artifact['implementation']['acceptance'], ['测试通过'])
        self.assertNotIn('command', artifact)
        self.assertTrue(demands.check(self.path)['valid'])
        Path(approval['evidence']['ref']).write_text('tampered')
        with self.assertRaises(demands.DemandError):
            demands.prepare_handoff(self.path, 'demand-1')
        with self.assertRaises(demands.DemandError):
            demands.check(self.path)

    def test_independent_feedback_required_to_close(self):
        demands.initialize(self.path)
        self.apply(self.propose)
        self.approve()
        with self.assertRaises(demands.DemandError):
            self.apply({'op': 'close', 'id': 'demand-1', 'resolution': 'completed'})
        technical = {'op': 'feedback', 'id': 'demand-1', 'dimension': 'technical', 'outcome': 'met',
                     'summary': 'Focused checks passed', 'evidence': [source('test-1', kind='test')]}
        usage = {'op': 'feedback', 'id': 'demand-1', 'dimension': 'usage', 'outcome': 'met',
                 'summary': 'User confirmed result useful', 'evidence': [source('user-2', kind='user_message')]}
        self.apply(technical)
        with self.assertRaises(demands.DemandError):
            self.apply({'op': 'close', 'id': 'demand-1', 'resolution': 'completed'})
        self.apply(usage)
        self.apply({'op': 'close', 'id': 'demand-1', 'resolution': 'completed'})
        self.assertEqual(demands.read(self.path)['demands']['demand-1']['closure']['resolution'], 'completed')
        with self.assertRaises(demands.DemandError):
            self.apply(usage | {'outcome': 'not_met'})

    def test_sync_exact_published_plan_and_stale_preserves_last_observation(self):
        demands.initialize(self.path)
        self.apply(self.propose)
        self.approve()
        handoff = demands.prepare_handoff(self.path, 'demand-1')
        plan = self.root / 'plan.md'
        record = {'schema': 'execplan-progress/v1', 'plan_id': 'plan-1', 'project_id': 'lab',
                  'title': 'Test plan', 'objective': 'Implement', 'execution_status': 'active',
                  'assessment': {'outcome': 'unassessed', 'summary': 'Pending', 'evidence': []},
                  'milestones': [], 'next_action': 'Run', 'updated_at': '2026-10-08T12:00:00+08:00',
                  'published_sha256': ''}
        plan.write_text(execplan_progress.publication('## Progress\n' + execplan_progress.block(record)))
        link = {'op': 'link_execution', 'id': 'demand-1', 'handoff_id': handoff['handoff_id'],
                'thread_id': 'real-thread', 'goal_id': None, 'goal_status': 'not_created',
                'plan_path': str(plan), 'plan_id': 'plan-1'}
        self.apply(link)
        result = demands.sync(self.path)
        self.assertTrue(result['saved'])
        first = demands.read(self.path)['demands']['demand-1']['execution']['observation']
        self.assertEqual(first['freshness'], 'fresh')
        self.assertFalse(demands.sync(self.path)['saved'])
        record['execution_status'] = 'closed'
        record['assessment'] = {'outcome': 'met', 'summary': 'Plan report complete', 'evidence': ['plan-test']}
        plan.write_text(execplan_progress.publication('## Progress\n' + execplan_progress.block(record)))
        demands.sync(self.path)
        self.assertIsNone(demands.read(self.path)['demands']['demand-1']['closure'])
        closed_observation = demands.read(self.path)['demands']['demand-1']['execution']['observation']
        plan.unlink()
        demands.sync(self.path)
        stale = demands.read(self.path)['demands']['demand-1']['execution']['observation']
        self.assertEqual(stale['freshness'], 'stale')
        self.assertEqual(stale['raw_sha256'], closed_observation['raw_sha256'])
        self.assertEqual(stale['execution_status'], 'closed')

    def test_archive_move_is_fresh_without_changing_approved_or_linked_path(self):
        original, moved, _ = self.archivable_execution()
        before = demands.read(self.path)['demands']['demand-1']
        old_digest = before['proposal_sha256']
        original.rename(moved)
        result = demands.sync(self.path)
        self.assertTrue(result['saved'])
        demand = demands.read(self.path)['demands']['demand-1']
        self.assertEqual(demand['proposal_sha256'], old_digest)
        self.assertEqual(demand['proposal']['implementation']['plan_path'], str(original))
        self.assertEqual(demand['execution']['plan_path'], str(original))
        self.assertEqual(demand['execution']['observation']['source_path'], str(moved))
        self.assertEqual(demand['execution']['observation']['freshness'], 'fresh')
        self.assertIsNone(demand['closure'])
        self.assertFalse(demands.sync(self.path)['saved'])

    def test_archive_duplicate_wrong_project_and_offline_keep_last_reliable(self):
        original, moved, record = self.archivable_execution()
        original.rename(moved)
        demands.sync(self.path)
        reliable = demands.read(self.path)['demands']['demand-1']['execution']['observation']
        duplicate = moved.parent.parent / 'active' / 'duplicate.md'
        duplicate.write_bytes(moved.read_bytes())
        demands.sync(self.path)
        observation = demands.read(self.path)['demands']['demand-1']['execution']['observation']
        self.assertEqual(observation['freshness'], 'stale')
        self.assertEqual(observation['raw_sha256'], reliable['raw_sha256'])
        duplicate.unlink()
        record['project_id'] = 'wrong-project'
        moved.write_text(execplan_progress.publication('## Progress\n' + execplan_progress.block(record)))
        demands.sync(self.path)
        observation = demands.read(self.path)['demands']['demand-1']['execution']['observation']
        self.assertEqual(observation['freshness'], 'stale')
        self.assertEqual(observation['raw_sha256'], reliable['raw_sha256'])
        record['project_id'] = 'lab'
        moved.write_text(execplan_progress.publication('## Progress\n' + execplan_progress.block(record)) + '\nunpublished edit\n')
        demands.sync(self.path)
        observation = demands.read(self.path)['demands']['demand-1']['execution']['observation']
        self.assertEqual(observation['freshness'], 'stale')
        self.assertEqual(observation['raw_sha256'], reliable['raw_sha256'])
        moved.unlink()
        demands.sync(self.path)
        observation = demands.read(self.path)['demands']['demand-1']['execution']['observation']
        self.assertEqual(observation['freshness'], 'stale')
        self.assertEqual(observation['raw_sha256'], reliable['raw_sha256'])
        self.assertEqual(observation['source_path'], str(moved))

    def test_offline_voice_source_does_not_erase_recorded_provenance(self):
        demands.initialize(self.path)
        self.apply(self.propose)
        self.assertFalse(Path(self.propose['sources'][0]['ref']).exists())
        self.assertEqual(demands.read(self.path)['demands']['demand-1']['sources'], self.propose['sources'])
        self.assertTrue(demands.check(self.path)['valid'])

    def test_revise_during_execution_retains_old_link_and_requires_closed_observation(self):
        demands.initialize(self.path)
        self.apply(self.propose)
        self.approve()
        old_handoff = demands.prepare_handoff(self.path, 'demand-1')
        plan = self.root / 'plan.md'
        record = {'schema': 'execplan-progress/v1', 'plan_id': 'old-plan', 'project_id': 'lab',
                  'title': 'Old scope', 'objective': 'Implement old scope', 'execution_status': 'active',
                  'assessment': {'outcome': 'unassessed', 'summary': 'Pending', 'evidence': []},
                  'milestones': [], 'next_action': 'Run', 'updated_at': '2026-10-08T12:00:00+08:00',
                  'published_sha256': ''}
        plan.write_text(execplan_progress.publication('## Progress\n' + execplan_progress.block(record)))
        self.apply({'op': 'link_execution', 'id': 'demand-1', 'handoff_id': old_handoff['handoff_id'],
                    'thread_id': 'real-old-thread', 'goal_id': None, 'goal_status': 'not_created',
                    'plan_path': str(plan), 'plan_id': 'old-plan'})
        demands.sync(self.path)
        self.apply({'op': 'revise_proposal', 'id': 'demand-1', 'changes': {'outcome': 'New scope'}})
        changed = demands.read(self.path)['demands']['demand-1']
        self.assertTrue(changed['authorization_stale'])
        self.assertIsNone(changed['decision'])
        self.assertEqual(changed['execution']['thread_id'], 'real-old-thread')
        self.assertEqual(changed['history'][-1]['execution']['thread_id'], 'real-old-thread')
        self.apply({'op': 'assess', 'id': 'demand-1',
                    'analysis': {'capability': '已有记录', 'gap': '新范围待做', 'options': ['升级'],
                                 'recommendation': '重建交接', 'disposition': 'needs_upgrade',
                                 'evidence': [source('analysis-2', kind='analysis')]}})
        self.apply({'op': 'decide', 'id': 'demand-1', 'decision': 'approved',
                    'proposal_sha256': changed['proposal_sha256'],
                    'evidence': self.evidence(b'Human approved revised scope')})
        with self.assertRaises(demands.DemandError):
            demands.prepare_handoff(self.path, 'demand-1')
        with self.assertRaises(demands.DemandError):
            self.apply({'op': 'close', 'id': 'demand-1', 'resolution': 'completed'})
        record['execution_status'] = 'closed'
        record['assessment'] = {'outcome': 'not_met', 'summary': 'Old scope stopped', 'evidence': []}
        plan.write_text(execplan_progress.publication('## Progress\n' + execplan_progress.block(record)))
        demands.sync(self.path)
        new_handoff = demands.prepare_handoff(self.path, 'demand-1')
        self.assertNotEqual(new_handoff['handoff_id'], old_handoff['handoff_id'])
        current = demands.read(self.path)['demands']['demand-1']
        self.assertFalse(current['authorization_stale'])
        self.assertIsNone(current['execution'])
        self.assertIsNone(current['closure'])
        self.assertEqual(current['history'][-1]['type'], 'execution_reconciled')
        self.assertEqual(current['history'][-1]['execution']['observation']['execution_status'], 'closed')
        self.assertEqual(current['history'][-2]['execution']['observation']['execution_status'], 'active')

    def test_revision_cas_concurrent_and_explicit_recovery(self):
        demands.initialize(self.path)
        expected = demands.read(self.path)['revision']
        with ThreadPoolExecutor(max_workers=2) as pool:
            operations = [self.propose, self.propose | {'id': 'demand-2', 'sources': [source('voice-2')]}]
            outcomes = []
            futures = [pool.submit(self.apply, op, revision=expected) for op in operations]
            for future in futures:
                try:
                    outcomes.append(future.result())
                except demands.DemandError:
                    outcomes.append(None)
        self.assertEqual(sum(item is not None for item in outcomes), 1)
        winner = next(iter(demands.read(self.path)['demands']))
        self.apply({'op': 'append_sources', 'id': winner, 'sources': [source('voice-3')]})
        self.path.write_bytes(b'{corrupt')
        with self.assertRaises(demands.DemandError):
            demands.read(self.path)
        recovered = demands.recover(self.path)
        self.assertGreaterEqual(recovered['revision'], 2)
        self.assertTrue(list(self.path.parent.glob('demands.json.pre-recover-*')))
        with self.assertRaises(demands.DemandError):
            demands.recover(self.path)


if __name__ == '__main__':
    unittest.main()
