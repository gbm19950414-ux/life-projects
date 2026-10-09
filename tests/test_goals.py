import copy
import unittest
from uuid import uuid4

from app.model import ValidationError, derived_view, initial_workspace, normalize_and_validate


def editable(workspace):
    return {name: copy.deepcopy(workspace[name]) for name in ("projects", "goals", "tasks", "time_blocks", "work_logs")}


def assess(workspace, outcome, *, evidence="使用反馈：能找到下一步"):
    body = editable(workspace)
    body["goals"][0]["assessments"].append({
        "outcome": outcome, "summary": "本轮检查", "evidence": evidence, "basis": "user_feedback",
    })
    return normalize_and_validate(workspace, body, "记录观察")


class GoalModelTests(unittest.TestCase):
    def test_assessment_is_independent_of_tasks_and_decision(self):
        workspace = initial_workspace()
        first = assess(workspace, "insufficient_evidence")
        goal = first["goals"][0]
        self.assertEqual(goal["decision"], "active")
        self.assertEqual(goal["assessments"][0]["definition_version"], 1)
        self.assertTrue(goal["assessments"][0]["id"])
        self.assertTrue(goal["assessments"][0]["at"])
        self.assertEqual(derived_view(first)["goal_assessment_status"][goal["id"]], "insufficient_evidence")
        body = editable(first)
        body["goals"][0]["decision"] = "closed"
        closed = normalize_and_validate(first, body, "本轮暂不继续")
        self.assertEqual(closed["tasks"], first["tasks"])
        self.assertEqual(derived_view(closed)["ready_task_ids"], [])
        self.assertEqual(derived_view(closed)["goal_assessment_status"][goal["id"]], "insufficient_evidence")

    def test_definition_revision_resets_current_assessment_without_erasing_history(self):
        first = assess(initial_workspace(), "met")
        body = editable(first)
        body["goals"][0]["target_state"] += " 并看清是否应结束。"
        revised = normalize_and_validate(first, body, "细化本轮结果")
        goal = revised["goals"][0]
        self.assertEqual(goal["definition_version"], 2)
        self.assertEqual(goal["assessments"][0]["outcome"], "met")
        self.assertEqual(derived_view(revised)["goal_assessment_status"][goal["id"]], "unassessed")
        body = editable(first)
        body["goals"][0]["target_state"] += " 并看清是否应结束。"
        body["goals"][0]["assessments"].append({"outcome": "met", "summary": "通过", "evidence": "新反馈", "basis": "observation"})
        with self.assertRaisesRegex(ValidationError, "必须分开保存"):
            normalize_and_validate(first, body, "细化并验收")

    def test_assessments_are_append_only_and_not_auto_met_when_tasks_done(self):
        workspace = initial_workspace()
        body = editable(workspace)
        body["tasks"] = []
        with self.assertRaisesRegex(ValidationError, "不能删除"):
            normalize_and_validate(workspace, body, "")
        body = editable(workspace)
        for task in body["tasks"]:
            task["state"] = "done"
        # Even if a migrated ledger contains completed work, no assessment is inferred.
        snapshot = copy.deepcopy(workspace)
        snapshot["tasks"] = body["tasks"]
        self.assertEqual(derived_view(snapshot)["goal_assessment_status"][snapshot["goals"][0]["id"]], "unassessed")
        assessed = assess(workspace, "not_met")
        body = editable(assessed)
        body["goals"][0]["assessments"][0]["summary"] = "改写旧结论"
        with self.assertRaisesRegex(ValidationError, "只能追加"):
            normalize_and_validate(assessed, body, "")

    def test_new_phase_preserves_old_result_and_requires_old_decision(self):
        assessed = assess(initial_workspace(), "met")
        old_id = assessed["goals"][0]["id"]
        next_id = str(uuid4())
        body = editable(assessed)
        body["projects"][0]["current_goal_id"] = next_id
        body["goals"].append({
            "id": next_id, "project_id": "life-projects", "title": "第二阶段",
            "target_state": "可凭证据结束这一轮", "acceptance_criteria": "能够判定达成与否",
            "execplan_ref": "plan-b.md", "predecessor_id": old_id,
        })
        with self.assertRaisesRegex(ValidationError, "只允许一个当前 active"):
            normalize_and_validate(assessed, body, "开始新阶段")
        body["goals"][0]["decision"] = "closed"
        next_phase = normalize_and_validate(assessed, body, "上一阶段结束，开始新阶段")
        self.assertEqual(next_phase["goals"][0]["assessments"][0]["outcome"], "met")
        self.assertEqual(derived_view(next_phase)["goal_assessment_status"][next_id], "unassessed")
        self.assertEqual(derived_view(next_phase)["ready_task_ids"], [])

    def test_goal_and_task_refs_are_strict(self):
        workspace = initial_workspace()
        body = editable(workspace)
        body["tasks"][0]["goal_id"] = "unknown-goal"
        with self.assertRaisesRegex(ValidationError, "任务目标引用"):
            normalize_and_validate(workspace, body, "")
        body = editable(workspace)
        body["goals"][0]["predecessor_id"] = body["goals"][0]["id"]
        with self.assertRaisesRegex(ValidationError, "目标接续引用"):
            normalize_and_validate(workspace, body, "")

    def test_inactive_noncurrent_cannot_be_reactivated_and_unknown_is_legacy_only(self):
        first = assess(initial_workspace(), "met")
        old_id = first["goals"][0]["id"]
        new_id = str(uuid4())
        body = editable(first)
        body["goals"][0]["decision"] = "closed"
        body["goals"].append({"id": new_id, "project_id": "life-projects", "title": "下一阶段",
                              "target_state": "结果成立", "acceptance_criteria": "可核对",
                              "execplan_ref": "", "predecessor_id": old_id})
        body["projects"][0]["current_goal_id"] = new_id
        next_phase = normalize_and_validate(first, body, "开始下一阶段")
        body = editable(next_phase)
        body["goals"][0]["decision"] = "active"
        with self.assertRaisesRegex(ValidationError, "只允许一个当前 active"):
            normalize_and_validate(next_phase, body, "恢复旧阶段")
        body = editable(next_phase)
        body["goals"][1]["decision"] = "unknown"
        with self.assertRaisesRegex(ValidationError, "推进决定无效"):
            normalize_and_validate(next_phase, body, "不再继续")

    def test_new_goal_cannot_import_assessment_or_forge_version(self):
        workspace = initial_workspace()
        body = editable(workspace)
        body["goals"][0]["decision"] = "paused"
        new_id = str(uuid4())
        body["projects"][0]["current_goal_id"] = new_id
        body["goals"].append({"id": new_id, "project_id": "life-projects", "title": "下一阶段",
                              "target_state": "结果成立", "acceptance_criteria": "可核对",
                              "definition_version": 2, "execplan_ref": "", "predecessor_id": workspace["goals"][0]["id"],
                              "decision": "active", "assessments": []})
        # Version 1 is the only valid initial definition.
        with self.assertRaisesRegex(ValidationError, "新目标定义版本"):
            normalize_and_validate(workspace, body, "开始下一阶段")
        body["goals"][-1]["definition_version"] = 1
        body["goals"][-1]["assessments"].append({"outcome": "met", "summary": "通过", "evidence": "证据", "basis": "observation"})
        with self.assertRaisesRegex(ValidationError, "单独追加"):
            normalize_and_validate(workspace, body, "开始下一阶段")

    def test_current_goal_is_required_and_goal_ids_cannot_shadow_task_nodes(self):
        workspace = initial_workspace()
        body = editable(workspace)
        body["projects"][0]["current_goal_id"] = None
        with self.assertRaisesRegex(ValidationError, "当前目标引用"):
            normalize_and_validate(workspace, body, "本轮暂不继续")
        body = editable(workspace)
        body["goals"][0]["decision"] = "paused"
        body["projects"][0]["current_goal_id"] = workspace["tasks"][0]["id"]
        body["goals"].append({"id": workspace["tasks"][0]["id"], "project_id": "life-projects",
                              "title": "冲突节点", "target_state": "结果成立", "acceptance_criteria": "可核对",
                              "execplan_ref": "", "predecessor_id": workspace["goals"][0]["id"]})
        with self.assertRaisesRegex(ValidationError, "ID 与项目或任务 ID 重复"):
            normalize_and_validate(workspace, body, "切换阶段")


if __name__ == "__main__":
    unittest.main()
