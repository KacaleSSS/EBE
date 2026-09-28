import hashlib
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from ebe.core import modules
from ebe.server import readonly_status
from ebe.cli import reject_network_paths


class CLITests(unittest.TestCase):
    def run_cli(self, *args):
        return subprocess.run([sys.executable, '-m', 'ebe', *map(str,args)], capture_output=True, text=True, encoding='utf-8', timeout=30)

    def test_doctor(self):
        process = self.run_cli('doctor')
        self.assertEqual(process.returncode, 0, process.stderr)
        self.assertEqual(json.loads(process.stdout)['version'], '1.0.0')

    def test_export_exit_and_json(self):
        with tempfile.TemporaryDirectory() as folder:
            root=Path(folder)
            draft=root/'draft.md'; draft.write_text('# Synthetic\n\nSafe test text.', encoding='utf-8')
            for suffix in ['html','epub']:
                output=root/('book.'+suffix)
                result=self.run_cli('export',draft,output)
                self.assertEqual(result.returncode,0,result.stderr)
                self.assertEqual(json.loads(result.stdout)['output'],str(output))
                self.assertTrue(output.is_file())

    def test_state_server_does_not_migrate_database(self):
        engine,_,_=modules()
        with tempfile.TemporaryDirectory() as folder:
            project=Path(engine.init_project(folder,'test-book','Synthetic',allow_single_copy=True)['project'])
            db=project/'state.sqlite3'
            before=hashlib.sha256(db.read_bytes()).digest()
            self.assertEqual(readonly_status(project)['project_id'],'test-book')
            self.assertEqual(before,hashlib.sha256(db.read_bytes()).digest())

    def test_mcp_offline_surface(self):
        request='{"jsonrpc":"2.0","id":1,"method":"tools/list"}\n'
        process=subprocess.run([sys.executable,'-m','ebe','mcp'],input=request,capture_output=True,text=True,timeout=30)
        self.assertEqual(process.returncode,0,process.stderr)
        names={r['name'] for r in json.loads(process.stdout)['result']['tools']}
        self.assertNotIn('ingest_url',names)
        self.assertNotIn('run_research_batch',names)
        self.assertNotIn('purge_knowledge_base',names)
        self.assertIn('review_source',names)

    def test_unc_rejected_recursively(self):
        for path in [r'\\remote\share\secret', '//remote/share', r'\\?\UNC\server\share']:
            with self.assertRaises(ValueError):
                reject_network_paths({'project_dir':path})
