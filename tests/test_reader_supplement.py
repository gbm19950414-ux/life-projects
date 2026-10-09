import copy
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from app.graphs import _alias
from app.model import initial_workspace
from app.reader import ReadingViews, build_bundle, fingerprint
from app.store import WorkspaceStore
from test_graphs import CONTEXT
from test_nested_reader import two_stages
from test_reader import SVG


def supplemental(version="v1", status="synced"):
    item = "pilot:personal-development:action:existing-action"
    view = "pilot:personal-development:goal:existing-goal"
    return {
        "projects": [
            {"id": "life-projects", "title": "Life Projects", "summary": {"next_step": "实施计划等待本人用途"}},
            {"id": "personal-development", "title": "个人发展与求职", "node": "pd", "read_only": True,
             "summary": {"next_step": "正式行动仍在进行", "recent_change": "材料已更新，未自动改行动"}},
        ],
        "views": [{"id": view, "project_id": "personal-development", "record_kind": "pilot_goal",
                   "flow_key": None, "full_key": None, "detail_node_ids": [item]}],
        "nodes": {item: {"title": "进行中的真实行动", "fields": [["状态", "doing"], ["材料", version]]}},
        "navigation": {item: {"project_id": "personal-development", "view_id": view, "node_id": item, "kind": "action"}},
        "source_states": [{"plan_id": "pilot-source", "title": "求职来源", "project_id": "personal-development",
                           "status": status, "raw_sha256": version if status == "synced" else None,
                           "checked_at": "time-one", "message": "" if status == "synced" else "保留上次读取"}],
        "source_versions": {"pilot-source": version},
        "coverage": [{"id": "recordings", "title": "录音原管道", "status": "not_connected", "message": "独立入口"}],
        "fingerprint_inputs": {"source": version, "status": status},
        "projections": {},
    }


class SupplementIntegrationTests(unittest.TestCase):
    @patch("app.reader._render_svg", return_value=SVG)
    def test_merge_keeps_one_local_project_and_source_owned_navigation(self, render):
        workspace, extra = initial_workspace(), supplemental()
        before = copy.deepcopy((workspace, extra))
        bundle = build_bundle(workspace, CONTEXT, supplement=extra)
        self.assertEqual([p["id"] for p in bundle["projects"]], ["life-projects", "personal-development"])
        self.assertFalse(bundle["projects"][0].get("read_only", False))
        self.assertTrue(bundle["projects"][1]["read_only"])
        self.assertEqual(bundle["revision"], workspace["revision"])
        self.assertEqual(bundle["navigation"][next(iter(extra["navigation"]))], next(iter(extra["navigation"].values())))
        self.assertEqual((workspace, extra), before)
        self.assertEqual(render.call_count, 3)  # No invented supplemental graph.
        self.assertIn("not_connected", json.dumps(bundle["coverage"]))

    @patch("app.reader._render_svg", return_value=SVG)
    def test_task_navigation_uses_real_ownership_even_for_cross_stage_prerequisite(self, render):
        workspace = two_stages()
        bundle = build_bundle(workspace, CONTEXT)
        prerequisite = workspace["tasks"][0]
        route = bundle["navigation"][f'local:task:{prerequisite["id"]}']
        self.assertEqual(route["view_id"], "local:goal:historical-stage")
        self.assertEqual(route["node_id"], _alias(prerequisite["id"]))
        self.assertIsNone(bundle["navigation"]["local:task:unassigned-item"]["view_id"])

    def test_checked_time_does_not_change_fingerprint_but_material_hash_does(self):
        workspace, extra = initial_workspace(), supplemental()
        first = fingerprint(workspace, CONTEXT, supplement=extra)
        extra["source_states"][0]["checked_at"] = "time-two"
        self.assertEqual(first, fingerprint(workspace, CONTEXT, supplement=extra))
        extra["fingerprint_inputs"]["source"] = "changed-material"
        self.assertNotEqual(first, fingerprint(workspace, CONTEXT, supplement=extra))

    @patch("app.reader._render_svg", return_value=SVG)
    def test_changed_material_refreshes_without_rerendering_unaffected_graphs(self, render):
        with tempfile.TemporaryDirectory() as folder, WorkspaceStore(Path(folder) / "workspace.json") as store:
            root = Path(folder)
            (root / "context.json").write_text(json.dumps(CONTEXT))
            (root / "reader-sources.json").write_text(json.dumps({'schema_version':1,'sources':[{'id':'pd','kind':'markdown','project_id':'personal-development','project_title':'个人发展与求职','title':'入口','root':str(root/'offline'),'path':'README.md'}],'not_connected':[]}))
            reader = ReadingViews(store, root / "context.json", root / "views")
            canonical = store.path.read_bytes()
            with patch("app.reader.read_sources", return_value=supplemental()):
                old = reader.snapshot()
            render.reset_mock()
            with patch("app.reader.read_sources", return_value=supplemental("new-material")):
                new = reader.snapshot()
            self.assertFalse(new["stale"])
            self.assertNotEqual(old["bundle"]["fingerprint"], new["bundle"]["fingerprint"])
            self.assertEqual(new["bundle"]["revision"], old["bundle"]["revision"])
            self.assertEqual(new["sources"][0]["rendered_version"], "new-material")
            render.assert_not_called()
            self.assertEqual(store.path.read_bytes(), canonical)

    @patch("app.reader._render_svg", return_value=SVG)
    def test_material_change_during_legacy_render_uses_new_read_with_same_graphs(self, render):
        with tempfile.TemporaryDirectory() as folder, WorkspaceStore(Path(folder) / "workspace.json") as store:
            root = Path(folder)
            (root / "context.json").write_text(json.dumps(CONTEXT))
            (root / "reader-sources.json").write_text(json.dumps({'schema_version':1,'sources':[{'id':'pd','kind':'markdown','project_id':'personal-development','project_title':'个人发展与求职','title':'入口','root':str(root/'offline'),'path':'README.md'}],'not_connected':[]}))
            reader = ReadingViews(store, root / "context.json", root / "views")
            with patch("app.reader.read_sources", side_effect=[supplemental("before"), supplemental("after")]):
                result = reader.snapshot()
            self.assertFalse(result["stale"])
            self.assertEqual(result["bundle"]["source_versions"]["pilot-source"], "after")
            self.assertEqual(result["sources"][0]["status"], "synced")
            self.assertEqual(render.call_count, 3)

    @patch("app.reader._render_svg", return_value=SVG)
    def test_partial_source_failure_is_not_reported_as_synchronized(self, render):
        with tempfile.TemporaryDirectory() as folder, WorkspaceStore(Path(folder) / "workspace.json") as store:
            root = Path(folder)
            (root / "context.json").write_text(json.dumps(CONTEXT))
            (root / "reader-sources.json").write_text(json.dumps({'schema_version':1,'sources':[{'id':'pd','kind':'markdown','project_id':'personal-development','project_title':'个人发展与求职','title':'入口','root':str(root/'offline'),'path':'README.md'}],'not_connected':[]}))
            reader = ReadingViews(store, root / "context.json", root / "views")
            with patch("app.reader.read_sources", return_value=supplemental("old", "unavailable")):
                result = reader.snapshot()
            self.assertFalse(result["stale"])  # Other project content is independently usable.
            self.assertEqual(result["sources"][0]["status"], "unavailable")
            self.assertEqual(result["sources"][0]["rendered_version"], "old")
            self.assertEqual(len(result["bundle"]["projects"]), 2)

    @patch("app.reader._render_svg", return_value=SVG)
    def test_classification_for_registered_supplement_keeps_business_facts_readonly(self, render):
        context = copy.deepcopy(CONTEXT)
        context['schema_version'] = 2
        context['relations'].append({'from':'personal-development','to':'area-a','kind':'belongs_to','status':'proposed'})
        extra = supplemental()
        original = copy.deepcopy(extra)
        bundle = build_bundle(initial_workspace(),context,supplement=extra)
        self.assertIn(_alias('personal-development'), bundle['sources']['context'])
        self.assertTrue(bundle['projects'][1]['read_only'])
        self.assertEqual(bundle['context']['relations'][-1]['status'],'proposed')
        self.assertEqual(extra,original)
        # The catalog, not source availability, owns the classification identity.
        offline = build_bundle(initial_workspace(),context,registered_projects=[{'id':'personal-development','title':'个人发展'}])
        self.assertEqual(offline['context']['relations'][-1]['from'],'personal-development')
        self.assertNotIn('personal-development',offline['reading_model']['project_ids'])


if __name__ == "__main__":
    unittest.main()
