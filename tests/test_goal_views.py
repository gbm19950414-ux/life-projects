import copy
import unittest
from unittest.mock import patch

from app.graphs import _alias, context_puml, flow_puml, tracking_alias
from app.model import derived_view, initial_workspace
from app.reader import build_bundle
from app.tracking import empty_tracking


CONTEXT = {"schema_version": 1, "project_id": "life-projects", "directions": [], "areas": [], "relations": []}
SVG = b'<svg xmlns="http://www.w3.org/2000/svg" width="1" height="1" />'


class GoalViewTests(unittest.TestCase):
    def test_endpoint_remains_visible_with_empty_or_completed_tasks(self):
        workspace = initial_workspace()
        goal = workspace["goals"][0]
        empty = copy.deepcopy(workspace)
        empty["tasks"] = []
        text = flow_puml(empty, focus=True)
        self.assertIn("阶段终点", text)
        self.assertIn(_alias(goal["id"]), text)
        self.assertIn("尚未规划推进事项", text)
        done = copy.deepcopy(workspace)
        for task in done["tasks"]:
            task["state"] = "done"
        text = flow_puml(done, focus=True)
        self.assertIn(_alias(goal["id"]), text)
        self.assertIn("任务完成不等于目标达成", text)
        self.assertEqual(derived_view(done)["goal_assessment_status"][goal["id"]], "unassessed")

    def test_paused_goal_preserves_tasks_but_hides_next_recommendation(self):
        workspace = initial_workspace()
        workspace["goals"][0]["decision"] = "paused"
        self.assertEqual(derived_view(workspace)["ready_task_ids"], [])
        text = flow_puml(workspace, focus=True)
        self.assertIn("暂停；任务状态保留，不推荐下一步", text)
        self.assertEqual(workspace["tasks"][0]["state"], "todo")

    def test_assessment_and_external_source_are_distinct_in_reader(self):
        workspace = initial_workspace()
        goal = workspace["goals"][0]
        goal["assessments"].append({"id": "a1", "definition_version": 1,
                                    "outcome": "insufficient_evidence", "summary": "还需实际使用",
                                    "evidence": "技术测试已通过，用户体验尚未核对", "basis": "observation",
                                    "at": "2026-09-26T10:00:00+08:00"})
        tracking = empty_tracking()
        tracking.update({"revision": 1, "observed_at": "2026-09-26T10:00:00+08:00", "sync_status": "ok",
                         "sync_message": "按需观察", "source_thread": {
                             "id": "synthetic-development-thread", "title": "执行工作流优化与扩展验收",
                             "url": "codex://threads/synthetic-development-thread", "status": "active"},
                         "projects": [{"id": "external-dev", "title": "外部开发"}],
                         "goals": [{"id": "external-c", "project_id": "external-dev", "title": "第六章验收",
                                    "target_state": "章节可复现", "plan_id": "plan-c", "plan_ref": "docs/c.md",
                                    "progress": "来源报告：进行中", "assessment": "尚未验收", "gap": "浏览器验证",
                                    "next_action": "执行真实来源对比", "sources": [{"kind": "plan", "ref": "docs/c.md",
                                                                               "title": "ExecPlan C", "observed_at": "2026-09-26T10:00:00+08:00",
                                                                               "sha256": "a" * 64}]}], "relations": []})
        diagram = context_puml(workspace, CONTEXT, tracking)
        self.assertIn(tracking_alias("goal", "external-c"), diagram)
        self.assertIn("只读观察", diagram)
        self.assertNotIn(tracking_alias("goal", "external-c"), flow_puml(workspace, focus=True))
        with patch("app.reader._render_svg", return_value=SVG):
            bundle = build_bundle(workspace, CONTEXT, tracking=tracking)
        local = bundle["nodes"][_alias(goal["id"])]["fields"]
        external = bundle["nodes"][tracking_alias("goal", "external-c")]["fields"]
        self.assertIn(["结果评价", "证据不足"], local)
        self.assertIn(["同步方式", "仅展示来源报告；同步状态见页面，本地未重新验收"], external)
        self.assertIn(["来源报告的验收", "尚未验收"], external)
        self.assertIn("SHA-256: " + "a" * 64, dict(external)["来源"])


if __name__ == "__main__":
    unittest.main()
