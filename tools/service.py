"""User launch agent plus an internal-disk HTTP entry point with volume recovery."""
from __future__ import annotations

import argparse
import importlib
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import os
from pathlib import Path
import plistlib
import shutil
import socket
import subprocess
import sys
import threading

LABEL = 'local.life-projects'
SUPPORT = Path.home() / 'Library/Application Support/Life Projects'
PLIST = Path.home() / 'Library/LaunchAgents' / (LABEL + '.plist')
TARGET = f'gui/{os.getuid()}/{LABEL}'


class Runtime:
    def __init__(self, project):
        self.project = Path(project)
        self.store = self.reading = self.handler = None
        self.identity = None
        self.lock = threading.RLock()

    def close(self):
        if self.store:
            self.store.close()
        self.store = self.reading = self.handler = self.identity = None

    def connect(self):
        ledger = self.project / 'data/workspace.json'
        try:
            if not ledger.is_file() or not (self.project / 'app/server.py').is_file():
                raise OSError('项目磁盘或账本不可用；未创建空账本')
            stat = (self.project / 'data/workspace.json.lock').stat()
            identity = (stat.st_dev, stat.st_ino)
            if self.store and identity == self.identity:
                return
            self.close()
            sys.path.insert(0, str(self.project)) if str(self.project) not in sys.path else None
            store_module = importlib.import_module('app.store')
            server_module = importlib.import_module('app.server')
            self.store = store_module.WorkspaceStore(ledger, create=False)
            self.reading = server_module.ReadingViews(self.store, ledger.parent / 'context.json', self.project / 'views/current')
            self.handler = server_module.Handler
            self.identity = identity
        except Exception:
            self.close()
            raise


class EntryHandler(BaseHTTPRequestHandler):
    def _reply(self, status, body, content_type='application/json; charset=utf-8'):
        payload = body if isinstance(body, bytes) else json.dumps(body, ensure_ascii=False).encode()
        self.send_response(status)
        for name, value in {'Content-Type':content_type, 'Content-Length':str(len(payload)), 'Cache-Control':'no-store',
                            'X-Content-Type-Options':'nosniff', 'Referrer-Policy':'no-referrer',
                            'Content-Security-Policy':"default-src 'self'; script-src 'self'; style-src 'self' 'unsafe-inline'; connect-src 'self'; img-src 'self' data:; object-src 'none'; base-uri 'none'"}.items():
            self.send_header(name, value)
        self.end_headers()
        self.wfile.write(payload)

    def _error(self, status, message, **extra):
        self._reply(status, {'error':message, **extra})

    def dispatch(self, method):
        runtime = self.server.runtime
        with runtime.lock:
            try:
                runtime.connect()
                self.server.store, self.server.reading = runtime.store, runtime.reading
                if self.path == '/healthz' and method == 'GET':
                    self._reply(200, {'service':LABEL, 'ready':True})
                else:
                    getattr(runtime.handler, 'do_' + method)(self)
            except (BrokenPipeError, ConnectionResetError):
                pass
            except Exception as exc:
                print(f'项目暂不可用：{type(exc).__name__}: {exc}', file=sys.stderr, flush=True)
                if method == 'GET' and self.path in ('/', '/index.html'):
                    self._reply(503, ('<!doctype html><html lang="zh-CN"><meta charset="utf-8"><meta name="viewport" content="width=device-width"><meta http-equiv="refresh" content="5"><title>Life Projects · 来源不可用</title><main><h1>Life Projects</h1><p>项目磁盘或账本暂不可用。</p><p>入口仍在运行。连接项目磁盘后将自动恢复；没有创建空账本。</p><a href="/">重新检查</a></main></html>').encode(), 'text/html; charset=utf-8')
                else:
                    self._reply(503, {'service':LABEL, 'ready':False, 'error':'项目来源不可用；保留原数据并自动重试'})

    def do_GET(self):
        self.dispatch('GET')

    def do_PUT(self):
        self.dispatch('PUT')

    def log_message(self, format, *args):
        # Five-second successful polls need no persistent access log.
        if len(args) > 1 and str(args[1]) not in ('200', '304'):
            super().log_message(format, *args)


class EntryServer(ThreadingHTTPServer):
    daemon_threads = True
    allow_reuse_address = True

    def __init__(self, project, port):
        self.runtime = Runtime(project)
        super().__init__(('127.0.0.1', port), EntryHandler)

    def server_close(self):
        with self.runtime.lock:
            self.runtime.close()
        super().server_close()


def launch(*args, check=False):
    return subprocess.run(['launchctl', *args], capture_output=True, text=True, check=check)


def definition(project, python, support=SUPPORT):
    return {'Label':LABEL, 'ProgramArguments':[str(python), str(support/'service.py'), 'run', '--project', str(project), '--port', '8765'],
            'RunAtLoad':True, 'KeepAlive':True, 'ThrottleInterval':10, 'WorkingDirectory':str(support),
            'StandardOutPath':str(support/'service.out.log'), 'StandardErrorPath':str(support/'service.err.log')}


def owns_plist(path=PLIST, support=SUPPORT):
    if not path.exists():
        return False
    value = plistlib.loads(path.read_bytes())
    return value.get('Label') == LABEL and len(value.get('ProgramArguments', [])) >= 2 and value['ProgramArguments'][1] == str(support/'service.py')


def install(project):
    project = Path(project).resolve()
    if not (project/'data/workspace.json').is_file():
        raise RuntimeError('项目账本缺失；拒绝安装或初始化空账本')
    loaded = launch('print', TARGET).returncode == 0
    if (PLIST.exists() and not owns_plist()) or (loaded and not owns_plist()):
        raise RuntimeError('已有同名服务不属于此安装器，未修改')
    with socket.socket() as probe:
        occupied = probe.connect_ex(('127.0.0.1',8765)) == 0
    if occupied and not loaded:
        owner = subprocess.run(['lsof','-nP','-iTCP:8765','-sTCP:LISTEN'], capture_output=True, text=True).stdout
        raise RuntimeError('8765 已被占用；未终止任何进程。先核对并停止旧服务：\n'+owner)
    SUPPORT.mkdir(parents=True, exist_ok=True)
    PLIST.parent.mkdir(parents=True, exist_ok=True)
    temp = SUPPORT/'service.py.new'; shutil.copy2(__file__, temp); os.replace(temp, SUPPORT/'service.py')
    plist_temp = SUPPORT/'agent.plist.new'; plist_temp.write_bytes(plistlib.dumps(definition(project, Path(shutil.which('python3') or sys.executable))))
    os.replace(plist_temp, PLIST)
    if loaded:
        launch('bootout', TARGET, check=True)
    launch('bootstrap', f'gui/{os.getuid()}', str(PLIST), check=True)
    print('已安装用户登录启动：'+TARGET)


def uninstall():
    if not owns_plist():
        raise RuntimeError('没有本工具拥有的启动配置；未改动')
    if launch('print', TARGET).returncode == 0:
        launch('bootout', TARGET, check=True)
    PLIST.unlink()
    print('已停止并移除登录启动；保留服务文件、日志和项目数据')


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('action', choices=['install','status','uninstall','run'])
    parser.add_argument('--project', type=Path, default=Path(__file__).resolve().parent.parent)
    parser.add_argument('--port',type=int,default=8765)
    args=parser.parse_args()
    try:
        if args.action == 'install': install(args.project)
        elif args.action == 'uninstall': uninstall()
        elif args.action == 'status':
            result=launch('print',TARGET)
            print(result.stdout if result.returncode==0 else '未运行用户启动服务')
        else:
            with EntryServer(args.project,args.port) as server:
                print(f'Life Projects entry: http://127.0.0.1:{server.server_port}/',flush=True)
                server.serve_forever()
    except (OSError, ValueError, RuntimeError, subprocess.CalledProcessError) as exc:
        parser.exit(1,f'服务操作失败：{exc}\n')


if __name__=='__main__':
    main()
