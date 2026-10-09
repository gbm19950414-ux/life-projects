import copy
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from app.model import ValidationError, initial_workspace, migrate_workspace_v1, validate_workspace, validate_workspace_v1
from app.recovery import restore_backup
from app.store import StorageError, WorkspaceStore


def legacy_fixture():
    current = initial_workspace()
    goal = current["goals"][0]
    project = current["projects"][0]
    old = {
        "schema_version": 1, "revision": 25,
        "projects": [{
            "id": project["id"], "title": project["title"],
            "goal": goal["target_state"], "reason": project["reason"],
            "acceptance_criteria": goal["acceptance_criteria"],
            "source_ref": project["source_ref"], "execplan_ref": goal["execplan_ref"],
        }],
        "tasks": [{key: value for key, value in task.items() if key != "goal_id"} for task in current["tasks"]],
        "time_blocks": [], "work_logs": [], "changes": [],
    }
    old["tasks"][0]["state"] = "doing"
    old["time_blocks"].append({
        "id": "planned", "task_id": old["tasks"][0]["id"],
        "planned_start": "2026-09-26T10:00:00+08:00", "planned_end": "2026-09-26T11:00:00+08:00",
    })
    old["work_logs"].append({
        "id": "actual", "task_id": old["tasks"][0]["id"], "block_id": "planned",
        "actual_start": "2026-09-26T10:02:00+08:00", "actual_end": "2026-09-26T10:51:00+08:00",
        "result": "已试用", "obstacle": "", "evidence": "本地运行", "note": "",
        "criteria_checked": False,
    })
    old["changes"].append({
        "entity": "task", "id": old["tasks"][0]["id"], "old": None,
        "new": copy.deepcopy(old["tasks"][0]), "at": "2026-09-26T10:00:00+08:00", "reason": "原始建立",
    })
    validate_workspace_v1(old)
    return old


class MigrationTests(unittest.TestCase):
    def test_strict_upgrade_preserves_all_existing_work(self):
        old = legacy_fixture()
        upgraded = migrate_workspace_v1(old)
        validate_workspace(upgraded)
        self.assertEqual(upgraded["schema_version"], 2)
        self.assertEqual(upgraded["revision"], 26)
        self.assertEqual(upgraded["tasks"][0]["goal_id"], None)
        for before, after in zip(old["tasks"], upgraded["tasks"]):
            self.assertEqual({key: value for key, value in after.items() if key != "goal_id"}, before)
        self.assertEqual(upgraded["time_blocks"], old["time_blocks"])
        self.assertEqual(upgraded["work_logs"], old["work_logs"])
        self.assertEqual(upgraded["changes"], old["changes"])
        goal = upgraded["goals"][0]
        self.assertEqual(goal["id"], "legacy-life-projects")
        self.assertEqual(goal["decision"], "unknown")
        self.assertEqual(goal["assessments"], [])
        self.assertEqual(goal["target_state"], old["projects"][0]["goal"])

    def test_startup_migrates_once_and_keeps_raw_preimage(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "workspace.json"
            raw = json.dumps(legacy_fixture(), ensure_ascii=False, indent=2).encode()
            path.write_bytes(raw)
            with WorkspaceStore(path) as store:
                self.assertEqual(store.read()["revision"], 26)
                self.assertEqual(store.pre_v2.read_bytes(), raw)
            with WorkspaceStore(path) as reopened:
                self.assertEqual(reopened.read()["revision"], 26)
                self.assertEqual(reopened.pre_v2.read_bytes(), raw)

    def test_migration_failure_does_not_change_original(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "workspace.json"
            raw = json.dumps(legacy_fixture(), ensure_ascii=False).encode()
            path.write_bytes(raw)
            real_write = WorkspaceStore._write_atomically
            def fail_main(target, value):
                if target == path.resolve():
                    raise OSError("simulated disk failure")
                return real_write(target, value)
            with patch.object(WorkspaceStore, "_write_atomically", side_effect=fail_main):
                with self.assertRaisesRegex(StorageError, "迁移"):
                    WorkspaceStore(path)
            self.assertEqual(path.read_bytes(), raw)
            self.assertEqual(Path(str(path) + ".pre-v2").read_bytes(), raw)
            with WorkspaceStore(path) as store:
                self.assertEqual(store.read()["revision"], 26)
            malformed = legacy_fixture()
            malformed["tasks"][0]["state"] = "fake"
            path2 = Path(folder) / "invalid.json"
            invalid_raw = json.dumps(malformed).encode()
            path2.write_bytes(invalid_raw)
            with self.assertRaisesRegex(StorageError, "校验失败"):
                WorkspaceStore(path2)
            self.assertEqual(path2.read_bytes(), invalid_raw)
            self.assertFalse(Path(str(path2) + ".pre-v2").exists())

    def test_valid_v1_refuses_restore_and_restored_v1_upgrades_on_start(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "workspace.json"
            old = legacy_fixture()
            path.write_text(json.dumps(old), encoding="utf-8")
            backup = Path(str(path) + ".bak")
            backup.write_text(json.dumps(initial_workspace()), encoding="utf-8")
            with self.assertRaisesRegex(StorageError, "仍有效"):
                restore_backup(path)
            # A corrupt current ledger can recover a strict v1 backup.
            path.write_text("{broken", encoding="utf-8")
            backup.write_text(json.dumps(old), encoding="utf-8")
            revision, preserved = restore_backup(path)
            self.assertEqual(revision, 25)
            self.assertEqual(preserved.read_text(), "{broken")
            with WorkspaceStore(path) as store:
                self.assertEqual(store.read()["revision"], 26)

    def test_restored_older_v1_keeps_both_migration_preimages(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "workspace.json"
            newer = legacy_fixture()
            raw_newer = json.dumps(newer).encode()
            path.write_bytes(raw_newer)
            with WorkspaceStore(path) as store:
                self.assertEqual(store.read()["revision"], 26)
            older = legacy_fixture()
            older["revision"] = 24
            raw_older = json.dumps(older).encode()
            Path(str(path) + ".bak").write_bytes(raw_older)
            path.write_text("{broken", encoding="utf-8")
            restore_backup(path)
            restored_bytes = path.read_bytes()
            with WorkspaceStore(path) as store:
                self.assertEqual(store.read()["revision"], 25)
            self.assertEqual(Path(str(path) + ".pre-v2").read_bytes(), raw_newer)
            others = list(Path(folder).glob("workspace.json.pre-v2-*"))
            self.assertEqual(len(others), 1)
            self.assertEqual(others[0].read_bytes(), restored_bytes)


if __name__ == "__main__":
    unittest.main()
