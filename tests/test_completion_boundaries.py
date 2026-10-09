import json
from pathlib import Path
import tempfile
import unittest

from app.conversation import prepare
from app.graphs import _alias, flow_puml
from app.model import derived_view, initial_workspace, normalize_and_validate, ValidationError, validate_workspace_v1
from app.store import ConflictError, WorkspaceStore
from test_migration import legacy_fixture


class CompletionBoundaryTests(unittest.TestCase):
    def test_unassigned_candidate_is_findable_without_becoming_a_commitment(self):
        workspace = initial_workspace()
        body, _ = prepare(workspace, [{"type": "add_candidate", "title": "以后尝试一个想法"}])
        saved = normalize_and_validate(workspace, body, "只记录想法")
        candidate = saved["tasks"][-1]
        self.assertIsNone(candidate["goal_id"])
        self.assertEqual(candidate["state"], "proposed")
        self.assertNotIn(candidate["id"], derived_view(saved)["ready_task_ids"])
        focus = flow_puml(saved, focus=True)
        self.assertIn("其中候选 1 项", focus)
        self.assertNotIn("项历史", focus)
        full = flow_puml(saved)
        self.assertIn(candidate["title"], full)
        self.assertIn(f'PROJECT --> {_alias(candidate["id"])} : 候选记录（未承诺）', full)
        self.assertNotIn(f'{_alias(candidate["id"])} ..>', full)

    def test_pre_migration_snapshot_conflicts_before_legacy_payload_is_parsed(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "workspace.json"
            legacy = legacy_fixture()
            path.write_text(json.dumps(legacy))
            old_body = {name: legacy[name] for name in ("projects", "tasks", "time_blocks", "work_logs")}
            with WorkspaceStore(path) as store:
                before = path.read_bytes()
                with self.assertRaises(ConflictError) as caught:
                    store.save(25, old_body, "旧页面保存")
                self.assertEqual(caught.exception.revision, 26)
                self.assertEqual(path.read_bytes(), before)

    def test_boolean_is_not_a_valid_legacy_schema_version(self):
        legacy = legacy_fixture()
        legacy["schema_version"] = True
        with self.assertRaises(ValidationError):
            validate_workspace_v1(legacy)


if __name__ == "__main__":
    unittest.main()
