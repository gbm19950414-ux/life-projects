import copy
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from app.graphs import context_puml, external_flow_puml, milestone_alias, tracking_alias, validate_context
from app.model import ValidationError, initial_workspace
from app.reader import ReadingViews, build_bundle
from app.store import WorkspaceStore
from app.tracking import TrackingError, save_tracking, validate_tracking
from test_tracking import example
from test_reader import SVG


def tracking_v2():
    value = example()
    value["schema_version"] = 2
    value["goals"][0].update(stage_status="closed", milestones=[
        {"id": "C0", "title": "来源冻结", "state": "done", "depends_on": [], "source_ref": "old/C.md", "note": "来源报告完成"},
        {"id": "C1", "title": "完成检查", "state": "done", "depends_on": [], "source_ref": "old/C.md", "note": "未声明 C0 前置"},
    ])
    return value


def context_v2():
    return {"schema_version": 2, "project_id": "life-projects",
            "directions": [{"id": "direction-a", "title": "健康生活方向", "status": "proposed"}],
            "areas": [{"id": "area-a", "title": "软件工程", "status": "proposed"},
                      {"id": "area-b", "title": "知识建模", "status": "confirmed"}],
            "relations": [{"from": "life-projects", "to": "area-a", "kind": "belongs_to", "status": "proposed"},
                          {"from": "tracking:project:external", "to": "area-b", "kind": "supports", "status": "confirmed"},
                          {"from": "area-a", "to": "direction-a", "kind": "serves", "status": "proposed"}]}


class CrossProjectViewTests(unittest.TestCase):
    def test_milestone_alias_is_unique_across_goal_boundaries(self):
        self.assertNotEqual(milestone_alias("a:b", "c"), milestone_alias("a", "b:c"))

    def test_v1_has_no_inferred_milestones_and_v2_rejects_bad_references(self):
        old = example()
        self.assertEqual(validate_tracking(old), old)
        current = tracking_v2()
        self.assertEqual(validate_tracking(current), current)
        invalid = copy.deepcopy(current)
        invalid["goals"][0]["milestones"][0]["source_ref"] = "missing"
        with self.assertRaisesRegex(TrackingError, "来源引用"):
            validate_tracking(invalid)
        invalid = copy.deepcopy(current)
        invalid["goals"][0]["milestones"][0]["depends_on"] = ["C1"]
        invalid["goals"][0]["milestones"][1]["depends_on"] = ["C0"]
        with self.assertRaisesRegex(TrackingError, "成环"):
            validate_tracking(invalid)
        invalid = copy.deepcopy(current)
        invalid["goals"][0]["milestones"][0]["depends_on"] = ["other-goal-C0"]
        with self.assertRaisesRegex(TrackingError, "前置引用"):
            validate_tracking(invalid)
        invalid = copy.deepcopy(current)
        invalid["goals"][0]["milestones"][1]["id"] = "C0"
        with self.assertRaisesRegex(TrackingError, "ID 重复"):
            validate_tracking(invalid)
        invalid = copy.deepcopy(current)
        invalid["goals"][0]["stage_status"] = "done"
        with self.assertRaisesRegex(TrackingError, "阶段状态"):
            validate_tracking(invalid)
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "tracking.json"
            saved = save_tracking(path, 0, current)
            with self.assertRaises(TrackingError):
                save_tracking(path, 1, invalid)
            self.assertEqual(json.loads(path.read_text()), saved)

    def test_context_v2_types_unknown_and_shared_partitions(self):
        tracking = tracking_v2()
        context = context_v2()
        validate_context(context, "life-projects", tracking)
        diagram = context_puml(initial_workspace(), context, tracking)
        for label in ('package "方向"', 'package "领域"', 'package "项目"', 'package "阶段目标"'):
            self.assertIn(label, diagram)
        self.assertNotIn('package "外部计划', diagram)
        self.assertIn(tracking_alias("project", "external"), diagram)
        self.assertIn("领域待明确", diagram)
        self.assertIn("方向待明确", diagram)
        self.assertIn("支持", diagram)
        self.assertNotIn("虚线：拟议", diagram)
        invalid = copy.deepcopy(context)
        invalid["relations"].append({"from": "tracking:project:external", "to": "area-a", "kind": "belongs_to", "status": "confirmed"})
        invalid["relations"].append({"from": "tracking:project:external", "to": "area-b", "kind": "belongs_to", "status": "confirmed"})
        with self.assertRaisesRegex(ValidationError, "一个主要领域"):
            validate_context(invalid, "life-projects", tracking)
        invalid = copy.deepcopy(context)
        invalid["relations"][1]["from"] = "tracking:project:missing"
        with self.assertRaisesRegex(ValidationError, "引用"):
            validate_context(invalid, "life-projects", tracking)
        invalid = copy.deepcopy(context)
        invalid["relations"][1]["kind"] = "serves"
        with self.assertRaisesRegex(ValidationError, "服务目标"):
            validate_context(invalid, "life-projects", tracking)

    @patch("app.reader._render_svg", return_value=SVG)
    def test_view_contract_keeps_goals_separate_and_source_details(self, renderer):
        tracking = tracking_v2()
        tracking["goals"][0]["next_action"] = "阶段已归档；来源已指定 C+"
        workspace = initial_workspace()
        bundle = build_bundle(workspace, context_v2(), tracking=tracking)
        self.assertEqual(len(bundle["views"]), 2)
        fields = {"id", "project_id", "project_title", "goal_id", "goal_node", "title", "target_state", "status", "next", "external", "flow_key", "full_key", "observed_at", "stage_status", "is_current", "predecessor_ids", "plan_id", "task_count", "record_kind"}
        self.assertEqual(set(bundle["views"][0]), fields)
        self.assertEqual(set(bundle["views"][1]), fields | {"block_reason"})
        self.assertEqual(bundle["views"][1]["block_reason"], "")
        local, external = bundle["views"]
        self.assertEqual(local["id"], f'local:goal:{workspace["goals"][0]["id"]}')
        self.assertEqual(local["project_id"], "life-projects")
        self.assertEqual(external["id"], "tracking:goal:goal-c")
        self.assertEqual(external["project_id"], "tracking:project:external")
        self.assertEqual(external["goal_node"], tracking_alias("goal", "goal-c"))
        self.assertEqual(external["next"], tracking["goals"][0]["next_action"])
        self.assertIn(external["flow_key"], bundle["svgs"])
        self.assertIn(external["full_key"], bundle["sources"])
        self.assertNotIn("来源明确前置", bundle["sources"][external["full_key"]])
        detail = dict(bundle["nodes"][milestone_alias("goal-c", "C0")]["fields"])
        self.assertIn("old/C.md", detail["来源"])
        self.assertIn("2026-09-26 18:00", detail["来源"])
        self.assertIn("主要领域待明确", dict(bundle["nodes"][tracking_alias("project", "external")]["fields"])["主要领域"])
        self.assertEqual(renderer.call_count, 5)
        legacy = build_bundle(workspace, context_v2(), tracking=example())
        legacy_view = legacy["views"][1]
        self.assertIn("未记录结构化里程碑", legacy["sources"][legacy_view["flow_key"]])

    @patch("app.reader._render_svg", return_value=SVG)
    def test_invalid_v2_snapshot_preserves_last_successful_bundle(self, renderer):
        with tempfile.TemporaryDirectory() as folder, WorkspaceStore(Path(folder) / "workspace.json") as store:
            root = Path(folder)
            (root / "context.json").write_text(json.dumps(context_v2()))
            save_tracking(root / "tracking.json", 0, tracking_v2())
            reader = ReadingViews(store, root / "context.json", root / "views")
            first = reader.snapshot()
            self.assertFalse(first["stale"])
            broken = tracking_v2()
            broken["goals"][0]["milestones"][0]["source_ref"] = "missing"
            (root / "tracking.json").write_text(json.dumps(broken))
            stale = reader.snapshot()
            self.assertTrue(stale["stale"])
            self.assertEqual(stale["bundle"], first["bundle"])


if __name__ == "__main__":
    unittest.main()
