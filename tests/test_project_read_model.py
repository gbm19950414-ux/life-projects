import copy
import hashlib
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from app.execplan_progress import block, publication
from app.pilot import initial_record
from app.project_read_model import read_sources
from app import project_read_model as model


class ProjectReadModelTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.config = self.root / "reader-sources.json"
        self.job = self.root / "job.json"
        self.material = self.root / "resume.md"
        self.plan = self.root / "plan.md"
        self.job.write_text(json.dumps(initial_record(), ensure_ascii=False))
        self.material.write_text("# 简历基准\n\n## 当前更新（2026-10-09）\n\n事实已更新到 r3；PDF 仍为 r2 历史成品，未重新生成。\n计划待执行，不表示已投递。\n")
        self.plan_record = {"schema": "execplan-progress/v1", "plan_id": "life-dot-entry", "project_id": "life-projects", "title": "接入计划", "objective": "能真实使用", "execution_status": "active", "assessment": {"outcome": "insufficient_evidence", "summary": "真实用途待验证", "evidence": []}, "milestones": [{"id": "m1", "title": "技术测试", "state": "done", "depends_on": [], "evidence": ["隔离测试"]}], "next_action": "实际使用反馈", "updated_at": "2026-10-09T00:00:00Z", "published_sha256": ""}
        self.plan.write_text(self.plan_text())
        self.sources = [self.spec("career", "pilot", self.job.name), self.spec("resume", "markdown", self.material.name), self.spec("dot-plan", "execplan", self.plan.name, project="life-projects")]
        self.write_config()

    def spec(self, ident, kind, path, project="personal-development"):
        return {"id": ident, "kind": kind, "project_id": project, "project_title": project, "title": ident, "root": str(self.root), "path": path}

    def plan_text(self):
        return publication("# Plan\n\n## Progress\n\n" + block(self.plan_record) + "\n\n## Outcomes & Retrospective\n\n真实用途待验证。\n")

    def write_config(self):
        self.config.write_text(json.dumps({"schema_version": 1, "sources": self.sources, "not_connected": [{"id": "voice", "title": "录音原管道", "reason": "独立范围，未读取"}]}, ensure_ascii=False))

    def state(self, value, ident):
        return next(item for item in value["source_states"] if item["plan_id"] == ident)

    def test_readonly_no_writer_renderer_or_subprocess(self):
        before = {p.name: p.read_bytes() for p in self.root.iterdir()}
        with patch("app.pilot.apply", side_effect=AssertionError("write")), patch("app.pilot.render", side_effect=AssertionError("render")), patch("app.live_sync.SourceSync.refresh", side_effect=AssertionError("sync")), patch("app.demands.sync", side_effect=AssertionError("sync")), patch("subprocess.run", side_effect=AssertionError("spawn")):
            result = read_sources(self.config)
        self.assertEqual({p.name: p.read_bytes() for p in self.root.iterdir()}, before)
        self.assertEqual({s["status"] for s in result["source_states"]}, {"synced"})
        self.assertEqual({p["id"] for p in result["projects"]}, {"life-projects", "personal-development"})
        self.assertEqual({v["record_kind"] for v in result["views"]}, {"pilot_goal", "implementation_plan", "source_material"})
        self.assertTrue(all(v["flow_key"] is None and v["full_key"] is None for v in result["views"]))
        self.assertIn("not_connected", {x["status"] for x in result["coverage"]})

    def test_navigation_resolves_every_detail_and_preserves_original_states(self):
        value = read_sources(self.config)
        for project in value["projects"]:
            self.assertIn(project["node"], value["nodes"])
            self.assertEqual(value["navigation"][project["node"]]["project_id"], project["id"])
        for view in value["views"]:
            for node_id in view["detail_node_ids"]:
                self.assertIn(node_id, value["nodes"])
                self.assertEqual(value["navigation"][node_id]["view_id"], view["id"])
        action = value["nodes"]["pilot:personal-development:action:map-existing-experience"]
        goal = value["nodes"]["pilot:personal-development:goal:accepted-suitable-offer"]
        milestone = value["nodes"]["execplan:life-dot-entry:milestone:m1"]
        self.assertEqual(action["state"], "proposed")
        self.assertEqual(goal["assessment_outcome"], "unassessed")
        self.assertEqual(milestone["state"], "done")
        self.assertEqual(milestone["kind"], "implementation_milestone")
        self.assertIn("不推断当天容量", str(goal["fields"]))

    def test_material_original_text_contains_current_and_historical_boundary(self):
        value = read_sources(self.config)
        material = value["nodes"]["material:personal-development:resume"]
        fields = dict(material["fields"])
        self.assertIn("5: 事实已更新到 r3；PDF 仍为 r2 历史成品", fields["材料原文（按行）"])
        self.assertIn("计划待执行，不表示已投递", fields["材料原文（按行）"])
        self.assertEqual(fields["来源 SHA-256"], hashlib.sha256(self.material.read_bytes()).hexdigest())
        self.assertEqual(material["reconciliation"], "not_evaluated")
        self.assertFalse(material["excerpt_truncated"])

    def test_business_parent_current_and_source_identity_survive_projection(self):
        data = json.loads(self.job.read_text())
        data['current_state']['stage_id'] = 'explore'
        self.job.write_text(json.dumps(data))
        value = read_sources(self.config)
        goal_id = 'pilot:personal-development:goal:accepted-suitable-offer'
        stage_id = 'pilot:personal-development:stage:explore'
        action_id = 'pilot:personal-development:action:map-existing-experience'
        goal, stage, action = [value['nodes'][x] for x in (goal_id, stage_id, action_id)]
        self.assertEqual(goal['parent_id'], 'personal-development')
        self.assertEqual(goal['source_project_id'], data['project']['id'])
        self.assertEqual(stage['parent_id'], goal_id)
        self.assertTrue(stage['is_current'])
        self.assertEqual(action['parent_id'], stage_id)
        for source_stage in data['stages']:
            projected = value['nodes']['pilot:personal-development:stage:' + source_stage['id']]
            self.assertEqual(projected['depends_on'], ['pilot:personal-development:stage:' + x for x in source_stage['depends_on']])

    @patch('app.reader._render_svg', return_value=b'<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 10 10"/>')
    def test_merged_directory_contains_business_stages_actions_materials_and_same_project_plan(self, render):
        from app.reader import build_bundle
        from app.model import initial_workspace
        from test_graphs import CONTEXT
        data = json.loads(self.job.read_text())
        data['current_state']['stage_id'] = 'explore'
        self.job.write_text(json.dumps(data))
        extra = read_sources(self.config)
        bundle = build_bundle(initial_workspace(), CONTEXT, supplement=extra)
        model = bundle['reading_model']
        self.assertEqual(model['project_ids'], ['life-projects', 'personal-development'])
        obj = model['objects']
        goal = 'pilot:personal-development:goal:accepted-suitable-offer'
        stage = 'pilot:personal-development:stage:explore'
        action = 'pilot:personal-development:action:map-existing-experience'
        self.assertEqual(obj[action]['parent_id'], stage)
        self.assertIn(action, obj[stage]['children'])
        self.assertNotIn(action, obj[goal]['children'])
        self.assertEqual(obj[goal]['source_project_id'], data['project']['id'])
        self.assertEqual(obj[action]['source_version'], extra['source_versions']['career'])
        pd = next(p for p in bundle['projects'] if p['id'] == 'personal-development')
        self.assertEqual(pd['reading']['current_stage_id'], stage)
        self.assertEqual(len(pd['reading']['business_stage_ids']), len(data['stages']))
        self.assertEqual(pd['reading']['material_ids'], ['material:personal-development:resume'])
        self.assertEqual(obj['execplan:life-dot-entry']['parent_id'], 'life-projects')
        self.assertEqual(obj['execplan:life-dot-entry:milestone:m1']['parent_id'], 'execplan:life-dot-entry')

    def test_material_changed_without_job_revision_changes_fingerprint_not_actions(self):
        before = read_sources(self.config)
        self.material.write_text(self.material.read_text() + "\n新增待核对材料。\n")
        after = read_sources(self.config, previous=before)
        self.assertNotEqual(before["fingerprint_inputs"], after["fingerprint_inputs"])
        self.assertEqual(before["source_versions"]["career"], after["source_versions"]["career"])
        self.assertEqual(before["projections"]["career"], after["projections"]["career"])
        self.assertEqual(set(before["navigation"]), set(after["navigation"]))

    def test_checked_time_not_part_of_fingerprint(self):
        with patch.object(model, "_now", return_value="2026-10-09T00:00:00Z"):
            before = read_sources(self.config)
        with patch.object(model, "_now", return_value="2026-10-09T00:05:00Z"):
            after = read_sources(self.config, previous=before)
        self.assertNotEqual(before["source_states"][0]["checked_at"], after["source_states"][0]["checked_at"])
        self.assertEqual(before["fingerprint_inputs"], after["fingerprint_inputs"])

    def test_missing_material_retains_only_its_previous_projection(self):
        before = read_sources(self.config)
        self.material.unlink()
        after = read_sources(self.config, previous=before)
        self.assertEqual(self.state(after, "resume")["status"], "unavailable")
        self.assertEqual(self.state(after, "career")["status"], "synced")
        self.assertEqual(after["source_versions"]["resume"], before["source_versions"]["resume"])
        self.assertIn("上次可靠内容", str(after["nodes"]["material:personal-development:resume"]["fields"]))
        self.assertEqual(next(p for p in after["projects"] if p["id"] == "personal-development")["freshness"], "partial")

    def test_invalid_business_record_isolated_and_no_empty_initialization(self):
        before = read_sources(self.config)
        self.job.write_text("{broken")
        after = read_sources(self.config, previous=before)
        self.assertEqual(self.state(after, "career")["status"], "pending")
        self.assertEqual(self.state(after, "dot-plan")["status"], "synced")
        self.assertEqual(after["source_versions"]["career"], before["source_versions"]["career"])
        self.assertEqual(self.job.read_text(), "{broken")
        fresh = read_sources(self.config)
        self.assertNotIn("career", fresh["projections"])
        self.assertIn("personal-development", {p["id"] for p in fresh["projects"]})

    def test_unpublished_execplan_does_not_restate_new_fact(self):
        before = read_sources(self.config)
        self.plan.write_text(self.plan.read_text().replace("实际使用反馈", "已全部完成"))
        after = read_sources(self.config, previous=before)
        self.assertEqual(self.state(after, "dot-plan")["status"], "pending")
        plan = next(v for v in after["views"] if v["record_kind"] == "implementation_plan")
        self.assertEqual(plan["next"], "实际使用反馈")
        self.assertIn("上次可靠内容", plan["status"])
        self.assertNotEqual(self.state(after, "dot-plan")["raw_sha256"], self.state(after, "dot-plan")["rendered_version"])

    def test_file_race_keeps_previous_bytes_and_healthy_source(self):
        before = read_sources(self.config)
        original = model._read_source
        calls = []
        def racing(spec):
            raw, path = original(spec)
            if spec["id"] == "resume":
                calls.append(spec["id"])
                if len(calls) == 2:
                    return raw + b"race", path
            return raw, path
        with patch.object(model, "_read_source", side_effect=racing):
            after = read_sources(self.config, previous=before)
        self.assertEqual(self.state(after, "resume")["status"], "checking")
        self.assertEqual(self.state(after, "career")["status"], "synced")
        self.assertEqual(after["projections"]["resume"], before["projections"]["resume"])

    def test_later_material_read_changes_earlier_business_source(self):
        before = read_sources(self.config)
        original = model._read_source
        calls = []
        def racing(spec):
            raw, path = original(spec)
            calls.append(spec["id"])
            if spec["id"] == "resume" and calls.count("resume") == 2:
                data = json.loads(self.job.read_text())
                data["revision"] += 1
                self.job.write_text(json.dumps(data))
            return raw, path
        with patch.object(model, "_read_source", side_effect=racing):
            after = read_sources(self.config, previous=before)
        self.assertEqual(self.state(after, "career")["status"], "checking")
        self.assertEqual(self.state(after, "resume")["status"], "synced")
        self.assertEqual(self.state(after, "dot-plan")["status"], "synced")
        self.assertEqual(after["projections"]["career"], before["projections"]["career"])
        self.assertNotEqual(self.state(after, "career")["raw_sha256"], self.state(after, "career")["rendered_version"])
        self.assertEqual(calls.count("career"), 3)  # Bounded final sweep, not retries.

    def test_cross_source_race_without_previous_does_not_promote_old_projection(self):
        original = model._read_source
        changed = False
        def racing(spec):
            nonlocal changed
            raw, path = original(spec)
            if spec["id"] == "resume" and not changed:
                self.job.write_text(self.job.read_text() + " ")
                changed = True
            return raw, path
        with patch.object(model, "_read_source", side_effect=racing):
            after = read_sources(self.config)
        self.assertEqual(self.state(after, "career")["status"], "checking")
        self.assertEqual(self.state(after, "career")["rendered_version"], "")
        self.assertNotIn("career", after["projections"])
        self.assertEqual(self.state(after, "resume")["status"], "synced")

    def test_end_sweep_missing_file_is_local_failure(self):
        before = read_sources(self.config)
        original = model._read_source
        count = 0
        def racing(spec):
            nonlocal count
            if spec["id"] == "career":
                count += 1
                if count == 3:
                    self.job.unlink()
            return original(spec)
        with patch.object(model, "_read_source", side_effect=racing):
            after = read_sources(self.config, previous=before)
        self.assertEqual(self.state(after, "career")["status"], "unavailable")
        self.assertEqual(after["projections"]["career"], before["projections"]["career"])
        self.assertEqual(self.state(after, "resume")["status"], "synced")

    def test_config_unavailable_keeps_previous_without_opening_business(self):
        before = read_sources(self.config)
        self.config.unlink()
        with patch.object(model, "_read_source", side_effect=AssertionError("old config paths must not be used")):
            after = read_sources(self.config, previous=before)
        self.assertEqual(before["source_versions"], after["source_versions"])
        self.assertTrue(all(item["status"] == "unavailable" for item in after["source_states"]))
        self.assertIn("voice", {x["id"] for x in after["coverage"]})

    def test_invalid_config_keeps_previous_and_does_not_raise(self):
        before = read_sources(self.config)
        self.config.write_text("{bad")
        after = read_sources(self.config, previous=before)
        self.assertEqual(after["source_versions"], before["source_versions"])
        self.assertIn("registry_error", after["fingerprint_inputs"])

    def test_changed_registration_does_not_reuse_other_path(self):
        before = read_sources(self.config)
        self.sources[1]["path"] = "not-present.md"
        self.write_config()
        after = read_sources(self.config, previous=before)
        self.assertNotIn("resume", after["projections"])
        self.assertEqual(self.state(after, "resume")["rendered_version"], "")

    def test_path_escape_and_symlink_escape_not_read(self):
        outside = self.root.parent / (self.root.name + "-outside.md")
        outside.write_text("must not read")
        self.addCleanup(lambda: outside.unlink(missing_ok=True))
        (self.root / "escape.md").symlink_to(outside)
        self.sources += [self.spec("escape", "markdown", "escape.md"), self.spec("traversal", "markdown", "../" + outside.name)]
        self.write_config()
        value = read_sources(self.config)
        for ident in ("escape", "traversal"):
            self.assertEqual(self.state(value, ident)["status"], "pending")
            self.assertNotIn(ident, value["projections"])
        self.assertEqual(self.state(value, "career")["status"], "synced")

    def test_unlisted_files_and_links_are_not_followed(self):
        (self.root / "not-registered.md").write_text("not part of reading")
        self.material.write_text(self.material.read_text() + "\n[外部来源](https://example.invalid/)\n[另一材料](not-registered.md)\n")
        original = model._read_source
        names = []
        def track(spec):
            names.append(spec["path"])
            return original(spec)
        with patch.object(model, "_read_source", side_effect=track):
            value = read_sources(self.config)
        self.assertNotIn("not-registered.md", names)
        self.assertEqual(len(value["projections"]), 3)

    def test_size_and_excerpt_limits_visible_without_blocking_other_source(self):
        self.material.write_text("# 材料\n" + "一行原文\n" * 5000)
        value = read_sources(self.config)
        node = value["nodes"]["material:personal-development:resume"]
        self.assertTrue(node["excerpt_truncated"])
        self.assertIn("余文未展示", dict(node["fields"])["摘录范围"])
        self.material.write_bytes(b"x" * (model.MAX_SOURCE_BYTES + 1))
        after = read_sources(self.config, previous=value)
        self.assertEqual(self.state(after, "resume")["status"], "pending")
        self.assertEqual(self.state(after, "career")["status"], "synced")

    def test_registry_race_preserves_previous_and_marks_all_checking(self):
        before = read_sources(self.config)
        original = model._bounded
        count = 0
        def changed(path, limit):
            nonlocal count
            raw = original(path, limit)
            if Path(path) == self.config:
                count += 1
                if count == 2:
                    return raw + b" "
            return raw
        with patch.object(model, "_bounded", side_effect=changed):
            after = read_sources(self.config, previous=before)
        self.assertEqual(before["projections"], after["projections"])
        self.assertTrue(all(s["status"] == "checking" for s in after["source_states"]))
        self.assertEqual(before["source_versions"], after["source_versions"])


if __name__ == "__main__":
    unittest.main()
