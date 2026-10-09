import copy
from datetime import datetime, timedelta, timezone
import json
import os
from pathlib import Path
import tempfile
import threading
import unittest
from unittest.mock import patch
from urllib.error import HTTPError
from urllib.request import Request, urlopen

from app.model import ValidationError, derived_view, initial_workspace, normalize_and_validate, validate_workspace
from app.server import LocalServer
from app.recovery import restore_backup
from app.store import ConflictError, StorageError, WorkspaceStore


def editable(workspace):
    return {key: copy.deepcopy(workspace[key]) for key in ("projects", "goals", "tasks", "time_blocks", "work_logs")}


def add_log(body, task_id, result="实际完成了验收内容", checked=True, **other):
    body["work_logs"].append({"task_id": task_id, "result": result,
                              "criteria_checked": checked, **other})


def advance(store, task_id, state, *, reason="", result=None, checked=False):
    current = store.read()
    body = editable(current)
    task = next(item for item in body["tasks"] if item["id"] == task_id)
    task["state"] = state
    if result:
        add_log(body, task_id, result, checked)
    return store.save(current["revision"], body, reason)


class WorkspaceModelTests(unittest.TestCase):
    def test_candidate_requires_only_title_then_commitment_requires_criteria(self):
        current = initial_workspace()
        body = editable(current)
        body["tasks"].append({"title": "稍后研究一个问题"})
        saved = normalize_and_validate(current, body, "")
        item = saved["tasks"][-1]
        self.assertEqual(item["state"], "proposed")
        self.assertEqual(item["source_ref"], "手工输入")
        self.assertTrue(item["id"])
        self.assertEqual(saved["revision"], 1)
        self.assertTrue(saved["changes"][-1]["reason"])
        body = editable(saved)
        body["tasks"][-1]["state"] = "todo"
        with self.assertRaisesRegex(ValidationError, "完成条件"):
            normalize_and_validate(saved, body, "")
        body["tasks"][-1]["completion_criteria"] = "写出可核对的答案"
        body["tasks"][-1]["goal_id"] = saved["projects"][0]["current_goal_id"]
        later = normalize_and_validate(saved, body, "")
        self.assertIn(item["id"], derived_view(later)["ready_task_ids"])

    def test_dependency_completion_cancellation_and_cycle(self):
        current = initial_workspace()
        first, second, third = [task["id"] for task in current["tasks"]]
        self.assertEqual(derived_view(current)["ready_task_ids"], [first])
        body = editable(current)
        body["tasks"][1]["state"] = "doing"
        with self.assertRaisesRegex(ValidationError, "前置任务"):
            normalize_and_validate(current, body, "")
        body = editable(current)
        body["tasks"][0]["depends_on"] = [third]
        with self.assertRaisesRegex(ValidationError, "循环"):
            normalize_and_validate(current, body, "")
        body = editable(current)
        body["tasks"][0]["state"] = "cancelled"
        cancelled = normalize_and_validate(current, body, "本轮放弃")
        self.assertNotIn(second, derived_view(cancelled)["ready_task_ids"])
        body = editable(cancelled)
        body["tasks"][0]["state"] = "todo"
        with self.assertRaisesRegex(ValidationError, "需说明原因"):
            normalize_and_validate(cancelled, body, "")

    def test_blocked_requires_obstacle_and_unblock_reason(self):
        current = initial_workspace()
        body = editable(current)
        body["tasks"][0]["state"] = "blocked"
        with self.assertRaisesRegex(ValidationError, "阻碍"):
            normalize_and_validate(current, body, "")
        body["tasks"][0]["block_reason"] = "等待浏览器验收"
        blocked = normalize_and_validate(current, body, "")
        self.assertNotIn(blocked["tasks"][0]["id"], derived_view(blocked)["ready_task_ids"])
        body = editable(blocked)
        body["tasks"][0]["state"] = "todo"
        with self.assertRaisesRegex(ValidationError, "需说明原因"):
            normalize_and_validate(blocked, body, "")
        unblocked = normalize_and_validate(blocked, body, "验收已完成")
        self.assertIn(unblocked["tasks"][0]["id"], derived_view(unblocked)["ready_task_ids"])

    def test_actual_remains_after_reschedule_and_upstream_reopen_warns(self):
        with tempfile.TemporaryDirectory() as folder, WorkspaceStore(Path(folder) / "state.json") as store:
            first, second = [task["id"] for task in store.read()["tasks"][:2]]
            body = editable(store.read())
            body["time_blocks"].append({"task_id": first, "planned_start": "2026-09-25T15:00:00+08:00",
                                        "planned_end": "2026-09-25T16:00:00+08:00"})
            saved = store.save(0, body)
            block_id = saved["workspace"]["time_blocks"][0]["id"]
            body = editable(store.read())
            body["work_logs"].append({"task_id": first, "block_id": block_id,
                                      "actual_start": "2026-09-25T15:05:00+08:00",
                                      "actual_end": "2026-09-25T15:42:00+08:00", "result": "完成了可靠保存测试"})
            saved = store.save(1, body)
            actual = copy.deepcopy(saved["workspace"]["work_logs"][0])
            body = editable(store.read())
            body["time_blocks"][0]["planned_start"] = "2026-09-25T17:00:00+08:00"
            body["time_blocks"][0]["planned_end"] = "2026-09-25T18:00:00+08:00"
            saved = store.save(2, body, "验证改期")
            self.assertEqual(saved["workspace"]["work_logs"][0], actual)
            self.assertEqual(saved["workspace"]["changes"][-1]["old"]["planned_start"], "2026-09-25T15:00:00+08:00")
            advance(store, first, "doing")
            with self.assertRaisesRegex(ValidationError, "完成前需核对"):
                advance(store, first, "done", result="完成", checked=False)
            advance(store, first, "done", result="全部完成", checked=True)
            self.assertIn(second, derived_view(store.read())["ready_task_ids"])
            advance(store, second, "doing")
            advance(store, first, "todo", reason="重新打开，发现遗漏")
            warning_ids = [item["task_id"] for item in derived_view(store.read())["warnings"] if item["kind"] == "upstream_reopened"]
            self.assertIn(second, warning_ids)

    def test_remove_plan_keeps_actual_after_unlinking(self):
        current = initial_workspace()
        task_id = current["tasks"][0]["id"]
        body = editable(current)
        body["time_blocks"].append({"task_id": task_id, "planned_start": "2026-09-25T15:00:00+08:00",
                                    "planned_end": "2026-09-25T16:00:00+08:00"})
        scheduled = normalize_and_validate(current, body, "")
        block_id = scheduled["time_blocks"][0]["id"]
        body = editable(scheduled)
        body["work_logs"].append({"task_id": task_id, "block_id": block_id,
                                  "actual_start": "2026-09-25T15:10:00+08:00",
                                  "actual_end": "2026-09-25T15:40:00+08:00", "result": "实际完成"})
        logged = normalize_and_validate(scheduled, body, "")
        body = editable(logged)
        body["time_blocks"] = []
        body["work_logs"][0]["block_id"] = None
        updated = normalize_and_validate(logged, body, "移除安排但保留实际")
        self.assertEqual(updated["time_blocks"], [])
        self.assertEqual(updated["work_logs"][0]["result"], "实际完成")
        self.assertEqual(updated["work_logs"][0]["actual_start"], "2026-09-25T15:10:00+08:00")
        self.assertEqual(updated["work_logs"][0]["block_id"], None)
        self.assertTrue(any(change["entity"] == "time_block" and change["new"] is None
                            for change in updated["changes"]))

    def test_goal_definition_change_requires_reason_and_invalid_state_is_reported(self):
        current = initial_workspace()
        body = editable(current)
        body["goals"][0]["target_state"] += " 通过真实任务验证。"
        with self.assertRaisesRegex(ValidationError, "简短说明"):
            normalize_and_validate(current, body, "")
        changed = normalize_and_validate(current, body, "通过真实任务验证目标")
        self.assertEqual(changed["changes"][-1]["reason"], "通过真实任务验证目标")
        self.assertEqual(changed["goals"][0]["definition_version"], 2)
        body = editable(current)
        body["tasks"][0]["state"] = ["doing"]
        with self.assertRaisesRegex(ValidationError, "状态无效"):
            normalize_and_validate(current, body, "")

    def test_invalid_refs_and_modified_history_fail(self):
        current = initial_workspace()
        body = editable(current)
        body["time_blocks"].append({"task_id": "missing", "planned_start": "2026-09-25T15:00:00+08:00",
                                    "planned_end": "2026-09-25T16:00:00+08:00"})
        with self.assertRaisesRegex(ValidationError, "引用"):
            normalize_and_validate(current, body, "")
        with self.assertRaisesRegex(ValidationError, "只能包含"):
            normalize_and_validate(current, {**editable(current), "changes": []}, "")
        body = editable(current)
        body["tasks"].pop()
        with self.assertRaisesRegex(ValidationError, "不能删除"):
            normalize_and_validate(current, body, "")


class WorkspaceStoreTests(unittest.TestCase):
    def test_revision_conflict_and_restart_preserve_state(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "workspace.json"
            with WorkspaceStore(path) as store:
                old = store.read()
                body = editable(old)
                body["tasks"].append({"title": "保存后重开"})
                changed = store.save(0, body)
                self.assertEqual(changed["workspace"]["revision"], 1)
                with self.assertRaises(ConflictError) as conflict:
                    store.save(0, body)
                self.assertEqual(conflict.exception.revision, 1)
                with self.assertRaises(OSError):
                    with WorkspaceStore(path):
                        pass
            with WorkspaceStore(path) as reopened:
                self.assertEqual(reopened.read()["tasks"][-1]["title"], "保存后重开")
                self.assertEqual(reopened.read()["revision"], 1)

    def test_atomic_failure_preserves_previous_bytes(self):
        with tempfile.TemporaryDirectory() as folder, WorkspaceStore(Path(folder) / "workspace.json") as store:
            before = store.path.read_bytes()
            body = editable(store.read())
            body["tasks"].append({"title": "模拟磁盘失败"})
            real_write = store._write_atomically
            def fail_on_main(target, value):
                if target == store.path:
                    raise OSError("simulated disk failure")
                return real_write(target, value)
            with patch.object(store, "_write_atomically", side_effect=fail_on_main):
                with self.assertRaises(StorageError):
                    store.save(0, body)
            self.assertEqual(store.path.read_bytes(), before)
            self.assertEqual(store.read()["revision"], 0)
            self.assertEqual(json.loads(store.backup.read_text())["revision"], 0)

    def test_explicit_restore_keeps_corrupt_preimage(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "workspace.json"
            with WorkspaceStore(path) as store:
                body = editable(store.read())
                body["tasks"].append({"title": "一次真实修改"})
                store.save(0, body)
                self.assertEqual(store.read()["revision"], 1)
                with self.assertRaisesRegex(StorageError, "先停止服务"):
                    restore_backup(path)
            path.write_text("{broken", encoding="utf-8")
            revision, old = restore_backup(path)
            self.assertEqual(revision, 0)
            self.assertEqual(old.read_text(), "{broken")
            with WorkspaceStore(path) as reopened:
                self.assertEqual(reopened.read()["revision"], 0)
            with self.assertRaisesRegex(StorageError, "当前账本仍有效"):
                restore_backup(path)

    def test_corruption_stops_startup_and_keeps_file(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "workspace.json"
            path.write_text("{broken", encoding="utf-8")
            with self.assertRaisesRegex(StorageError, "校验失败"):
                WorkspaceStore(path)
            self.assertEqual(path.read_text(), "{broken")
            path.unlink()
            (Path(folder) / "workspace.json.bak").write_text("{}")
            with self.assertRaisesRegex(StorageError, "显式恢复"):
                WorkspaceStore(path)
            self.assertFalse(path.exists())


class ApiTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.store = WorkspaceStore(Path(self.temp.name) / "workspace.json")
        views = Path(self.temp.name) / "views"
        views.mkdir()
        (views / "index.html").write_text('life-projects <meta name="ledger-revision" content="0">', encoding="utf-8")
        self.server = LocalServer(0, self.store, views)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.url = f"http://127.0.0.1:{self.server.server_port}"

    def tearDown(self):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join()
        self.store.close()
        self.temp.cleanup()

    def request(self, path, *, method="GET", body=None, origin=None, content_type="application/json"):
        headers = {}
        if origin is not None:
            headers["Origin"] = origin
        if body is not None:
            headers["Content-Type"] = content_type
        req = Request(self.url + path, data=json.dumps(body).encode() if body is not None else None,
                      headers=headers, method=method)
        try:
            response = urlopen(req, timeout=3)
        except HTTPError as error:
            response = error
        content = response.read()
        response.close()
        if response.headers.get("Content-Type", "").startswith("application/json"):
            content = json.loads(content)
        return response.code, content

    def test_static_boundary_and_same_origin_write(self):
        code, index = self.request("/")
        self.assertEqual(code, 200)
        self.assertIn(b"life-projects", index)
        self.assertEqual(self.request("/editor/")[0], 200)
        for path in ("/data/workspace.json", "/../data/workspace.json", "/experiments/voice-intake-2026-09-24/review.html"):
            self.assertEqual(self.request(path)[0], 404)
        current = self.store.read()
        body = editable(current)
        body["tasks"].append({"title": "本地写入"})
        payload = {"expected_revision": 0, "workspace": body, "reason": ""}
        self.assertEqual(self.request("/api/workspace", method="PUT", body=payload)[0], 403)
        self.assertEqual(self.request("/api/workspace", method="PUT", body=payload, origin="http://evil.example")[0], 403)
        code, result = self.request("/api/workspace", method="PUT", body=payload, origin=self.url)
        self.assertEqual(code, 200)
        self.assertEqual(result["workspace"]["revision"], 1)
        self.assertEqual(self.request("/api/workspace", method="PUT", body=payload, origin=self.url)[0], 409)
        self.assertEqual(self.request("/api/workspace")[1]["workspace"]["tasks"][-1]["title"], "本地写入")
        self.assertEqual(self.request("/")[0], 200)  # reader stays available; freshness is reported by reading API


if __name__ == "__main__":
    unittest.main()
