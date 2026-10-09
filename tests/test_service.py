import json
from pathlib import Path
import tempfile
import threading
import unittest
from urllib.error import HTTPError
from urllib.request import urlopen
from unittest.mock import patch

from app.store import StorageError, WorkspaceStore
from tools.service import EntryServer, definition, owns_plist


class ServiceTests(unittest.TestCase):
    def test_no_initialization_for_missing_existing_volume(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp)/'missing-disk/project/data/workspace.json'
            with self.assertRaises(StorageError): WorkspaceStore(path, create=False)
            self.assertFalse(path.parent.exists())

    def test_entry_survives_missing_volume_and_reconnects_same_url(self):
        with tempfile.TemporaryDirectory() as tmp:
            project = Path(tmp)/'project'
            server = EntryServer(project,0)
            thread=threading.Thread(target=server.serve_forever,daemon=True);thread.start()
            url=f'http://127.0.0.1:{server.server_port}'
            try:
                with self.assertRaises(HTTPError) as error: urlopen(url,timeout=3)
                self.assertEqual(error.exception.code,503)
                self.assertIn('没有创建空账本',error.exception.read().decode())
                error.exception.close()
                self.assertFalse(project.exists())
                (project/'app').mkdir(parents=True);(project/'app/server.py').write_text('# code availability marker')
                with WorkspaceStore(project/'data/workspace.json'): pass
                with urlopen(url+'/healthz',timeout=3) as response: self.assertTrue(json.load(response)['ready'])
                detached=project.with_name('detached');project.rename(detached)
                with self.assertRaises(HTTPError) as offline:urlopen(url+'/healthz',timeout=3)
                offline.exception.close()
                self.assertFalse(project.exists())
                detached.rename(project)
                with urlopen(url+'/healthz',timeout=3) as response:self.assertTrue(json.load(response)['ready'])
            finally:
                server.shutdown();server.server_close();thread.join()

    def test_launch_agent_owns_only_its_entry_and_port_conflict_is_non_destructive(self):
        import plistlib
        from tools import service
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp);plist=root/'agent.plist'
            config=definition(root/'project',Path('/usr/bin/python3'),root/'support')
            self.assertTrue(config['KeepAlive']);self.assertTrue(config['RunAtLoad'])
            self.assertNotIn('/Volumes',config['WorkingDirectory'])
            plist.write_bytes(plistlib.dumps(config));self.assertTrue(owns_plist(plist,root/'support'))
            config['ProgramArguments'][1]='/unrelated.py';plist.write_bytes(plistlib.dumps(config));self.assertFalse(owns_plist(plist,root/'support'))
            project=root/'project';(project/'data').mkdir(parents=True);(project/'data/workspace.json').write_text('{}')
            with patch.object(service,'PLIST',root/'absent.plist'), patch.object(service,'launch') as launch, patch.object(service.socket,'socket') as sock, patch.object(service.subprocess,'run') as run:
                launch.return_value.returncode=1;sock.return_value.__enter__.return_value.connect_ex.return_value=0;run.return_value.stdout='unrelated listener'
                with self.assertRaisesRegex(RuntimeError,'已被占用'):service.install(project)
                self.assertEqual(launch.call_count,1)


if __name__=='__main__':unittest.main()
