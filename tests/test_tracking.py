import copy
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from app.tracking import TrackingError, empty_tracking, load_tracking, save_tracking, validate_tracking
from app.model import initial_workspace, normalize_and_validate, derived_view, ValidationError
from app.conversation import prepare
from app.reader import ReadingViews, fingerprint
from app.store import WorkspaceStore
from test_graphs import CONTEXT
from test_reader import SVG


def example():
    value = empty_tracking()
    value.update(sync_status='ok', sync_message='explicit read', observed_at='2026-09-26T18:00:00+08:00',
                 source_thread={'id':'thread-a','title':'Source','url':'codex://threads/thread-a','status':'active'},
                 projects=[{'id':'external','title':'External project'}],
                 goals=[{'id':'goal-c','project_id':'external','title':'C','target_state':'Observable result',
                         'plan_id':'plan-c','plan_ref':'old/C.md','progress':'Not complete','assessment':'Not independently assessed',
                         'gap':'Browser check','next_action':'Check UI',
                         'sources':[{'kind':'plan','ref':'old/C.md','title':'Plan C','observed_at':'2026-09-26T18:00:00+08:00','sha256':'a'*64}]}])
    return value


class TrackingTests(unittest.TestCase):
    def test_import_conflict_identity_move_and_failure_preserve_last_facts(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / 'tracking.json'
            self.assertEqual(load_tracking(path), empty_tracking())
            saved = save_tracking(path, 0, example())
            with self.assertRaisesRegex(TrackingError, '冲突'):
                save_tracking(path, 0, example())
            moved = copy.deepcopy(saved)
            moved['goals'][0]['plan_ref'] = 'new/C.md'
            updated = save_tracking(path, 1, moved)
            self.assertEqual(len(updated['goals']), 1)
            changed_id = copy.deepcopy(updated)
            changed_id['goals'][0]['id'] = 'another-c'
            with self.assertRaisesRegex(TrackingError, '保留目标 ID'):
                save_tracking(path, 2, changed_id)
            failed = save_tracking(path, 2, failure='Source unavailable')
            self.assertEqual(failed['goals'], updated['goals'])
            self.assertEqual(failed['observed_at'], updated['observed_at'])
            self.assertEqual(failed['sync_status'], 'failed')
            self.assertEqual(load_tracking(path), failed)

    def test_invalid_file_or_duplicate_plan_not_silently_empty(self):
        with tempfile.TemporaryDirectory() as folder:
            path=Path(folder)/'tracking.json'
            path.write_text('{invalid')
            with self.assertRaises(TrackingError): load_tracking(path)
        value=example()
        duplicate=copy.deepcopy(value['goals'][0]); duplicate['id']='another'
        value['goals'].append(duplicate)
        with self.assertRaises(TrackingError): validate_tracking(value)

    @patch('app.reader._render_svg', return_value=SVG)
    def test_tracking_participates_in_atomic_read_bundle_and_failure(self, renderer):
        with tempfile.TemporaryDirectory() as folder, WorkspaceStore(Path(folder)/'workspace.json') as store:
            root=Path(folder); context=root/'context.json'; context.write_text(json.dumps(CONTEXT))
            reader=ReadingViews(store,context,root/'views')
            first=reader.snapshot(); self.assertFalse(first['stale'])
            save_tracking(root/'tracking.json',0,example())
            second=reader.snapshot(); self.assertFalse(second['stale'])
            self.assertNotEqual(first['bundle']['fingerprint'],second['bundle']['fingerprint'])
            self.assertIn('外部计划',second['bundle']['sources']['context'])
            (root/'tracking.json').write_text('invalid')
            failed=reader.snapshot()
            self.assertTrue(failed['stale']); self.assertEqual(failed['bundle'],second['bundle'])


class GoalConversationTests(unittest.TestCase):
    def save(self, workspace, operations, reason='按用户说明调整'):
        body,_=prepare(workspace,operations)
        return normalize_and_validate(workspace,body,reason)

    def test_new_stage_keeps_assessment_and_does_not_inherit_old_tasks(self):
        original=initial_workspace(); old=original['goals'][0]['id']
        evaluated=self.save(original,[{'type':'assess_goal','goal_id':old,'outcome':'met','summary':'此前结果已确认','evidence':'真实反馈示例','basis':'user_feedback'}])
        with self.assertRaisesRegex(ValueError,'previous_decision'):
            prepare(evaluated,[{'type':'start_goal','title':'Next','target_state':'New result','acceptance_criteria':'New evidence','execplan_ref':'next.md'}])
        started=self.save(evaluated,[{'type':'start_goal','title':'Next','target_state':'New result','acceptance_criteria':'New evidence','execplan_ref':'next.md','previous_decision':'closed'},
                                     {'type':'add_task','title':'New work','completion_criteria':'Check result'},
                                     {'type':'add_candidate','title':'Maybe later'}])
        new=started['projects'][0]['current_goal_id']
        self.assertNotEqual(old,new)
        self.assertEqual(derived_view(started)['goal_assessment_status'][old],'met')
        self.assertEqual(derived_view(started)['goal_assessment_status'][new],'unassessed')
        self.assertEqual(started['tasks'][-2]['goal_id'],new)
        self.assertIsNone(started['tasks'][-1]['goal_id'])
        self.assertEqual(derived_view(started)['ready_task_ids'],[started['tasks'][-2]['id']])
        paused=self.save(started,[{'type':'assess_goal','goal_id':new,'outcome':'insufficient_evidence','summary':'仍需实际使用','evidence':'技术验证仅一部分','basis':'technical_check'}, {'type':'set_goal_decision','goal_id':new,'decision':'paused'}])
        self.assertEqual(derived_view(paused)['ready_task_ids'],[])
        self.assertEqual(paused['tasks'][-2]['state'],'todo')
        revised=self.save(paused,[{'type':'revise_goal','goal_id':new,'acceptance_criteria':'More precise evidence'}])
        self.assertEqual(derived_view(revised)['goal_assessment_status'][new],'unassessed')
        self.assertEqual(len(revised['goals'][-1]['assessments']),1)
