import copy
import json
from pathlib import Path
import tempfile
import threading
import unittest
from unittest.mock import patch
from urllib.error import HTTPError
from urllib.request import Request, urlopen

from app.context_edit import ContextConflict, apply_change, read_context, save_context, version
from app.model import ValidationError
from app.server import LocalServer
from app.store import StorageError, WorkspaceStore
from app.tracking import empty_tracking
from test_graphs import CONTEXT
from tools.service import EntryServer


class ContextEditTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.path = Path(self.temp.name) / 'context.json'
        self.path.write_text(json.dumps(CONTEXT))
        self.tracking = empty_tracking()

    def save(self, change, expected=None):
        return save_context(self.path, 'life-projects', self.tracking,
                            expected or version(json.loads(self.path.read_text())), change)

    def test_assignment_confirmation_and_undo_preserve_other_facts(self):
        before = self.path.read_bytes()
        result = self.save({'action':'assign_area','project_id':'life-projects','area':{'title':'科研'}})
        context = result['context']
        self.assertEqual(context['relations'][0], CONTEXT['relations'][1])
        self.assertEqual(context['directions'], CONTEXT['directions'])
        self.assertEqual(context['areas'][0]['status'], 'proposed')
        self.assertEqual(context['areas'][-1]['status'], 'confirmed')
        self.assertEqual(json.loads(self.path.with_suffix('.json.bak').read_text()), CONTEXT)
        undone = self.save({'action':'restore','context':result['previous']}, result['version'])
        self.assertEqual(undone['context'], json.loads(before))
        self.assertEqual(read_context(self.path, 'life-projects', self.tracking)['version'], version(CONTEXT))

    def test_external_classification_and_area_directions_do_not_infer_goal_acceptance(self):
        tracking = empty_tracking()
        tracking.update(projects=[{'id':'external','title':'外部开发'}], source_thread={'id':'fixture','title':'隔离来源','url':'codex://threads/fixture','status':'test'}, observed_at='2026-09-29T00:00:00Z')
        before_tracking = copy.deepcopy(tracking)
        changed = apply_change(CONTEXT, {'action':'assign_area','project_id':'tracking:project:external','area':{'id':'area-a'}}, 'life-projects', tracking)
        changed = apply_change(changed, {'action':'set_directions','subject_id':'area-a','directions':[{'id':'direction-a'},{'title':'保持健康'}]}, 'life-projects', tracking)
        self.assertEqual(len(changed['directions']), 2)
        self.assertEqual(changed['relations'][1]['status'], 'proposed')
        self.assertEqual(CONTEXT['areas'][0]['status'], 'proposed')
        self.assertEqual(tracking, before_tracking)
        self.assertNotIn('assessment', changed)

    def test_duplicate_names_reuse_id_and_invalid_changes_do_not_write(self):
        result = self.save({'action':'assign_area','project_id':'life-projects','area':{'title':' 软件工程 '}})
        self.assertEqual(len(result['context']['areas']), 1)
        self.assertEqual(result['context']['relations'][-1]['to'], 'area-a')
        before = self.path.read_bytes()
        for change in [
            {'action':'assign_area','project_id':'missing','area':None},
            {'action':'assign_area','project_id':'life-projects','area':{'id':'missing'}},
            {'action':'set_directions','subject_id':'life-projects','directions':[{'id':'direction-a'},{'title':'可修订方向'}]},
            {'action':'restore','context':{}},
        ]:
            with self.assertRaises(ValidationError): self.save(change)
            self.assertEqual(self.path.read_bytes(), before)

    def test_stale_save_and_undo_do_not_overwrite_newer_edit(self):
        initial = version(CONTEXT)
        result = self.save({'action':'assign_area','project_id':'life-projects','area':None})
        latest = self.save({'action':'set_directions','subject_id':'life-projects','directions':[]})
        for expected, change in [(initial, {'action':'confirm_area','area_id':'area-a'}),
                                 (result['version'], {'action':'restore','context':result['previous']})]:
            with self.assertRaises(ContextConflict): self.save(change, expected)
            self.assertEqual(json.loads(self.path.read_text()), latest['context'])

    def test_failed_atomic_write_keeps_primary_and_missing_source_is_not_created(self):
        original = self.path.read_bytes()
        with patch('app.context_edit.WorkspaceStore._write_atomically', side_effect=OSError('full')):
            with self.assertRaises(StorageError): self.save({'action':'confirm_area','area_id':'area-a'})
        self.assertEqual(self.path.read_bytes(), original)
        self.path.unlink()
        with self.assertRaises(StorageError): self.save({'action':'confirm_area','area_id':'area-a'}, version(CONTEXT))
        self.assertFalse(self.path.exists())

    def test_simultaneous_writers_only_one_wins(self):
        barrier = threading.Barrier(2)
        outcomes = []
        def writer(title):
            barrier.wait()
            try: self.save({'action':'assign_area','project_id':'life-projects','area':{'title':title}}, version(CONTEXT));outcomes.append('saved')
            except ContextConflict: outcomes.append('conflict')
        threads=[threading.Thread(target=writer,args=(n,)) for n in ['A','B']]
        for thread in threads: thread.start()
        for thread in threads: thread.join()
        self.assertCountEqual(outcomes, ['saved','conflict'])

    def test_installed_entry_delegates_context_read_and_save(self):
        project = self.path.parent
        (project/'app').mkdir();(project/'app/server.py').write_text('# availability marker')
        with WorkspaceStore(project/'data/workspace.json'): pass
        (project/'data/context.json').write_text(json.dumps(CONTEXT))
        server = EntryServer(project,0)
        thread=threading.Thread(target=server.serve_forever,daemon=True);thread.start()
        url=f'http://127.0.0.1:{server.server_port}'
        try:
            with urlopen(url+'/api/context',timeout=3) as response: before=json.load(response)
            body={'expected_version':before['version'],'change':{'action':'confirm_area','area_id':'area-a'}}
            with urlopen(Request(url+'/api/context',data=json.dumps(body).encode(),method='PUT',headers={'Origin':url,'Content-Type':'application/json'}),timeout=3) as response:
                self.assertEqual(json.load(response)['context']['areas'][0]['status'],'confirmed')
        finally:
            server.shutdown();server.server_close();thread.join()

    def test_http_save_read_conflict_origin_and_ledger_is_unchanged(self):
        with WorkspaceStore(self.path.parent / 'workspace.json') as store:
            ledger = store.path.read_bytes()
            server = LocalServer(0, store, self.path.parent / 'views', self.path)
            thread = threading.Thread(target=server.serve_forever,daemon=True);thread.start()
            url = f'http://127.0.0.1:{server.server_port}'
            def put(body, origin=url):
                return urlopen(Request(url+'/api/context',data=json.dumps(body).encode(),method='PUT',headers={'Origin':origin,'Content-Type':'application/json'}))
            try:
                with urlopen(url+'/api/context') as response: original=json.load(response)
                body={'expected_version':original['version'],'change':{'action':'assign_area','project_id':'life-projects','area':{'id':'area-a'}}}
                with put(body) as response: saved=json.load(response)
                with urlopen(url+'/api/context') as response: self.assertEqual(json.load(response)['context'],saved['context'])
                for request, code in [(lambda:put(body),409),(lambda:put(body,'https://foreign.example'),403),(lambda:put({}),400)]:
                    with self.assertRaises(HTTPError) as error: request()
                    self.assertEqual(error.exception.code,code)
                self.assertEqual(store.path.read_bytes(),ledger)
            finally:
                server.shutdown();server.server_close();thread.join()

    def test_registered_business_classification_offline_and_unknown_rejection(self):
        # Catalog registration grants only local classification; source need not exist.
        catalog = {'schema_version':1,'sources':[{'id':'pd','kind':'markdown','project_id':'personal-development',
            'project_title':'个人发展','title':'入口','root':str(self.path.parent/'offline'), 'path':'README.md'}], 'not_connected':[]}
        (self.path.parent/'reader-sources.json').write_text(json.dumps(catalog))
        result = self.save({'action':'assign_area','project_id':'personal-development','area':{'id':'area-a'}})
        self.assertEqual(read_context(self.path,'life-projects',self.tracking)['context'],result['context'])
        self.assertFalse((self.path.parent/'offline').exists())
        self.assertEqual(json.loads((self.path.parent/'reader-sources.json').read_text()),catalog)
        before = self.path.read_bytes()
        with self.assertRaises(ValidationError):
            self.save({'action':'assign_area','project_id':'unregistered-project','area':{'id':'area-a'}})
        self.assertEqual(self.path.read_bytes(),before)
        # Direction and undo use the same version/registration contract.
        changed = self.save({'action':'set_directions','subject_id':'personal-development','directions':[{'id':'direction-a'}]})
        self.save({'action':'restore','context':changed['previous']},changed['version'])
        self.assertEqual(json.loads(self.path.read_text()),result['context'])
