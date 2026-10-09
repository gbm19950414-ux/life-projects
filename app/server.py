"""Browser UI and JSON API. Start with python3 -m app.server."""
from __future__ import annotations

import argparse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
from pathlib import Path
from urllib.parse import urlsplit

from .context_edit import ContextConflict, read_context, save_context
from .tracking import load_tracking, TrackingError
from .reader import ReadingViews
from .model import ValidationError
from .store import ConflictError, StorageError, WorkspaceStore

STATIC = Path(__file__).parent / "static"
FILES = {"/": ("reader.html", "text/html; charset=utf-8"),
         "/index.html": ("reader.html", "text/html; charset=utf-8"),
         "/reader.css": ("reader.css", "text/css; charset=utf-8"),
         "/reader.js": ("reader.js", "text/javascript; charset=utf-8"),
         "/editor": ("index.html", "text/html; charset=utf-8"),
         "/editor/": ("index.html", "text/html; charset=utf-8"),
         "/style.css": ("style.css", "text/css; charset=utf-8"),
         "/app.js": ("app.js", "text/javascript; charset=utf-8")}
MAX_BODY = 2_000_000



class LocalServer(ThreadingHTTPServer):
    daemon_threads = True
    allow_reuse_address = True

    def __init__(self, port, store, views_dir=None, context_path=None):
        self.store = store
        self.views_dir = Path(views_dir) if views_dir is not None else Path(__file__).parent.parent / "views" / "current"
        self.reading = ReadingViews(store, context_path or store.path.parent / "context.json", self.views_dir)
        super().__init__(("127.0.0.1", port), Handler)


def context_args(server):
    # The installed entry delegates handler methods, so helpers cannot rely on its class.
    return (server.reading.context_path, server.store.read()["projects"][0]["id"],
            load_tracking(server.reading.tracking_path))


class Handler(BaseHTTPRequestHandler):
    server: LocalServer

    def _reply(self, status, body, content_type="application/json; charset=utf-8"):
        payload = body if isinstance(body, bytes) else json.dumps(body, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(payload)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Referrer-Policy", "no-referrer")
        self.send_header("Content-Security-Policy", "default-src 'self'; script-src 'self'; style-src 'self' 'unsafe-inline'; connect-src 'self'; img-src 'self' data:; object-src 'none'; base-uri 'none'")
        self.end_headers()
        self.wfile.write(payload)

    def _error(self, status, message, **extra):
        self._reply(status, {"error": message, **extra})

    def do_GET(self):
        path = urlsplit(self.path).path
        if path == "/api/context":
            try:
                self._reply(200, read_context(*context_args(self.server)))
            except (StorageError, ValidationError, TrackingError) as exc:
                self._error(503, str(exc))
            return
        if path == "/api/workspace":
            try:
                self._reply(200, self.server.store.view())
            except StorageError as exc:
                self._error(500, str(exc))
            return
        if path == "/api/reading":
            self._reply(200, self.server.reading.snapshot())
            return
        if path in {"/flow.svg", "/full.svg", "/context.svg"}:
            state = self.server.reading.snapshot()
            if state["bundle"] is None or state["stale"]:
                self._error(503, "图暂未更新，请在首页查看上次成功视图")
            else:
                key = path[1:-4]
                self._reply(200, state["bundle"]["svgs"][key].encode(), "image/svg+xml")
            return
        if path not in FILES:
            self._error(404, "页面不存在")
            return
        filename, content_type = FILES[path]
        self._reply(200, (STATIC / filename).read_bytes(), content_type)

    def do_PUT(self):
        path = urlsplit(self.path).path
        if path not in {"/api/workspace", "/api/context"}:
            self._error(404, "接口不存在")
            return
        expected_origin = f"http://127.0.0.1:{self.server.server_port}"
        if self.headers.get("Origin") != expected_origin:
            self._error(403, "仅允许本地面板同源写入")
            return
        if self.headers.get("Content-Type", "").split(";", 1)[0].lower() != "application/json":
            self._error(415, "请使用 JSON 提交")
            return
        try:
            length = int(self.headers.get("Content-Length", "0"))
        except ValueError:
            length = 0
        if not 0 < length <= MAX_BODY:
            self._error(413, "请求为空或超出大小限制")
            return
        try:
            data = json.loads(self.rfile.read(length))
            if path == "/api/context":
                if not isinstance(data, dict) or set(data) != {"expected_version", "change"}:
                    raise ValidationError("提交格式必须包含 expected_version 和 change")
                self._reply(200, save_context(*context_args(self.server), data["expected_version"], data["change"]))
                return
            if not isinstance(data, dict) or set(data) != {"expected_revision", "workspace", "reason"}:
                raise ValidationError("提交格式必须包含 expected_revision、workspace 和 reason")
            result = self.server.store.save(data["expected_revision"], data["workspace"], data["reason"])
            self._reply(200, result)
        except json.JSONDecodeError:
            self._error(400, "JSON 内容无效")
        except ValidationError as exc:
            self._error(400, str(exc))
        except ContextConflict as exc:
            self._error(409, str(exc))
        except TrackingError as exc:
            self._error(503, str(exc))
        except ConflictError as exc:
            self._error(409, str(exc), current_revision=exc.revision)
        except StorageError as exc:
            self._error(500, str(exc))


def main():
    parser = argparse.ArgumentParser(description="life-projects local dashboard")
    parser.add_argument("--data", default="data/workspace.json")
    parser.add_argument("--port", type=int, default=8765)
    args = parser.parse_args()
    if not 1 <= args.port <= 65535:
        parser.error("port must be 1–65535")
    try:
        with WorkspaceStore(args.data) as store:
            server = LocalServer(args.port, store)
            print(f"life-projects: http://127.0.0.1:{server.server_port}/", flush=True)
            try:
                server.serve_forever()
            except KeyboardInterrupt:
                print("已停止本地服务。", flush=True)
            finally:
                server.server_close()
    except (StorageError, OSError) as exc:
        parser.exit(1, f"无法启动：{exc}\n")


if __name__ == "__main__":
    main()
