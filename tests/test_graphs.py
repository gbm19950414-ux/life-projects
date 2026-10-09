import copy
import json
from pathlib import Path
import tempfile
import threading
import unittest
from unittest.mock import patch

from app.conversation import apply, prepare
from app.graphs import _alias, context_puml, flow_puml, validate_context
from app.model import ValidationError, initial_workspace
from app.server import LocalServer
from app.store import WorkspaceStore


CONTEXT = {
    "schema_version": 1, "project_id": "life-projects",
    "directions": [{"id": "direction-a", "title": "可修订方向", "status": "proposed"}],
    "areas": [{"id": "area-a", "title": "软件工程", "status": "proposed"}],
    "relations": [
        {"from": "life-projects", "to": "area-a", "kind": "belongs_to", "status": "proposed"},
        {"from": "life-projects", "to": "direction-a", "kind": "serves", "status": "proposed"},
    ],
}


class GraphTests(unittest.TestCase):
    def test_views_keep_relations_distinct_and_show_unknown_dates(self):
        workspace = initial_workspace()
        first, second = workspace["tasks"][:2]
        first["title"] = '任务"\n@startuml\n危险'
        flow = flow_puml(workspace)
        context = context_puml(workspace, CONTEXT)
        self.assertEqual(flow.count("@startuml"), 1)
        self.assertIn(f'{_alias(first["id"])} --> {_alias(second["id"])} : 前置', flow)
        self.assertIn("计划：未安排", flow)
        self.assertIn("状态：待做 · 下一步可做", flow)
        self.assertIn("归属（拟议）", context)
        self.assertIn("服务于（拟议）", context)
        self.assertNotIn(": 前置", context)
        self.assertNotIn("归属", flow)

    def test_context_ref_and_kind_are_checked(self):
        invalid = copy.deepcopy(CONTEXT)
        invalid["relations"][0]["to"] = "missing"
        with self.assertRaisesRegex(ValidationError, "引用"):
            validate_context(invalid, "life-projects")
        invalid = copy.deepcopy(CONTEXT)
        invalid["relations"][0]["kind"] = "serves"
        with self.assertRaisesRegex(ValidationError, "服务目标"):
            validate_context(invalid, "life-projects")
        empty = {"schema_version": 1, "project_id": "life-projects", "directions": [], "areas": [], "relations": []}
        self.assertIn("待明确", context_puml(initial_workspace(), empty))

    def test_prepare_candidate_blocker_dependency_and_reschedule(self):
        workspace = initial_workspace()
        first, second = workspace["tasks"][:2]
        unchanged = copy.deepcopy(workspace)
        body, summary = prepare(workspace, [{"type": "add_candidate", "title": "稍后思考"},
                                            {"type": "set_blocker", "task_id": first["id"], "obstacle": "等待反馈"},
                                            {"type": "set_dependency", "task_id": second["id"], "depends_on": []},
                                            {"type": "schedule", "task_id": second["id"],
                                             "planned_start": "2026-09-25T10:00:00+08:00",
                                             "planned_end": "2026-09-25T11:00:00+08:00"}])
        self.assertEqual(workspace, unchanged)
        self.assertEqual(len(summary), 4)
        self.assertEqual(body["tasks"][-1]["title"], "稍后思考")
        self.assertEqual(body["tasks"][0]["state"], "blocked")
        self.assertEqual(body["tasks"][1]["depends_on"], [])
        self.assertEqual(body["time_blocks"][0]["task_id"], second["id"])

    def test_conversation_uses_existing_api_and_persists(self):
        with tempfile.TemporaryDirectory() as folder, WorkspaceStore(Path(folder) / "workspace.json") as store:
            context = Path(folder) / "context.json"
            context.write_text(json.dumps(CONTEXT), encoding="utf-8")
            server = LocalServer(0, store)
            thread = threading.Thread(target=server.serve_forever, daemon=True)
            thread.start()
            try:
                url = f"http://127.0.0.1:{server.server_port}"
                with patch("app.conversation.generate", return_value=Path(folder) / "index.html"):
                    result = apply(url, [{"type": "add_candidate", "title": "询问用户图是否易读"}], "",
                                   context, Path(folder) / "views", Path(folder) / "plantuml.jar")
                self.assertEqual(result["revision"], 1)
                self.assertEqual(store.read()["tasks"][-1]["state"], "proposed")
                self.assertEqual(store.read()["tasks"][-1]["title"], "询问用户图是否易读")
                first, second = store.read()["tasks"][:2]
                with patch("app.conversation.generate", return_value=Path(folder) / "index.html"):
                    changed = apply(url, [
                        {"type": "set_blocker", "task_id": first["id"], "obstacle": "等待反馈"},
                        {"type": "set_dependency", "task_id": second["id"], "depends_on": []},
                        {"type": "schedule", "task_id": second["id"],
                         "planned_start": "2026-09-25T10:00:00+08:00", "planned_end": "2026-09-25T11:00:00+08:00"},
                    ], "", context, Path(folder) / "views", Path(folder) / "plantuml.jar")
                self.assertEqual(changed["revision"], 2)
                self.assertEqual(store.read()["tasks"][0]["block_reason"], "等待反馈")
                self.assertEqual(store.read()["tasks"][1]["depends_on"], [])
                block = store.read()["time_blocks"][0]
                with patch("app.conversation.generate", side_effect=RuntimeError("renderer offline")):
                    with self.assertRaisesRegex(RuntimeError, "已保存 revision 3，但图未更新"):
                        apply(url, [{"type": "reschedule", "block_id": block["id"],
                                     "planned_start": "2026-09-25T14:00:00+08:00", "planned_end": "2026-09-25T15:00:00+08:00"}],
                              "", context, Path(folder) / "views", Path(folder) / "plantuml.jar")
                self.assertEqual(store.read()["time_blocks"][0]["planned_start"], "2026-09-25T14:00:00+08:00")
                self.assertEqual(store.read()["revision"], 3)
            finally:
                server.shutdown()
                server.server_close()
                thread.join()


if __name__ == "__main__":
    unittest.main()
