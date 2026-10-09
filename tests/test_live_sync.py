import copy
import hashlib
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from app.execplan_progress import block, inspect, parse, publication, publish, ProgressError
from app.live_sync import SourceSync
from app.reader import ReadingViews
from app.store import WorkspaceStore
from app.tracking import empty_tracking, load_tracking
from test_graphs import CONTEXT
from test_reader import SVG


def record(pid='plan-one', gid='goal-one'):
    return {'schema': 'execplan-progress/v1', 'plan_id': pid, 'goal_id': gid, 'project_id': 'project',
            'title': 'A plan', 'objective': 'Observable result', 'execution_status': 'active',
            'assessment': {'outcome': 'insufficient_evidence', 'summary': 'User trial pending', 'evidence': []},
            'milestones': [{'id': 'm1', 'title': 'Implementation', 'state': 'done', 'depends_on': [], 'evidence': ['tests passed']}],
            'next_action': 'User trial', 'updated_at': '2026-09-28T00:00:00Z', 'published_sha256': ''}


def document(value=None):
    return publication('# Plan\n\n## Progress\n\n' + block(value or record()) + '\n\n## Outcomes & Retrospective\n\nPending user feedback.\n')


class ProgressTests(unittest.TestCase):
    def test_publication_covers_prose_summary_and_json_and_is_idempotent(self):
        text = document()
        self.assertEqual(inspect(text)['status'], 'synced')
        self.assertEqual(publication(text), text)
        for before, after in [('Pending user feedback.', 'Changed scope.'), ('[完成]', '[未知]'), ('User trial', 'New trial')]:
            self.assertEqual(inspect(text.replace(before, after))['status'], 'pending')
        self.assertEqual(parse(text)['assessment']['outcome'], 'insufficient_evidence')
        self.assertEqual(inspect('## Progress\n- [x] old plan')['status'], 'legacy')

    def test_invalid_identity_dependencies_evidence_and_version(self):
        for mutate in [lambda r: r.update(schema='future'),
                       lambda r: r['milestones'].append(copy.deepcopy(r['milestones'][0])),
                       lambda r: r['milestones'][0].update(depends_on=['missing']),
                       lambda r: r['milestones'][0].update(depends_on=['m1']),
                       lambda r: r['assessment'].update(outcome='met')]:
            r = record(); mutate(r)
            with self.assertRaises(ProgressError): document(r)
        r = record(); r['milestones'].append(dict(r['milestones'][0], id='m2', depends_on=['m1'])); r['milestones'][0]['depends_on'] = ['m2']
        with self.assertRaises(ProgressError): document(r)

    def test_readonly_check_and_optimistic_atomic_publish(self):
        with tempfile.TemporaryDirectory() as tmp:
            p = Path(tmp) / 'plan.md'; p.write_text(document())
            before = p.read_bytes(); inspect(before.decode()); self.assertEqual(before, p.read_bytes())
            p.write_text(p.read_text() + '\nnew discovery\n')
            with self.assertRaises(ProgressError): publish(p, hashlib.sha256(before).hexdigest())
            self.assertIn('new discovery', p.read_text())
            self.assertEqual(publish(p)['status'], 'synced')
            r = parse(p.read_text()); r['execution_status'] = 'closed'
            self.assertEqual(parse(document(r))['assessment']['outcome'], 'insufficient_evidence')


class SyncTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(); self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name); self.active = self.root / 'external/active'; self.archive = self.root / 'external/archive'
        self.active.mkdir(parents=True); self.archive.mkdir()
        self.plan = self.active / 'one.md'; self.plan.write_text(document())
        self.path = self.root / 'tracking.json'
        t = empty_tracking(); t.update(schema_version=2, observed_at='2026-09-28T00:00:00Z', sync_status='ok', source_thread={'id':'test', 'url':'codex://threads/test', 'title':'source workflow', 'status':'observed'})
        self.path.write_text(json.dumps(t))
        self.registry = self.root / 'sources.json'; self.registry.write_text(json.dumps({'schema_version':1,'projects':[{'id':'project','title':'Project','root':str(self.root/'external'),'plan_dirs':['active','archive']}]}))
        self.sync = SourceSync(self.registry, self.path)

    def refresh(self):
        return self.sync.refresh(load_tracking(self.path))

    def test_discovery_noop_missing_publication_and_relocation(self):
        first, states = self.refresh(); self.assertEqual(states[0]['status'], 'synced')
        self.assertEqual(first['goals'][0]['assessment'], '证据不足：User trial pending')
        again, _ = self.refresh(); self.assertEqual(first, again)
        self.plan.write_text(self.plan.read_text().replace('Observable result','Revised result'))
        unchanged, states = self.refresh(); self.assertEqual(unchanged, first); self.assertEqual(states[0]['status'], 'pending')
        publish(self.plan); fresh, _ = self.refresh(); self.assertEqual(fresh['goals'][0]['target_state'], 'Revised result')
        moved = self.archive / 'renamed.md'; self.plan.rename(moved)
        archived, _ = self.refresh(); self.assertEqual(archived['goals'][0]['id'], 'goal-one'); self.assertEqual(archived['goals'][0]['stage_status'], 'active')
        self.assertEqual(archived['goals'][0]['plan_ref'], str(moved))

    def test_duplicate_missing_invalid_isolated_and_no_direction_inference(self):
        old, _ = self.refresh()
        duplicate = self.archive / 'duplicate.md'; duplicate.write_bytes(self.plan.read_bytes())
        (self.active/'two.md').write_text(document(record('plan-two','goal-two')))
        current, states = self.refresh(); self.assertEqual(len(current['goals']), 2)
        self.assertEqual(next(s for s in states if s['plan_id']=='plan-one')['status'],'pending')
        self.assertEqual(current['relations'], [])
        duplicate.unlink(); self.plan.write_text('## Progress\n{broken')
        current, states = self.refresh(); self.assertEqual(current['goals'][0],old['goals'][0]); self.assertEqual(states[0]['status'],'pending')
        self.plan.unlink(); _, states = self.refresh(); self.assertEqual(states[0]['status'],'unavailable')

    @patch('app.reader._render_svg', return_value=SVG)
    def test_incremental_render_failure_and_source_changes_during_render(self, renderer):
        with WorkspaceStore(self.root/'workspace.json') as store:
            (self.root/'context.json').write_text(json.dumps(CONTEXT))
            reader = ReadingViews(store, self.root/'context.json', self.root/'views')
            first = reader.snapshot(); self.assertFalse(first['stale']); self.assertEqual(renderer.call_count, 5)
            self.assertEqual(reader.snapshot()['bundle'], first['bundle']); self.assertEqual(renderer.call_count,5)
            r = record(); r['milestones'][0]['title'] = 'New implementation'; self.plan.write_text(document(r))
            renderer.side_effect = RuntimeError('renderer down')
            failed = reader.snapshot(); self.assertTrue(failed['stale']); self.assertEqual(failed['bundle'], first['bundle']); self.assertEqual(failed['sources'][0]['status'],'render_failed')
            renderer.side_effect = None; reader.retry_at=0
            fresh = reader.snapshot(); self.assertFalse(fresh['stale']); self.assertNotEqual(fresh['bundle']['fingerprint'],first['bundle']['fingerprint'])
            self.assertEqual(fresh['sources'][0]['status'],'synced')
            r['milestones'][0]['title'] = 'A third implementation'; self.plan.write_text(document(r))
            def racing(*args):
                self.plan.write_text(self.plan.read_text()+'\nchanged during render\n'); return SVG
            renderer.side_effect = racing
            raced = reader.snapshot(); self.assertTrue(raced['stale']); self.assertEqual(raced['bundle'],fresh['bundle']); self.assertEqual(raced['sources'][0]['status'],'checking')

    def test_invalid_projection_does_not_block_healthy_plan(self):
        old, _ = self.refresh()
        bad=record('plan-bad','goal-bad'); bad['milestones'][0]['evidence']=['a'*6000,'b'*6000]
        (self.active/'bad.md').write_text(document(bad))
        good=record();good['title']='Healthy changed title';self.plan.write_text(document(good))
        current, states=self.refresh()
        self.assertEqual(len(current['goals']),1)
        self.assertEqual(current['goals'][0]['title'],'Healthy changed title')
        self.assertEqual(next(s for s in states if s['plan_id']=='plan-bad')['status'],'pending')

    def test_symlink_outside_registry_not_read_and_closed_not_accepted(self):
        outside = self.root/'secret.md'; outside.write_text(document(record('private','private')))
        (self.active/'escape.md').symlink_to(outside)
        r=record(); r['execution_status']='closed'; self.plan.write_text(document(r))
        current, _ = self.refresh(); self.assertEqual(len(current['goals']),1); self.assertEqual(current['goals'][0]['stage_status'],'closed')
        self.assertIn('证据不足',current['goals'][0]['assessment'])


if __name__ == '__main__':
    unittest.main()
