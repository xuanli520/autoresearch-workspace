import datetime
import importlib.util
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

spec=importlib.util.spec_from_file_location('registry',Path(__file__).resolve().parents[1]/'registry.py')
registry=importlib.util.module_from_spec(spec);spec.loader.exec_module(registry)

class RegistryTests(unittest.TestCase):
    def test_archiving_final_entry_leaves_a_valid_empty_registry(self):
        with tempfile.TemporaryDirectory() as tmp:
            path=Path(tmp)/'tasks.json'
            path.write_text(json.dumps({'version':1,'hosts':{'local':{'transport':'local'}},
                'tasks':[{'id':'done','host':'local','root':'/completed'}]}))
            registry.archive(path,{'done':'completed'},True)
            result=subprocess.run([sys.executable,str(Path(__file__).resolve().parents[1]/'monitor.py'),
                'validate','--config',str(path),'--auth',str(Path(tmp)/'absent-auth')],
                text=True,capture_output=True,timeout=5)
            self.assertEqual(result.returncode,0,result.stderr)
            self.assertEqual(json.loads(result.stdout)['tasks'],[])
    def test_prune_excludes_active_unreachable_stale_and_conflicting_records(self):
        now=1700000000
        recent=datetime.datetime.fromtimestamp(now-10,datetime.timezone.utc).isoformat()
        expired=datetime.datetime.fromtimestamp(now-100,datetime.timezone.utc).isoformat()
        rows=[]
        for ident,state,processes,alerts,observed in [('done','COMPLETED',[],[],recent),('expired','EXITED_WITHOUT_RESULT',[],[],recent),('alive','RUNNING',[{'pid':3}],[],recent),('offline','UNREACHABLE',[],[],recent),('stale','STOPPED',[],[],expired),('conflict','FAILED',[],['STATUS_CONFLICT'],recent)]:
            rows.append(dict(id=ident,state=state,processes=processes,alerts=alerts,observed_at=observed,deadline_at=expired))
        self.assertEqual(set(registry.eligible({'tasks':rows},now,60)),{'done','expired'})
    def test_prune_preserves_records_with_controller_mismatches(self):
        now=1700000000
        recent=datetime.datetime.fromtimestamp(now-10,datetime.timezone.utc).isoformat()
        expired=datetime.datetime.fromtimestamp(now-100,datetime.timezone.utc).isoformat()
        for alert in ('CONTROLLER_TYPE_MISMATCH','CONTROLLER_IDENTITY_MISMATCH'):
            for state in ('COMPLETED','EXITED_WITHOUT_RESULT'):
                with self.subTest(alert=alert,state=state):
                    task=dict(id='unverified',state=state,processes=[],alerts=[alert],
                              observed_at=recent,deadline_at=expired)
                    self.assertEqual(registry.eligible({'tasks':[task]},now,60),{})
    def test_preview_is_reversible_and_archive_preserves_history_and_active_entries(self):
        with tempfile.TemporaryDirectory() as tmp:
            path=Path(tmp)/'tasks.json'
            config={'version':1,'hosts':{'a':{'transport':'ssh'},'b':{'transport':'local'}},'tasks':[{'id':'old','host':'a','root':'/old'},{'id':'live','host':'b','root':'/live'}]}
            path.write_text(json.dumps(config));original=path.read_bytes()
            preview=registry.archive(path,{'old':'finished'})
            self.assertEqual(path.read_bytes(),original);self.assertFalse(preview['applied'])
            result=registry.archive(path,{'old':'finished'},True,preview['config_sha256'])
            current=json.loads(path.read_text());archived=json.loads(Path(result['archive']).read_text())
            self.assertEqual(current['tasks'],[config['tasks'][1]])
            self.assertEqual(archived['tasks'],[config['tasks'][0]])
            self.assertEqual(set(current['hosts']),{'b'})
            self.assertFalse(result['remote_tasks_affected'])
    def test_changed_configuration_and_unknown_id_are_rejected(self):
        with tempfile.TemporaryDirectory() as tmp:
            path=Path(tmp)/'tasks.json';path.write_text('{"tasks":[],"hosts":{}}')
            with self.assertRaisesRegex(ValueError,'unknown'):registry.archive(path,{'missing':'old'},True)
            with self.assertRaisesRegex(ValueError,'changed'):registry.archive(path,{},True,'outdated-digest')

if __name__=='__main__':unittest.main()
