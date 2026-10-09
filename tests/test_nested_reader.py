import copy
import unittest
from unittest.mock import patch

from app.graphs import _alias, flow_puml
from app.model import initial_workspace
from app.reader import build_bundle
from test_cross_project_views import context_v2, tracking_v2
from test_reader import SVG


def two_stages():
    value = initial_workspace()
    goal = value['goals'][0]
    old = copy.deepcopy(goal)
    old.update(id='historical-stage', title='历史阶段', decision='closed', predecessor_id=None)
    value['goals'].append(old)
    goal['predecessor_id'] = old['id']
    value['tasks'][0]['goal_id'] = old['id']
    value['tasks'][0]['state'] = 'done'
    unassigned = copy.deepcopy(value['tasks'][0])
    unassigned.update(id='unassigned-item', title='无阶段候选', goal_id=None, state='proposed', depends_on=[])
    value['tasks'].append(unassigned)
    return value


class NestedReaderTests(unittest.TestCase):
    @patch('app.reader._render_svg', return_value=SVG)
    def test_all_stages_unassigned_and_relationships_are_exposed_without_mutation(self, renderer):
        workspace, context, tracking = two_stages(), context_v2(), tracking_v2()
        originals = copy.deepcopy((workspace, context, tracking))
        bundle = build_bundle(workspace, context, tracking=tracking)
        self.assertEqual((workspace, context, tracking), originals)
        self.assertEqual(bundle['presentation_version'], 4)
        local, old, external = bundle['views']
        self.assertTrue(local['is_current'])
        self.assertEqual(local['predecessor_ids'], [old['id']])
        self.assertFalse(old['is_current'])
        self.assertEqual(old['stage_status'], 'closed')
        self.assertIn('不推荐下一步', old['next'])
        self.assertEqual(bundle['projects'][0]['unassigned_task_nodes'], [_alias('unassigned-item')])
        self.assertEqual(bundle['context'], {k:context[k] for k in ('areas','directions','relations')})
        external_relations = [r for r in bundle['context']['relations'] if r['from']==external['project_id']]
        self.assertFalse(any(r['kind']=='belongs_to' for r in external_relations))
        self.assertEqual(external['stage_status'], 'closed')
        self.assertEqual(external['plan_id'], tracking['goals'][0]['plan_id'])
        self.assertEqual(len(bundle['svgs']), 7)
        model = bundle['reading_model']
        self.assertEqual(model['project_ids'], [p['id'] for p in bundle['projects']])
        objects = model['objects']
        prior_task = 'local:task:' + workspace['tasks'][0]['id']
        self.assertEqual(objects[prior_task]['parent_id'], old['id'])
        self.assertNotIn(prior_task, objects[local['id']]['children'])
        self.assertEqual(objects['local:task:unassigned-item']['parent_id'], 'life-projects')
        self.assertEqual(objects[prior_task]['source_version'], 'local:r0')
        self.assertEqual(objects[local['id']]['predecessor_ids'], [old['id']])

    def test_explicit_goal_excludes_other_work_and_labels_direct_prerequisites(self):
        workspace = two_stages()
        current, old = workspace['goals']
        text = flow_puml(workspace, goal_id=current['id'])
        self.assertIn('关联前置（不属本目标）', text)
        self.assertNotIn(_alias('unassigned-item'), text)
        self.assertIn(_alias(workspace['tasks'][0]['id']), text)
        history = flow_puml(workspace, focus=True, goal_id=old['id'])
        self.assertIn(_alias(workspace['tasks'][0]['id']), history)
        self.assertNotIn(_alias(workspace['tasks'][1]['id']), history)
        self.assertNotIn('下一步可做', history)
        self.assertIn('本阶段结束', history)

    @patch('app.reader._render_svg', return_value=SVG)
    def test_empty_and_all_done_do_not_change_stage_decision(self, renderer):
        workspace = initial_workspace()
        for task in workspace['tasks']:
            task['state'] = 'done'
        done = build_bundle(workspace, context_v2(), tracking=tracking_v2())
        self.assertEqual(done['views'][0]['stage_status'], 'active')
        self.assertIn('任务完成不等于目标达成', done['sources']['flow'])
        workspace['tasks'] = []
        empty = build_bundle(workspace, context_v2(), tracking=tracking_v2())
        self.assertEqual(empty['views'][0]['task_count'], 0)
        self.assertIn('尚未规划推进事项', empty['sources']['flow'])
        self.assertIn(_alias(workspace['goals'][0]['id']), empty['sources']['flow'])

    @patch('app.reader._render_svg', return_value=SVG)
    def test_cache_reuses_unaffected_goal_diagrams(self, renderer):
        workspace, context, tracking = two_stages(), context_v2(), tracking_v2()
        first = build_bundle(workspace, context, tracking=tracking)
        renderer.reset_mock()
        same = build_bundle(workspace, context, tracking=tracking, previous=first)
        renderer.assert_not_called()
        self.assertEqual(same['svgs'], first['svgs'])
        tracking['goals'][0]['milestones'][0]['title'] += '（新证据）'
        updated = build_bundle(workspace, context, tracking=tracking, previous=first)
        self.assertEqual(renderer.call_count, 2)
        self.assertEqual(updated['svgs']['flow'], first['svgs']['flow'])

    @patch('app.reader._render_svg', return_value=SVG)
    def test_multiple_directions_remain_relations_not_ownership(self, renderer):
        context = context_v2()
        context['directions'].append({'id':'another-direction','title':'另一方向','status':'confirmed'})
        context['relations'] += [
            {'from':'life-projects','to':'direction-a','kind':'serves','status':'proposed'},
            {'from':'life-projects','to':'another-direction','kind':'serves','status':'confirmed'},
        ]
        bundle = build_bundle(initial_workspace(), context, tracking=tracking_v2())
        self.assertEqual(len(bundle['projects']), 2)
        self.assertEqual(len([r for r in bundle['context']['relations'] if r['from']=='life-projects' and r['kind']=='serves']),2)
        self.assertEqual(len([r for r in bundle['context']['relations'] if r['from']=='life-projects' and r['kind']=='belongs_to']),1)

    @patch('app.reader._render_svg', return_value=SVG)
    def test_only_formally_blocked_stage_promotes_source_gap_to_block_reason(self, renderer):
        tracking = tracking_v2()
        goal = tracking['goals'][0]
        goal['stage_status'] = 'blocked'
        goal['gap'] = '缺本人阅读回执'
        blocked = build_bundle(initial_workspace(), context_v2(), tracking=tracking)
        view = next(v for v in blocked['views'] if v['external'])
        self.assertEqual(view['block_reason'], goal['gap'])
        self.assertEqual(blocked['reading_model']['objects'][view['id']]['block_reason'], goal['gap'])
        # Checking the external source belongs to the live response, not an
        # unqualified 'synced' default in an independently generated bundle.
        self.assertEqual(blocked['reading_model']['objects'][view['id']]['freshness'], 'not_checked')
        goal['stage_status'] = 'active'
        active = build_bundle(initial_workspace(), context_v2(), tracking=tracking)
        view = next(v for v in active['views'] if v['external'])
        self.assertEqual(view['block_reason'], '')


if __name__ == '__main__':
    unittest.main()
