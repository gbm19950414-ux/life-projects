import copy
import hashlib
import json
from pathlib import Path
import tempfile
import threading
import unittest
from unittest.mock import patch

from app.pilot import (PilotError, apply, day_report, initial_record, initialize,
                       overview_puml, read, ready, recover, render, validate)


class PilotTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.path = (Path(self.temp.name) / "job.json").resolve()
        initialize(self.path)
        self.day = "2026-10-08"

    def request(self, operations, revision=None, source="fixture:user-turn"):
        return {"expected_revision": revision or read(self.path)["revision"], "reason": "隔离试用：安排/反馈/修订", "source_ref": source, "operations": operations}

    def change(self, *ops, revision=None, source="fixture:user-turn"):
        return apply(self.path, self.request(list(ops), revision, source))

    def capacity(self, minutes=60):
        return {"type": "set_capacity", "date": self.day, "periods": {"afternoon": minutes}, "fixed": []}

    def schedule(self, minutes=30):
        return {"type": "set_schedule", "date": self.day, "entries": [{"action_id": "map-existing-experience", "period": "afternoon", "minutes": minutes, "role": "primary"}]}

    def feedback(self, source_id="fixture:feedback-1", state="done", minutes=20):
        return {"type": "record_feedback", "source_id": source_id, "action_id": "map-existing-experience", "result": "隔离试用：已经有经历摘要", "action_state": state, "actual_minutes": minutes, "evidence": ["隔离试用产物"]}

    def test_reopen_unknowns_and_no_capacity_are_honest(self):
        original = self.path.read_bytes()
        data = read(self.path)
        report = day_report(data, self.day)
        self.assertIn("行动建议，不是已排好的日程", report)
        self.assertIn("全景", report)
        self.assertIn("尚未核对", report)
        self.assertIn("约 30 分钟（可修订估时）", report)
        self.assertEqual(data["goal"]["assessment"]["outcome"], "unassessed")
        self.assertTrue(data["goal"]["unknowns"])
        self.assertEqual(read(self.path), data)
        self.assertEqual(self.path.read_bytes(), original)
        missing = self.path.with_name("missing.json")
        with self.assertRaises(PilotError): read(missing)
        self.assertFalse(missing.exists())
        with self.assertRaises(PilotError): initialize(self.path)

    def test_invalid_ids_dependencies_and_versions_do_not_write(self):
        original = self.path.read_bytes()
        for operation in [
            {"type": "update_action", "action_id": "missing", "changes": {"state": "todo"}},
            {"type": "update_action", "action_id": "map-existing-experience", "changes": {"depends_on": ["map-existing-experience"]}},
            {"type": "update_action", "action_id": "map-existing-experience", "changes": {"depends_on": ["missing"]}},
            {"type": "update_action", "action_id": "map-existing-experience", "changes": {"state": "done"}},
            {"type": "update_action", "action_id": "map-existing-experience", "changes": {"state": "blocked"}},
        ]:
            with self.assertRaises(PilotError): self.change(operation)
            self.assertEqual(self.path.read_bytes(), original)
        bad = read(self.path); bad["revision"] = True
        with self.assertRaises(PilotError): validate(bad)
        self.change({"type": "update_action", "action_id": "map-existing-experience", "changes": {"state": "todo"}})
        latest = self.path.read_bytes()
        with self.assertRaises(PilotError): self.change(self.capacity(), revision=1)
        self.assertEqual(self.path.read_bytes(), latest)

    def test_schedule_respects_capacity_and_actual_survives_reschedule(self):
        with self.assertRaises(PilotError): self.change(self.schedule())
        self.change(self.capacity(), self.schedule())
        self.change(self.feedback(state="doing"))
        before_actual = copy.deepcopy(read(self.path)["actuals"])
        self.change(self.schedule(40))
        self.assertEqual(read(self.path)["actuals"], before_actual)
        self.assertIn("40 分钟", day_report(read(self.path), self.day))
        before = self.path.read_bytes()
        with self.assertRaises(PilotError): self.change(self.schedule(61))
        with self.assertRaises(PilotError): self.change(self.capacity(10))
        self.assertEqual(self.path.read_bytes(), before)
        self.assertEqual(read(self.path.with_suffix('.json.bak'))["actuals"], before_actual)
        self.assertNotIn("40 分钟", day_report(read(self.path), "2026-10-09"))

    def test_feedback_deduplication_and_goal_result_are_separate(self):
        feedback = self.feedback()
        self.change(feedback)
        original = self.path.read_bytes()
        result = self.change(feedback)
        self.assertFalse(result["saved"])
        self.assertEqual(self.path.read_bytes(), original)
        self.assertEqual(len(read(self.path)["actuals"]), 1)
        self.assertEqual(read(self.path)["goal"]["assessment"]["outcome"], "unassessed")
        conflict = copy.deepcopy(feedback); conflict["result"] = "另一种结果"
        with self.assertRaises(PilotError): self.change(conflict)
        self.assertEqual(self.path.read_bytes(), original)

    def test_day_order_and_all_changes_since_seen_revision(self):
        second = copy.deepcopy(read(self.path)["actions"][0])
        second.update(id="followup", title="隔离试用：后续行动", depends_on=["map-existing-experience"])
        self.change({"type": "add_action", "action": second})
        self.change({"type": "set_capacity", "date": self.day, "periods": {"morning": 30, "afternoon": 30}, "fixed": []})
        entries = [{"action_id": "map-existing-experience", "period": "afternoon", "minutes": 30, "role": "primary"}, {"action_id": "followup", "period": "morning", "minutes": 30, "role": "optional"}]
        with self.assertRaises(PilotError): self.change({"type": "set_schedule", "date": self.day, "entries": entries})
        entries[0]["period"], entries[1]["period"] = "morning", "afternoon"
        self.change({"type": "set_schedule", "date": self.day, "entries": entries})
        report = day_report(read(self.path), self.day, since_revision=1)
        for version in (2, 3, 4): self.assertIn(f"- r{version}：", report)
        self.assertIn("前置未完成", report)
        self.assertIn("无新保存变化", day_report(read(self.path), self.day, since_revision=4))
        with self.assertRaises(PilotError): day_report(read(self.path), self.day, since_revision=5)

    def test_goal_revision_keeps_prior_definition_and_assessment(self):
        self.change({"type": "assess_goal", "outcome": "insufficient_evidence", "summary": "隔离试用：尚无证据", "evidence": []})
        initial = copy.deepcopy(read(self.path)["goal"])
        self.change({"type": "revise_goal", "changes": {"criteria": ["隔离试用：新完成条件"]}})
        goal = read(self.path)["goal"]
        self.assertEqual(goal["id"], initial["id"])
        self.assertEqual(goal["version"], 2)
        self.assertEqual(goal["assessment"]["outcome"], "unassessed")
        self.assertEqual(goal["history"][0]["assessment"], initial["assessment"])
        self.assertEqual(goal["history"][0]["criteria"], initial["criteria"])
        with self.assertRaises(PilotError): self.change({"type": "assess_goal", "outcome": "met", "summary": "没有证据", "evidence": []})

    def test_observation_trial_confirmation_and_project_override(self):
        self.change({"type": "observe", "id": "one-session", "text": "隔离试用：这一次只用了20分钟"},
                    {"type": "trial_preference", "id": "smaller", "key": "task_max_minutes", "value": 20, "rationale": "隔离试用：试拆小任务"})
        settings = read(self.path)["settings"]
        self.assertEqual(settings["confirmed"], {})
        self.assertEqual(settings["trials"][0]["status"], "trial")
        self.assertIn("试调（未成为长期偏好）", day_report(read(self.path), self.day))
        self.assertIn("先讨论拆分", day_report(read(self.path), self.day))
        with self.assertRaises(PilotError): self.change({"type": "confirm_preference", "trial_id": "smaller", "confirmation_ref": ""})
        self.change({"type": "confirm_preference", "trial_id": "smaller", "confirmation_ref": "fixture:explicit-human-confirmation"})
        self.change({"type": "set_preference", "key": "task_max_minutes", "value": 25, "scope": "project_overrides", "confirmation_ref": "fixture:project-specific-rule"})
        self.assertIn("25 分钟", day_report(read(self.path), self.day))
        self.change({"type": "set_preference", "key": "task_max_minutes", "value": None, "scope": "project_overrides", "confirmation_ref": "fixture:clear-rule"})
        self.assertIn("20 分钟", day_report(read(self.path), self.day))

    def test_source_failure_is_visible_and_pausing_does_not_delete_history(self):
        self.change(self.capacity(), self.schedule())
        self.change({"type": "check_feedback_source", "status": "unavailable", "detail": "隔离试用：来源连接不可用"})
        report = day_report(read(self.path), self.day)
        self.assertIn("unavailable", report)
        self.assertIn("来源连接不可用", report)
        self.change({"type": "set_decision", "decision": "paused"})
        self.assertEqual(ready(read(self.path)), [])
        self.assertIn("保留的原安排", day_report(read(self.path), self.day))
        with self.assertRaises(PilotError): self.change(self.schedule())

    def test_concurrent_writers_and_atomic_failure_preserve_primary(self):
        barrier = threading.Barrier(2); outcomes = []
        def writer(minutes):
            barrier.wait()
            try:
                self.change(self.capacity(minutes), revision=1); outcomes.append("saved")
            except PilotError:
                outcomes.append("conflict")
        threads = [threading.Thread(target=writer, args=(n,)) for n in (30, 45)]
        for thread in threads: thread.start()
        for thread in threads: thread.join()
        self.assertCountEqual(outcomes, ["saved", "conflict"])
        original = self.path.read_bytes()
        actual_write = __import__('app.pilot', fromlist=['WorkspaceStore']).WorkspaceStore._write_atomically
        def failing(target, value):
            if Path(target) == self.path:
                raise OSError("disk full")
            actual_write(target, value)
        with patch("app.pilot.WorkspaceStore._write_atomically", side_effect=failing):
            with self.assertRaises(OSError): self.change(self.capacity(60))
        self.assertEqual(self.path.read_bytes(), original)
        self.assertEqual(read(self.path.with_suffix('.json.bak')), read(self.path))

    def test_render_cache_failure_and_concurrent_save_keep_reliable_pointer(self):
        output = Path(self.temp.name) / "views"
        svg = b'<svg xmlns="http://www.w3.org/2000/svg"/>'
        png = b'\x89PNG\r\n\x1a\nfixture'
        with patch("app.pilot._render_svg", return_value=svg) as renderer, patch("app.pilot.render_png", return_value=png):
            result = render(self.path, output, self.day)
            pointer = (output / "latest.json").read_bytes()
            self.assertEqual(render(self.path, output, self.day), result)
            self.assertEqual(renderer.call_count, 1)
            self.assertEqual((output / "latest.json").read_bytes(), pointer)
        manifest = json.loads((Path(result["directory"]) / 'manifest.json').read_text())
        self.assertEqual(manifest["source_sha256"], hashlib.sha256(self.path.read_bytes()).hexdigest())
        self.change({"type": "set_current_state", "summary": "隔离试用：有新反馈", "stage_id": "explore"})
        with patch("app.pilot._render_svg", side_effect=RuntimeError('failed')):
            with self.assertRaises(RuntimeError): render(self.path, output, self.day)
        self.assertEqual((output / "latest.json").read_bytes(), pointer)
        def racing(source):
            self.change(self.capacity()); return svg
        with patch("app.pilot._render_svg", side_effect=racing), patch("app.pilot.render_png", return_value=png):
            with self.assertRaises(PilotError): render(self.path, output, self.day)
        self.assertEqual((output / "latest.json").read_bytes(), pointer)
        with patch("app.pilot._render_svg", return_value=svg), patch("app.pilot.render_png", return_value=png):
            refreshed = render(self.path, output, self.day)
        self.assertNotEqual(refreshed['directory'], result['directory'])

    def test_explicit_recovery_preserves_corrupt_preimage(self):
        self.change(self.capacity())
        with self.assertRaises(PilotError): recover(self.path)
        self.path.write_bytes(b"corrupt")
        result = recover(self.path)
        self.assertEqual(result["revision"], 1)
        self.assertTrue(list(self.path.parent.glob('job.json.pre-recover-*')))
        self.assertEqual(read(self.path)["revision"], 1)

    def test_symlink_apply_updates_canonical_record_and_keeps_alias(self):
        alias = self.path.with_name("legacy-job.json")
        alias.symlink_to(self.path.name)
        request = self.request([{"type": "set_current_state", "summary": "别名写入测试", "stage_id": "explore"}])

        result = apply(alias, request)

        self.assertTrue(result["saved"])
        self.assertTrue(alias.is_symlink())
        self.assertEqual(read(alias), read(self.path))
        self.assertEqual(read(self.path)["current_state"]["summary"], "别名写入测试")

    def test_symlink_recovery_uses_canonical_backup_and_preserves_corrupt_preimage(self):
        alias = self.path.with_name("legacy-job.json")
        alias.symlink_to(self.path.name)
        self.change(self.capacity())
        backup = read(self.path.with_suffix(".json.bak"))
        corrupted = b"corrupt canonical preimage"
        self.path.write_bytes(corrupted)

        result = recover(alias)

        self.assertEqual(result["revision"], backup["revision"])
        self.assertTrue(alias.is_symlink())
        self.assertEqual(read(alias), backup)
        self.assertEqual(read(self.path), backup)
        preserved = list(self.path.parent.glob("job.json.pre-recover-*"))
        self.assertEqual(len(preserved), 1)
        self.assertEqual(preserved[0].read_bytes(), corrupted)

    def test_dangling_symlink_read_and_initialize_do_not_create_target(self):
        target = self.path.with_name("canonical-missing.json")
        alias = self.path.with_name("legacy-missing.json")
        alias.symlink_to(target.name)

        with self.assertRaises(PilotError): read(alias)
        with self.assertRaises(PilotError): initialize(alias)

        self.assertTrue(alias.is_symlink())
        self.assertFalse(target.exists())

    def test_initialize_existing_target_and_dangling_backup_are_untouched(self):
        original = self.path.read_bytes()
        with self.assertRaises(PilotError): initialize(self.path)
        self.assertEqual(self.path.read_bytes(), original)

        path = self.path.with_name("backup-link.json")
        backup_target = self.path.with_name("missing-backup.json")
        path.with_suffix(path.suffix + ".bak").symlink_to(backup_target.name)
        with self.assertRaises(PilotError): initialize(path)
        self.assertFalse(path.exists())
        self.assertFalse(backup_target.exists())

    def test_cycle_is_only_feedback_not_dependency_and_labels_cannot_inject_puml(self):
        data = initial_record()
        data["stages"][0]["depends_on"] = ["outreach"]
        data["stages"][2]["depends_on"] = ["explore"]
        with self.assertRaises(PilotError): validate(data)
        data = initial_record()
        data["goal"]["target_state"] = 'literal"\n@enduml\n!include https://example.invalid'
        source = overview_puml(data)
        self.assertEqual(source.count('@enduml'), 1)
        self.assertIn('＠enduml', source)
        self.assertIn('反馈回看', source)
        self.assertIn('虚线：拟议路径', source)


if __name__ == '__main__':
    unittest.main()
