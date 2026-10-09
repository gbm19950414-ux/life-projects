import copy
import json
from pathlib import Path
import tempfile
import threading
import unittest
from unittest.mock import patch
from urllib.request import urlopen

from app.graphs import _alias, _short_datetime, flow_puml
from app.model import initial_workspace
from app.reader import ReadingViews, build_bundle, export_bundle, fingerprint, safe_svg
from app.server import LocalServer
from app.store import WorkspaceStore
from test_graphs import CONTEXT

SVG = b'<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 100 100"><g data-qualified-name="PROJECT"><rect width="100" height="100"/></g></svg>'


class ReaderTests(unittest.TestCase):
    def test_focus_hides_old_history_without_shortcut_edges(self):
        workspace = initial_workspace()
        first, second, third = workspace["tasks"][:3]
        first["state"] = second["state"] = "done"
        source = flow_puml(workspace, focus=True)
        self.assertNotIn(_alias(first["id"]), source)
        self.assertIn(_alias(second["id"]), source)
        self.assertIn(f'{_alias(second["id"])} --> {_alias(third["id"])} : 前置', source)
        self.assertIn("已收起 1 项其他事项", source)
        self.assertNotIn("计划：", source)

    def test_fingerprint_includes_context_and_local_time(self):
        workspace = initial_workspace()
        context = copy.deepcopy(CONTEXT)
        before = fingerprint(workspace, context)
        context["areas"][0]["title"] = "新领域名"
        self.assertNotEqual(before, fingerprint(workspace, context))
        self.assertEqual(_short_datetime("2026-09-25T23:30:00Z"), "2026-09-26 07:30")

    def test_active_svg_content_removed(self):
        source = b'<svg xmlns="http://www.w3.org/2000/svg" onload="bad()"><script>bad()</script><foreignObject/><image href="https://example.com"/><rect onclick="bad()" style="fill:url(https://example.com)"/></svg>'
        clean = safe_svg(source)
        for text in ("bad", "https", "onload", "script", "foreignObject", "image"):
            self.assertNotIn(text, clean)

    @patch("app.reader._render_svg", return_value=SVG)
    def test_bundle_excludes_private_sources_and_export_is_safe(self, renderer):
        workspace = initial_workspace()
        workspace["tasks"][0]["source_ref"] = "/private/recording.wav"
        workspace["goals"][0]["target_state"] = "目标 </script><script>bad()</script>"
        bundle = build_bundle(workspace, CONTEXT)
        self.assertEqual(renderer.call_count, 3)
        self.assertNotIn("/private/recording.wav", json.dumps(bundle))
        with tempfile.TemporaryDirectory() as folder:
            page = export_bundle(bundle, folder)
            self.assertNotIn("</script><script>bad()", page.read_text())
            self.assertEqual(json.loads((Path(folder) / "bundle.json").read_text())["revision"], 0)

    @patch("app.reader._render_svg", return_value=SVG)
    def test_context_refresh_failure_retry_and_restart_keep_one_snapshot(self, renderer):
        with tempfile.TemporaryDirectory() as folder, WorkspaceStore(Path(folder) / "workspace.json") as store:
            context = Path(folder) / "context.json"
            context.write_text(json.dumps(CONTEXT))
            output = Path(folder) / "views"
            reader = ReadingViews(store, context, output)
            original = reader.snapshot()
            self.assertFalse(original["stale"])
            self.assertEqual(renderer.call_count, 3)
            self.assertEqual(reader.snapshot(), original)
            self.assertEqual(renderer.call_count, 3)
            edited = copy.deepcopy(CONTEXT)
            edited["areas"][0]["title"] = "新的归属"
            context.write_text(json.dumps(edited))
            renderer.side_effect = RuntimeError("changed diagram failed")
            failed = reader.snapshot()
            self.assertTrue(failed["stale"])
            self.assertEqual(failed["bundle"], original["bundle"])
            renderer.side_effect = RuntimeError("offline")
            reopened = ReadingViews(store, context, output)
            self.assertEqual(reopened.snapshot()["bundle"], original["bundle"])
            renderer.side_effect = None
            reader.retry_at = 0
            fresh = reader.snapshot()
            self.assertFalse(fresh["stale"])
            self.assertNotEqual(fresh["bundle"]["fingerprint"], original["bundle"]["fingerprint"])
            self.assertEqual(fresh["bundle"]["revision"], original["bundle"]["revision"])
            context.write_text("{invalid")
            self.assertTrue(reader.snapshot()["stale"])
            self.assertEqual(reader.snapshot()["bundle"], fresh["bundle"])

    @patch("app.reader._render_svg", return_value=SVG)
    def test_http_reads_new_ledger_and_context_without_manual_render(self, renderer):
        with tempfile.TemporaryDirectory() as folder, WorkspaceStore(Path(folder) / "workspace.json") as store:
            context = Path(folder) / "context.json"
            context.write_text(json.dumps(CONTEXT))
            server = LocalServer(0, store, Path(folder) / "views", context)
            thread = threading.Thread(target=server.serve_forever, daemon=True)
            thread.start()
            try:
                url = f"http://127.0.0.1:{server.server_port}"
                def read():
                    with urlopen(url + "/api/reading", timeout=3) as response:
                        return json.load(response)
                old = read()
                body = {key: copy.deepcopy(store.read()[key]) for key in ("projects", "goals", "tasks", "time_blocks", "work_logs")}
                body["tasks"].append({"title": "API 保存后的候选"})
                store.save(0, body)
                fresh = read()
                self.assertEqual(fresh["bundle"]["revision"], 1)
                self.assertNotEqual(old["bundle"]["fingerprint"], fresh["bundle"]["fingerprint"])
                context.write_text("{broken")
                self.assertTrue(read()["stale"])
                with urlopen(url, timeout=3) as response:
                    self.assertEqual(response.status, 200)
                    self.assertIn(b"reader.js", response.read())
            finally:
                server.shutdown()
                server.server_close()
                thread.join()


if __name__ == "__main__":
    unittest.main()
