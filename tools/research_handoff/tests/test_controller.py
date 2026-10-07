import argparse
import hashlib
import json
import os
import signal
import shutil
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(ROOT), str(ROOT/'core')]
import bundle
import controller
import longrun
import remote
from processes import boot_id, pid_matches, process_start_ticks, scope_members, signal_identity, terminate_scope

SCRATCH = ROOT/'incidents/controller-hardening/scratch'
SCRATCH.mkdir(parents=True, exist_ok=True)
PREAMBLE = """import os,json,time
G=int(os.environ['AUTORESEARCH_CONTEXT_GENERATION'])
with open(os.environ['AUTORESEARCH_CONTEXT_FILE']) as context_stream:
 C=json.load(context_stream).get('conversation_id') or 'chat-'+str(G)
def emit(kind, **fields):
 print(json.dumps(dict(fields,autoresearch=kind,generation=G)),flush=True)
emit('context.usage',used_tokens=10,conversation_id=C)
emit('heartbeat')
"""


def config(root, code, **sections):
    value = {'version':1, 'task_id':'test', 'root':str(root), 'command':[sys.executable,'-B','-c',code],
        'stage':'diagnostic', 'score_expectation':'not_expected', 'metric':'runtime', 'direction':'max',
        'required_seeds':[], 'deadline':'2099-01-01T00:00:00Z',
        'candidate_manifest':'candidate.manifest.json', 'protocol_hash':'0'*64,
        'budget':{'mode':'active','window_seconds':.25,'hard_limit_seconds':6,'credit_policy':'successful_turn'},
        'turn':{'seconds':2,'grace_seconds':.1},
        'context':{'max_tokens':4096,'compact_at_tokens':3000,'reserve_tokens':256,'summary_seconds':.2},
        'heartbeat':{'required':True,'interval_seconds':.03,'stale_after_seconds':1,'controller_stale_seconds':.4},
        'policy':{'retry_backoff_seconds':.01}, 'output':{'min_free_bytes':1,'max_run_bytes':16*1024*1024}}
    for name, values in sections.items():
        if isinstance(values, dict): value.setdefault(name,{}).update(values)
        else: value[name]=values
    return value


class ControllerTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory(prefix=self._testMethodName+'-',dir=SCRATCH)
        self.base=Path(self.tmp.name); self.state=self.base/'state'; self.run=self.state/'runs/test'
        self.children=[]

    def tearDown(self):
        for p in self.children:
            if p.poll() is None: p.kill()
            p.wait(timeout=3)
        if (self.run/'state.json').exists():
            s=self.read()
            for name in ('controller','guard'):
                signal_identity(s.get(name+'_pid'), s.get(name+'_start_ticks'), signal.SIGKILL, s.get('process_boot_id'))
            terminate_scope(s['turn'].get('token',''),0)
        self.tmp.cleanup()

    def invoke(self,*args,timeout=15):
        return subprocess.run([sys.executable,'-B',str(ROOT/'controller.py'),'--state-dir',str(self.state),
            *map(str,args)],text=True,capture_output=True,timeout=timeout)

    def init(self,code=PREAMBLE+"time.sleep(.1); emit('turn.completed',credit=True)",**sections):
        cfg=config(self.base,code,**sections)
        (self.base/'config.json').write_text(json.dumps(cfg))
        r=self.invoke('init','--config',self.base/'config.json','--run-id','test')
        self.assertEqual(r.returncode,0,r.stderr)
        return cfg

    def read(self): return longrun.load_state(self.run)

    def launch(self,guard=True):
        args=[sys.executable,'-B',str(ROOT/'controller.py'),'--state-dir',str(self.state),'run','--run-id','test']
        if not guard: args.append('--no-guard')
        with (self.base/'process.log').open('w') as log:
            p=subprocess.Popen(args,stdout=log,stderr=log)
        self.children.append(p)
        self.until(lambda: self.read()['turn'].get('status')=='RUNNING' and
                   (Path(self.read()['turn']['dir'])/'worker-start.json').exists())
        return p

    def until(self,check,seconds=6):
        deadline=time.monotonic()+seconds
        while time.monotonic()<deadline:
            if check(): return
            time.sleep(.03)
        self.fail('condition timed out; state='+json.dumps(self.read()))

    def test_budget_and_each_turn_exit_contract(self):
        self.init()
        r=self.invoke('run','--run-id','test')
        self.assertEqual(r.returncode,0,r.stderr)
        s=self.read(); self.assertEqual(s['status'],'COMPLETED')
        exits=list((self.run/'turns').glob('*/exit.json'))
        self.assertEqual(len(exits),s['turn']['number'])
        credited=sum(json.loads(p.read_text())['elapsed_seconds'] for p in exits)
        self.assertAlmostEqual(credited,s['budget']['active_seconds'])
        self.assertGreaterEqual(credited,.25)
        self.assertTrue((self.run/'launch.json').exists());self.assertTrue((self.run/'exit.json').exists())
        self.assertFalse(scope_members(s['turn']['token']))

    def test_completion_missing_gets_no_credit_and_obeys_hard_deadline(self):
        self.init(PREAMBLE+'time.sleep(.05)', budget={'window_seconds':.05,'hard_limit_seconds':1.5},
                  turn={'seconds':.2})
        r=self.invoke('run','--run-id','test','--no-guard')
        self.assertEqual(r.returncode,3)
        s=self.read();self.assertEqual(s['status'],'EXPIRED')
        self.assertEqual(s['budget']['active_seconds'],0); self.assertGreaterEqual(s['turn']['number'],1)
        self.assertGreater(s['budget']['runtime_seconds'],0)

    def test_nonzero_exit_retries_without_a_count_limit(self):
        marker=self.base/'nonzero-once'
        code=PREAMBLE+f"""from pathlib import Path
marker=Path({str(marker)!r})
if not marker.exists():
 marker.write_text('failed')
 raise SystemExit(7)
emit('turn.completed',credit=True)
"""
        self.init(code,budget={'window_seconds':.05,'hard_limit_seconds':3})
        self.assertEqual(self.invoke('run','--run-id','test','--no-guard').returncode,0)
        self.assertGreaterEqual(self.read()['turn']['number'],2)
        self.assertEqual(self.read()['status'],'COMPLETED')
        first=json.loads((self.run/'turns/000001/exit.json').read_text())
        self.assertEqual(first['returncode'],7)
        self.assertFalse(first['credited'])

    def test_gpu_queue_wait_stays_in_same_round_and_excludes_credit(self):
        code = PREAMBLE + """emit('gpu.state',request_id='same-request',job_id='same-job',state='QUEUED')
time.sleep(.65)
emit('gpu.state',request_id='same-request',job_id='same-job',state='SESSION_CHANGED')
time.sleep(.1)
emit('gpu.state',request_id='same-request',job_id='same-job',state='RUNNING')
emit('heartbeat')
time.sleep(.12)
emit('gpu.state',request_id='same-request',job_id='same-job',state='SUCCEEDED')
emit('turn.completed',credit=True)
"""
        self.init(code, budget={'window_seconds':.1,'hard_limit_seconds':3},
                  turn={'seconds':.3}, heartbeat={'stale_after_seconds':.2})
        result = self.invoke('run', '--run-id', 'test')
        self.assertEqual(result.returncode, 0, result.stderr)
        state = self.read()
        self.assertEqual(state['turn']['number'], 1)
        self.assertEqual(state['retry']['used'], 0)
        self.assertGreater(state['budget']['runtime_seconds'], .7)
        self.assertLess(state['budget']['active_seconds'], .4)
        self.assertGreater(state['turn']['gpu_wait']['excluded_seconds'], .7)

    def test_gpu_queue_wait_preserves_wall_hard_deadline(self):
        code = PREAMBLE + """emit('gpu.state',request_id='same-request',state='QUEUED')
time.sleep(10)
"""
        self.init(code, budget={'window_seconds':.1,'hard_limit_seconds':.45},
                  turn={'seconds':.2}, heartbeat={'stale_after_seconds':.1})
        result = self.invoke('run', '--run-id', 'test')
        self.assertEqual(result.returncode, 3, result.stderr)
        state = self.read()
        self.assertEqual(state['turn']['number'], 1)
        self.assertEqual(state['stop_reason'], 'hard_limit')
        self.assertEqual(state['retry']['used'], 0)
        self.assertLess(state['budget']['active_seconds'], .2)

    def test_gpu_infeasible_exit_is_infrastructure_without_retry(self):
        code = PREAMBLE + """emit('gpu.state',request_id='same-request',state='INFEASIBLE')
raise SystemExit(7)
"""
        self.init(code)
        result = self.invoke('run', '--run-id', 'test', '--no-guard')
        self.assertEqual(result.returncode, 0, result.stderr)
        state = self.read()
        self.assertEqual(state['status'], 'PAUSED')
        self.assertEqual(state['turn']['number'], 1)
        self.assertEqual(state['turn']['reason'], 'gpu_infeasible')
        self.assertEqual(state['turn']['failure_class'], 'infrastructure')
        self.assertEqual(state['retry']['used'], 0)

    def test_gpu_infeasible_can_continue_cpu_work_in_the_same_round(self):
        code = PREAMBLE + """emit('gpu.state',request_id='same-request',state='INFEASIBLE')
time.sleep(.15)
emit('turn.completed',credit=True)
"""
        self.init(code, budget={'window_seconds':.1})
        result = self.invoke('run', '--run-id', 'test', '--no-guard')
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(self.read()['status'], 'COMPLETED')
        self.assertEqual(self.read()['turn']['number'], 1)

    def test_agent_reported_failure_retries_into_a_successful_turn(self):
        marker=self.base/'agent-failed-once'
        code=PREAMBLE+f"""from pathlib import Path
marker=Path({str(marker)!r})
if not marker.exists():
 marker.write_text('failed once')
 emit('turn.failed',reason='episode_interrupted')
 time.sleep(5)
else:
 emit('turn.completed',credit=True)
"""
        self.init(code,budget={'window_seconds':.05,'hard_limit_seconds':3},
                  policy={'retry_backoff_seconds':.01})
        result=self.invoke('run','--run-id','test','--no-guard')
        self.assertEqual(result.returncode,0,result.stderr)
        state=self.read()
        self.assertEqual(state['status'],'COMPLETED')
        self.assertGreaterEqual(state['turn']['number'],2)
        first=json.loads((self.run/'turns/000001/exit.json').read_text())
        self.assertEqual(first['reason'],'agent_reported_failure')
        self.assertFalse(first['credited'])
        second=json.loads((self.run/'turns/000002/exit.json').read_text())
        self.assertEqual(second['reason'],'turn_completed')
        events=[json.loads(line) for line in (self.run/'events.jsonl').read_text().splitlines()]
        self.assertTrue(any(item['event']=='turn.retry_scheduled' and
                            item['reason']=='agent_reported_failure' for item in events))

    def test_deterministic_evidence_failure_stops_once_without_credit(self):
        code = PREAMBLE + """from pathlib import Path
import hashlib,sys
path=Path(os.environ['AUTORESEARCH_TURN_DIR'])/'evidence-failure.json'
path.write_text(json.dumps({'status':'FINAL_SCORE_INVALID','score':None,'failure_code':'HARBOR_JOB_FAILED'}))
path.chmod(0o444)
print('Original Harbor permission traceback',file=sys.stderr,flush=True)
emit('turn.failed',reason='deterministic_evidence_failure',retryable=False,
     status='FINAL_SCORE_INVALID',failure_code='HARBOR_JOB_FAILED',score=None,
     method_summary='Actual method ran; raw Harbor permission error prevented sealing.',
     failure_evidence=str(path),failure_evidence_sha256=hashlib.sha256(path.read_bytes()).hexdigest())
raise SystemExit(70)
"""
        self.init(code, budget={'window_seconds':.05,'hard_limit_seconds':3})
        result = self.invoke('run', '--run-id', 'test', '--no-guard')
        self.assertEqual(result.returncode, 1, result.stderr)
        state = self.read()
        self.assertEqual(state['status'], 'FAILED')
        self.assertEqual(state['turn']['status'], 'FAILED')
        self.assertEqual(state['turn']['number'], 1)
        self.assertTrue(state['resume_required'])
        self.assertEqual(state['budget']['active_seconds'], 0)
        exited = json.loads((self.run/'turns/000001/exit.json').read_text())
        self.assertEqual(exited['reason'], 'deterministic_evidence_failure')
        self.assertEqual(exited['result']['failure_code'], 'HARBOR_JOB_FAILED')
        self.assertIsNone(exited['result']['score'])
        self.assertIn('Original Harbor permission traceback', (self.run/'turns/000001/stderr.log').read_text())
        events = [json.loads(line) for line in (self.run/'events.jsonl').read_text().splitlines()]
        self.assertFalse(any(item['event']=='turn.retry_scheduled' for item in events))

    def test_completion_contract_error_pauses_after_late_audited_credit(self):
        code = PREAMBLE + """from pathlib import Path
from datetime import datetime
import hashlib
td=Path(os.environ['AUTORESEARCH_TURN_DIR'])
evidence=td/'feedback.json'
evidence.write_text(json.dumps({'verified_score':.5}))
failure=td/'completion-error.json'
failure.write_text(json.dumps({'failure_class':'contract','failure_code':'DEADLINE_BINDING_MISMATCH'}))
emit('turn.failed',reason='completion_contract_error',retryable=False,failure_class='contract',
     failure_code='DEADLINE_BINDING_MISMATCH',
     method_summary='Training and scoring finished; completion contract rejected the result.',
     failure_evidence=str(failure),failure_evidence_sha256=hashlib.sha256(failure.read_bytes()).hexdigest())
time.sleep(.15)
start=datetime.fromisoformat(json.loads((td/'worker-start.json').read_text())['at']).timestamp()
report={'version':1,'run_id':'test','turn':int(os.environ['AUTORESEARCH_TURN']),
        'credited_seconds':.03,'intervals':[[start+.02,start+.05]],
        'evidence':[{'path':str(evidence),'sha256':hashlib.sha256(evidence.read_bytes()).hexdigest()}]}
path=td/'audited-credit.json'
path.write_text(json.dumps(report))
emit('turn.credit',credited_seconds=.03,credit_evidence=str(path),
     credit_evidence_sha256=hashlib.sha256(path.read_bytes()).hexdigest())
raise SystemExit(70)
"""
        self.init(code, budget={'window_seconds':.02,'hard_limit_seconds':3,
                               'credit_policy':'reported','allow_partial_credit':True})
        result=self.invoke('run','--run-id','test','--no-guard')
        self.assertEqual(result.returncode,0,result.stderr)
        state=self.read()
        self.assertEqual(state['status'],'PAUSED')
        self.assertEqual(state['stop_reason'],'completion_contract_error')
        self.assertEqual(state['turn']['number'],1)
        self.assertEqual(state['turn']['failure_class'],'contract')
        self.assertTrue(state['resume_required'])
        self.assertAlmostEqual(state['budget']['active_seconds'],.03,places=4)
        exited=json.loads((self.run/'turns/000001/exit.json').read_text())
        self.assertEqual(exited['reason'],'completion_contract_error')
        self.assertEqual(exited['returncode'],70)
        self.assertTrue(exited['credited'])
        self.assertEqual(exited['result']['failure_code'],'DEADLINE_BINDING_MISMATCH')
        self.assertEqual(exited['failure_class'],'contract')
        run_exit=json.loads((self.run/'exit.json').read_text())
        self.assertEqual(run_exit['failure_class'],'contract')
        self.assertEqual(run_exit['failure']['failure_code'],'DEADLINE_BINDING_MISMATCH')
        self.assertEqual(longrun.epoch(state['budget']['hard_deadline_at'])-
                         longrun.epoch(state['budget']['started_at']),3)
        events=[json.loads(line) for line in (self.run/'events.jsonl').read_text().splitlines()]
        self.assertFalse(any(item['event']=='turn.retry_scheduled' for item in events))

    def test_nonretryable_failure_artifact_requires_current_turn_regular_hashed_file(self):
        td=self.base/'turn'
        td.mkdir()
        outside=self.base/'outside.json'
        outside.write_text('{}')
        artifact=td/'failure.json'
        artifact.write_text('{}')
        link=td/'link.json'
        link.symlink_to(artifact)
        for path, digest, failure_class, retryable in (
                (outside,controller.sha256(outside),'contract',False),
                (link,controller.sha256(artifact),'contract',False),
                (artifact,'f'*64,'contract',False),
                (artifact,None,'contract',False),
                (artifact,controller.sha256(artifact),'infrastructure',False),
                (artifact,controller.sha256(artifact),'contract',True)):
            with self.subTest(path=path,digest=digest,failure_class=failure_class,retryable=retryable):
                supervisor=controller.LongRunController.__new__(controller.LongRunController)
                supervisor.state={'turn':{'number':1,'dir':str(td)},'context':{'generation':1}}
                supervisor.config={}
                supervisor.pending_reason=None
                supervisor.event=mock.Mock()
                event={'autoresearch':'turn.failed','generation':1,'reason':'completion_contract_error',
                       'failure_class':failure_class,'retryable':retryable,
                       'failure_evidence':str(path),'failure_evidence_sha256':digest}
                supervisor.ingest_line(json.dumps(event).encode())
                self.assertEqual(supervisor.pending_reason,'invalid_agent_event')
                self.assertNotIn('failure',supervisor.state)

    def test_later_events_cannot_turn_contract_failure_into_a_retry(self):
        supervisor=controller.LongRunController.__new__(controller.LongRunController)
        supervisor.state={'turn':{'number':1},'context':{'generation':1}}
        supervisor.config={'budget':{'allow_partial_credit':True}}
        supervisor.pending_reason='completion_contract_error'
        supervisor.result={'reason':'completion_contract_error'}
        supervisor.event=mock.Mock()
        for event in ({'autoresearch':'turn.completed','credit':True},
                      {'autoresearch':'turn.failed','reason':'episode_interrupted'},
                      {'autoresearch':'turn.credit','credited_seconds':-1}):
            with self.subTest(event=event):
                supervisor.ingest_line(json.dumps({**event,'generation':1}).encode())
                self.assertEqual(supervisor.pending_reason,'completion_contract_error')
                self.assertEqual(supervisor.result,{'reason':'completion_contract_error'})

    def test_completion_contract_failure_hang_obeys_existing_turn_timeout(self):
        code=PREAMBLE+"""from pathlib import Path
import hashlib
path=Path(os.environ['AUTORESEARCH_TURN_DIR'])/'completion-error.json'
path.write_text('{}')
emit('turn.failed',reason='completion_contract_error',retryable=False,failure_class='contract',
     failure_evidence=str(path),failure_evidence_sha256=hashlib.sha256(path.read_bytes()).hexdigest())
time.sleep(5)
"""
        self.init(code,budget={'window_seconds':.1,'hard_limit_seconds':2},turn={'seconds':.25})
        result=self.invoke('run','--run-id','test','--no-guard')
        self.assertEqual(result.returncode,0,result.stderr)
        state=self.read()
        self.assertEqual(state['status'],'PAUSED')
        self.assertEqual(state['turn']['reason'],'completion_contract_error')
        self.assertEqual(state['turn']['number'],1)
        self.assertFalse(state['turn']['credited'])
        self.assertEqual(state['budget']['active_seconds'],0)
        self.assertLess(state['budget']['runtime_seconds'],1)

    def test_completion_contract_failure_hang_never_extends_run_deadline(self):
        code=PREAMBLE+"""from pathlib import Path
import hashlib
path=Path(os.environ['AUTORESEARCH_TURN_DIR'])/'completion-error.json'
path.write_text('{}')
emit('turn.failed',reason='completion_contract_error',retryable=False,failure_class='contract',
     failure_evidence=str(path),failure_evidence_sha256=hashlib.sha256(path.read_bytes()).hexdigest())
time.sleep(5)
"""
        self.init(code,budget={'window_seconds':.1,'hard_limit_seconds':.45},turn={'seconds':.45})
        result=self.invoke('run','--run-id','test','--no-guard')
        self.assertEqual(result.returncode,3,result.stderr)
        state=self.read()
        self.assertEqual(state['status'],'EXPIRED')
        self.assertEqual(state['stop_reason'],'hard_limit')
        self.assertEqual(state['turn']['failure_class'],'contract')
        self.assertEqual(state['turn']['number'],1)
        self.assertEqual(state['retry']['used'],0)
        self.assertEqual(state['budget']['active_seconds'],0)
        self.assertLess(state['budget']['runtime_seconds'],1)

    def test_retry_backoff_longer_than_stale_limit_keeps_guard_alive(self):
        marker = self.base/'backoff-fail-once'
        code = PREAMBLE + f"""from pathlib import Path
marker=Path({str(marker)!r})
if not marker.exists():
 marker.write_text('failed once')
 raise SystemExit(7)
time.sleep(.08)
emit('turn.completed',credit=True)
"""
        self.init(code, budget={'window_seconds':.05,'hard_limit_seconds':4},
                  heartbeat={'controller_stale_seconds':.25},
                  policy={'retry_backoff_seconds':.7})
        result = self.invoke('run','--run-id','test')
        self.assertEqual(result.returncode,0,result.stderr)
        state = self.read()
        self.assertEqual(state['status'],'COMPLETED')
        self.assertEqual(state['turn']['number'],2)
        first = json.loads((self.run/'turns/000001/exit.json').read_text())
        self.assertFalse(first['credited'])
        self.assertEqual(first['credited_seconds'],0)
        self.assertLess(state['budget']['runtime_seconds'],.7)
        events = [json.loads(line) for line in (self.run/'events.jsonl').read_text().splitlines()]
        self.assertFalse(any(item['event']=='guard.stop' for item in events))

    def test_retry_backoff_obeys_original_hard_deadline_with_guard(self):
        self.init(PREAMBLE+"raise SystemExit(7)",
                  budget={'window_seconds':.05,'hard_limit_seconds':.6},
                  turn={'seconds':.4},
                  heartbeat={'controller_stale_seconds':.25},
                  policy={'retry_backoff_seconds':2})
        result = self.invoke('run','--run-id','test')
        self.assertEqual(result.returncode,3,result.stderr)
        state = self.read()
        self.assertEqual(state['status'],'EXPIRED')
        self.assertEqual(state['stop_reason'],'hard_limit')
        self.assertEqual(state['turn']['number'],1)
        self.assertEqual(state['budget']['active_seconds'],0)

    def test_operator_stop_during_retry_backoff_does_not_start_another_turn(self):
        self.init(PREAMBLE+"time.sleep(.1); raise SystemExit(7)",
                  policy={'retry_backoff_seconds':2})
        process = self.launch()
        self.until(lambda: self.read()['turn'].get('reason')=='agent_exit_nonzero')
        result = self.invoke('stop','--run-id','test','--reason','stop during backoff')
        self.assertEqual(result.returncode,0,result.stderr)
        process.wait(timeout=3)
        state = self.read()
        self.assertEqual(state['turn']['number'],1)
        self.assertEqual(state['budget']['active_seconds'],0)
        self.assertFalse(scope_members(state['turn']['token']))

    def test_abnormal_turn_retries_with_distinct_evidence_and_preserves_resources(self):
        marker = self.base/'fail-once'
        cleanup_log = self.base/'cleanup-flags'
        code = PREAMBLE + f"""from pathlib import Path
marker=Path({str(marker)!r})
if not marker.exists():
 marker.write_text('failed once')
 raise SystemExit(7)
time.sleep(.08)
emit('turn.completed',credit=True)
"""
        cleanup_code = f"import os;open({str(cleanup_log)!r},'a').write(os.environ['AUTORESEARCH_RETRY_PENDING']+'\\n')"
        self.init(code, budget={'window_seconds':.05,'hard_limit_seconds':6},
                  cleanup={'command':[sys.executable,'-B','-c',cleanup_code],'timeout_seconds':2},
                  policy={'retry_backoff_seconds':.01})
        result=self.invoke('run','--run-id','test','--no-guard')
        self.assertEqual(result.returncode,0,result.stderr)
        state=self.read()
        self.assertEqual(state['status'],'COMPLETED')
        self.assertEqual(state['turn']['number'],2)
        first=json.loads((self.run/'turns/000001/exit.json').read_text())
        second=json.loads((self.run/'turns/000002/exit.json').read_text())
        self.assertEqual(first['reason'],'agent_exit_nonzero')
        self.assertFalse(first['credited'])
        self.assertEqual(second['retry_of'],1)
        self.assertEqual(second['retry_index'],1)
        self.assertEqual(cleanup_log.read_text().splitlines(),['1','0'])
        events=[json.loads(line) for line in (self.run/'events.jsonl').read_text().splitlines()]
        self.assertEqual(sum(item['event']=='turn.retry_scheduled' for item in events),1)

    def test_retries_continue_until_hard_limit_and_failure_evidence_is_kept(self):
        self.init(PREAMBLE+"raise SystemExit(9)",
                  budget={'window_seconds':.1,'hard_limit_seconds':1.5},
                  turn={'seconds':.2},
                  policy={'retry_backoff_seconds':.01})
        result=self.invoke('run','--run-id','test','--no-guard')
        self.assertEqual(result.returncode,3)
        state=self.read()
        self.assertEqual(state['status'],'EXPIRED')
        self.assertGreater(state['turn']['number'],5)
        self.assertEqual(state['stop_reason'],'hard_limit')
        for number in range(1,state['turn']['number']+1):
            self.assertTrue((self.run/f'turns/{number:06d}/exit.json').exists())

    def test_unexpected_worker_exit_reclaims_attempt_scope_then_retries(self):
        marker=self.base/'child-started'
        code=PREAMBLE+f"""from pathlib import Path
marker=Path({str(marker)!r})
if not marker.exists():
 marker.write_text('started')
 time.sleep(5)
emit('turn.completed',credit=True)
"""
        self.init(code,budget={'window_seconds':.05,'hard_limit_seconds':6},
                  policy={'retry_backoff_seconds':0})
        controller_process=self.launch()
        self.until(marker.exists)
        state=self.read()
        signal_identity(state['turn']['pid'],state['turn']['pid_start_ticks'],signal.SIGKILL,
                       state['process_boot_id'])
        self.until(lambda:self.read()['turn']['number']==2 and self.read()['turn']['status']=='RUNNING')
        controller_process.wait(timeout=5)
        first=json.loads((self.run/'turns/000001/exit.json').read_text())
        second=json.loads((self.run/'turns/000002/exit.json').read_text())
        self.assertEqual(first['reason'],'worker_exit_missing')
        self.assertFalse(scope_members(first['token']))
        self.assertEqual(second['retry_of'],1)
        self.assertEqual(self.read()['status'],'COMPLETED')

    def test_turn_ceiling_and_retry_policy_validation(self):
        cfg=config(self.base,'pass')
        cfg['budget']['hard_limit_seconds']=7200
        cfg.pop('turn')
        self.assertEqual(longrun.validate_config(cfg)['turn']['seconds'],5400)
        cfg=config(self.base,'pass',turn={'seconds':5401})
        cfg['budget']['hard_limit_seconds']=7200
        with self.assertRaisesRegex(longrun.ControllerError,'5400'):
            longrun.validate_config(cfg)
        cfg=config(self.base,'pass',policy={'max_turn_retries':6})
        with self.assertRaisesRegex(longrun.ControllerError,'unknown fields'):
            longrun.validate_config(cfg)

    def test_cleanup_report_credits_nonzero_turn_without_rewriting_failure(self):
        evidence = self.base / 'trusted-feedback.json'
        evidence.write_text('{"gpu_feedback":"verified fixture"}')
        hook = f"""import os,json,hashlib
from pathlib import Path
from datetime import datetime
td=Path(os.environ['AUTORESEARCH_TURN_DIR'])
start=datetime.fromisoformat(json.loads((td/'worker-start.json').read_text())['at']).timestamp()
p=Path({str(evidence)!r})
report={{'version':1,'run_id':'test','turn':int(os.environ['AUTORESEARCH_TURN']),
 'credited_seconds':.03,'intervals':[[start+.02,start+.05]],
 'evidence':[{{'path':str(p),'sha256':hashlib.sha256(p.read_bytes()).hexdigest()}}]}}
(td/'partial-credit.json').write_text(json.dumps(report))
"""
        self.init(PREAMBLE+'time.sleep(.12); raise SystemExit(7)',
                  budget={'window_seconds':.02,'hard_limit_seconds':4,'credit_policy':'reported','allow_partial_credit':True},
                  cleanup={'command':[sys.executable,'-B','-c',hook],'timeout_seconds':1})
        result=self.invoke('run','--run-id','test','--no-guard')
        self.assertEqual(result.returncode,0,result.stderr)
        state=self.read()
        self.assertAlmostEqual(state['budget']['active_seconds'],.03,places=4)
        exited=json.loads((self.run/'turns/000001/exit.json').read_text())
        self.assertEqual(exited['reason'],'agent_exit_nonzero')
        self.assertEqual(exited['returncode'],7)
        self.assertEqual(exited['status'],'STOPPED')
        self.assertTrue(exited['credited'])

    def test_amend_preserves_identity_and_original_config_and_rejects_recount(self):
        cfg=self.init(budget={'hard_limit_seconds':20,'credit_policy':'reported'})
        state=self.read()
        longrun.begin_run(state)
        state.update(status='STOPPED',resume_required=True)
        state['turn'].update(number=1,status='STOPPED')
        td=self.run/'turns/000001';td.mkdir()
        began=longrun.epoch(state['budget']['started_at'])
        from datetime import datetime,timezone
        def stamp(at):return datetime.fromtimestamp(at,timezone.utc).isoformat()
        longrun.atomic_json(td/'exit.json',{'started_at':stamp(began),'ended_at':stamp(began+1),
            'elapsed_seconds':1,'credited':False,'credited_seconds':0,'reason':'turn_timeout'})
        longrun.save_state(self.run,state)
        evidence=self.base/'original-session';evidence.write_text('native session fixture')
        audit={'run_id':'test','turns':[{'turn':1,'intervals':[[began+.1,began+.3]]}],
               'evidence':[{'path':str(evidence),'sha256':controller.sha256(evidence)}]}
        cfg['budget'].update(hard_limit_seconds=25,allow_partial_credit=True)
        original=(self.run/'config.json').read_bytes()
        receipt=controller.amend_run(self.run,cfg,expected_sha256=state['config_sha256'],expected_turn=1,
                                    reason='authorized paused migration',credit=audit)
        amended=self.read()
        self.assertEqual(amended['run_id'],state['run_id'])
        self.assertEqual(amended['budget']['started_at'],state['budget']['started_at'])
        self.assertEqual((self.run/'config.json').read_bytes(),original)
        self.assertAlmostEqual(amended['budget']['active_seconds'],.2,places=4)
        self.assertEqual(controller.task_config(self.run)['budget']['hard_limit_seconds'],25)
        with self.assertRaisesRegex(longrun.ControllerError,'already audited'):
            controller.amend_run(self.run,cfg,expected_sha256=amended['config_sha256'],expected_turn=1,
                                 reason='duplicate audit',credit=audit)

    def test_expired_amend_requires_explicit_extension_and_preserves_accounting(self):
        cfg = self.init(budget={'hard_limit_seconds': 20, 'credit_policy': 'reported'})
        state = self.read()
        from datetime import datetime, timezone
        began = datetime.fromtimestamp(time.time() - 21, timezone.utc).isoformat()
        longrun.begin_run(state, now=began)
        state.update(status='EXPIRED', resume_required=True, stop_reason='hard_limit')
        state['budget']['active_seconds'] = .1
        longrun.save_state(self.run, state)
        original = (self.run / 'config.json').read_bytes()
        cfg['budget'].update(hard_limit_seconds=25, allow_extended_hard_limit=True)
        arguments = dict(expected_sha256=state['config_sha256'], expected_turn=0,
                         reason='user authorized a later absolute deadline')
        with self.assertRaisesRegex(longrun.ControllerError, 'explicit'):
            controller.amend_run(self.run, cfg, **arguments)
        with self.assertRaisesRegex(longrun.ControllerError, 'historical credit'):
            controller.amend_run(self.run, cfg, **arguments, extend_expired_budget=True,
                                 credit={'turns': [{'turn': 1}]})
        receipt = controller.amend_run(self.run, cfg, **arguments, extend_expired_budget=True)
        amended = self.read()
        self.assertEqual(amended['status'], 'PAUSED')
        self.assertTrue(amended['resume_required'])
        self.assertEqual(amended['run_id'], state['run_id'])
        self.assertEqual(amended['budget']['started_at'], state['budget']['started_at'])
        self.assertEqual(amended['budget']['active_seconds'], .1)
        self.assertEqual(amended['budget']['window_seconds'], state['budget']['window_seconds'])
        self.assertEqual((self.run / 'config.json').read_bytes(), original)
        self.assertTrue(receipt['amendment']['expired_budget_extended'])

    def test_storage_migration_requires_exact_preserved_copy_and_keeps_budget(self):
        import storage_migration
        mount = self.base / 'new-volume'
        root = mount / 'agent'
        root.mkdir(parents=True)
        (root / 'candidate.py').write_text('original candidate')
        self.state = mount / 'state'
        self.run = self.state / 'runs/test'
        old_storage = {'verified': True, 'mount': str(self.base / 'old-volume'),
                       'device': self.base.stat().st_dev}
        cfg = config(root, 'pass', budget={'hard_limit_seconds': 60},
                     storage={'data_mount': old_storage['mount']})
        with mock.patch.object(controller, 'check_storage', return_value=old_storage):
            controller.init_run(self.base / 'config.json', self.state, 'test', config=cfg)
        state = self.read()
        longrun.begin_run(state)
        state.update(status='STOPPED', resume_required=True)
        state['budget']['active_seconds'] = .1
        longrun.save_state(self.run, state)
        state = self.read()
        (self.run / '.controller.lock').touch()
        (self.run / '.state.lock').touch()
        source = self.base / 'preserved-copy'
        shutil.copytree(mount, source, symlinks=True)
        cfg['storage']['data_mount'] = str(mount)
        new_storage = {'verified': True, 'mount': str(mount), 'device': old_storage['device'] + 1}
        args = dict(expected_sha256=state['config_sha256'], expected_turn=0,
                    reason='user authorized independent data-volume migration')
        with mock.patch.object(controller, 'check_storage', return_value=new_storage), \
             mock.patch.object(storage_migration, 'check_storage', return_value=old_storage):
            with self.assertRaisesRegex(longrun.ControllerError, 'explicit'):
                controller.amend_run(self.run, cfg, **args)
            (root / 'candidate.py').write_text('corrupted copy')
            with self.assertRaisesRegex(longrun.ControllerError, 'differs'):
                controller.amend_run(self.run, cfg, **args, storage_migration_source=source)
            self.assertEqual(self.read(), state)
            (root / 'candidate.py').write_text('original candidate')
            receipt = controller.amend_run(self.run, cfg, **args, storage_migration_source=source)
        amended = self.read()
        self.assertEqual(amended['storage'], new_storage)
        self.assertEqual(amended['run_id'], state['run_id'])
        self.assertEqual(amended['context'], state['context'])
        self.assertEqual(amended['budget'], state['budget'])
        self.assertEqual(receipt['amendment']['credited_seconds_added'], 0)
        proof = receipt['amendment']['storage_migration']
        self.assertEqual(controller.sha256(Path(proof['path'])), proof['sha256'])
        self.assertTrue((source / 'state/runs/test/state.json').exists())

    def test_storage_migration_rejects_incomplete_cleanup_and_special_files(self):
        import storage_migration
        mount, source = self.base / 'new-volume', self.base / 'old-volume'
        run = mount / 'run'
        run.mkdir(parents=True)
        source.mkdir()
        turn = run / 'turn'
        turn.mkdir()
        longrun.atomic_json(turn / 'launch.json', {'cleanup': {'command': ['false']}})
        old = {'verified': True, 'mount': str(source), 'device': source.stat().st_dev}
        new = {'verified': True, 'mount': str(mount), 'device': old['device'] + 1}
        state = {'status': 'FAILED', 'storage': old, 'turn': {'dir': str(turn)}}
        with mock.patch.object(storage_migration, 'check_storage', return_value=old):
            with self.assertRaisesRegex(longrun.ControllerError, 'cleanup'):
                storage_migration.verify_migration(run, run, state, new, source)
        os.mkfifo(source / 'socket-like-file')
        with self.assertRaisesRegex(longrun.ControllerError, 'special file'):
            storage_migration.tree_manifest(source)

    def test_expired_scored_amend_reseals_contract_at_new_deadline(self):
        from datetime import datetime, timedelta, timezone
        candidate = self.base / 'candidate'
        evidence = self.base / 'private-evidence'
        keys = self.base / 'private-keys'
        candidate.mkdir()
        evidence.mkdir(mode=0o700)
        keys.mkdir(mode=0o700)
        key = keys / 'completion.key'
        key.write_bytes(os.urandom(32))
        key.chmod(0o600)
        old_deadline = (datetime.now(timezone.utc) + timedelta(seconds=60)).isoformat()
        cfg = config(candidate, 'true', stage='formal', score_expectation='required',
                     required_seeds=['0'], deadline=old_deadline,
                     completion={'evidence_root': str(evidence), 'signing_key': str(key),
                                 'protocol_manifest': 'protocol.json', 'data_hash': '0' * 64,
                                 'evaluator_hash': '1' * 64, 'training': True,
                                 'jobs_manifest': 'jobs.json', 'result': 'result.json',
                                 'receipt': 'receipt.json', 'isolation': 'isolation.json',
                                 'private_roots': []},
                     budget={'hard_limit_seconds': 20})
        (self.base / 'config.json').write_text(json.dumps(cfg))
        self.assertEqual(self.invoke('init', '--config', self.base / 'config.json', '--run-id', 'test').returncode, 0)
        state = self.read()
        began = datetime.fromtimestamp(time.time() - 21, timezone.utc).isoformat()
        longrun.begin_run(state, now=began)
        state.update(status='EXPIRED', resume_required=True, stop_reason='hard_limit')
        longrun.save_state(self.run, state)
        previous_contract = json.loads((self.run / 'completion.contract.json').read_text())
        new_deadline = (datetime.now(timezone.utc) + timedelta(seconds=120)).isoformat()
        amended_cfg = json.loads(json.dumps(cfg))
        amended_cfg['deadline'] = new_deadline
        amended_cfg['budget'].update(hard_limit_seconds=200, allow_extended_hard_limit=True)
        result = controller.amend_run(self.run, amended_cfg, expected_sha256=state['config_sha256'],
                                      expected_turn=0, reason='user authorized scored deadline extension',
                                      extend_expired_budget=True)
        amended = self.read()
        amendment = self.run / 'amendments' / '000001'
        self.assertEqual(json.loads((amendment / 'previous-completion-contract.json').read_text()), previous_contract)
        current_contract = json.loads((self.run / 'completion.contract.json').read_text())
        self.assertEqual(current_contract['score_expectation'], 'required')
        self.assertEqual(current_contract['deadline'], amended['budget']['hard_deadline_at'])
        self.assertEqual(current_contract['declared_deadline'], new_deadline)
        self.assertEqual(amended['completion']['contract_hash'], controller.digest(current_contract))
        self.assertEqual(amended['budget']['started_at'], state['budget']['started_at'])
        self.assertEqual(amended['budget']['active_seconds'], state['budget']['active_seconds'])
        self.assertEqual(amended['status'], 'PAUSED')
        self.assertTrue(result['amendment']['expired_budget_extended'])

    def test_stopped_run_can_extend_before_original_deadline_with_explicit_authorization(self):
        from datetime import datetime, timedelta, timezone
        cfg = self.init(budget={'hard_limit_seconds': 3600, 'allow_extended_hard_limit': True})
        state = self.read()
        longrun.begin_run(state)
        state.update(status='STOPPED', resume_required=True)
        longrun.save_state(self.run, state)
        amended = json.loads(json.dumps(cfg))
        amended['deadline'] = (datetime.now(timezone.utc) + timedelta(hours=8)).isoformat()
        amended['budget']['hard_limit_seconds'] = 3600 + 8 * 3600
        arguments = dict(expected_sha256=state['config_sha256'], expected_turn=0,
                         reason='user authorized pre-expiry eight-hour extension')
        with self.assertRaisesRegex(longrun.ControllerError, 'explicit'):
            controller.amend_run(self.run, amended, **arguments)
        result = controller.amend_run(self.run, amended, **arguments, extend_expired_budget=True)
        current = self.read()
        self.assertEqual(current['status'], 'STOPPED')
        self.assertEqual(current['budget']['started_at'], state['budget']['started_at'])
        self.assertEqual(current['budget']['active_seconds'], state['budget']['active_seconds'])
        self.assertEqual(current['declared_deadline'], amended['deadline'])
        self.assertFalse(result['amendment']['expired_budget_extended'])

    def test_stopped_run_past_deadline_requires_explicit_extension(self):
        cfg = self.init(budget={'hard_limit_seconds': 20, 'credit_policy': 'reported'})
        state = self.read()
        from datetime import datetime, timezone
        began = datetime.fromtimestamp(time.time() - 21, timezone.utc).isoformat()
        longrun.begin_run(state, now=began)
        state.update(status='STOPPED', resume_required=True)
        longrun.save_state(self.run, state)
        cfg['budget'].update(hard_limit_seconds=25, allow_extended_hard_limit=True)
        arguments = dict(expected_sha256=state['config_sha256'], expected_turn=0,
                         reason='user authorized deadline extension')
        with self.assertRaisesRegex(longrun.ControllerError, 'explicit'):
            controller.amend_run(self.run, cfg, **arguments)
        result = controller.amend_run(self.run, cfg, **arguments, extend_expired_budget=True)
        self.assertEqual(self.read()['status'], 'STOPPED')
        self.assertFalse(result['status']['budget']['view']['hard_reached'])
        self.assertTrue(self.read()['resume_required'])

    def test_extension_cannot_reopen_completed_target_or_shorten_deadline(self):
        cfg = self.init(budget={'hard_limit_seconds': 20})
        state = self.read()
        longrun.begin_run(state)
        state.update(status='STOPPED', resume_required=True)
        longrun.save_state(self.run, state)
        arguments = dict(expected_sha256=state['config_sha256'], expected_turn=0,
                         reason='user authorized deadline extension', extend_expired_budget=True)
        cfg['budget']['allow_extended_hard_limit'] = True
        with self.assertRaisesRegex(longrun.ControllerError, 'later deadline'):
            controller.amend_run(self.run, cfg, **arguments)
        cfg['budget']['hard_limit_seconds'] = 25
        state['budget']['active_seconds'] = state['budget']['window_seconds']
        longrun.save_state(self.run, state)
        with self.assertRaisesRegex(longrun.ControllerError, 'completed budget'):
            controller.amend_run(self.run, cfg, **arguments)

    def test_default_context_contract_is_150k_and_auto_compacting(self):
        cfg = {
            'version': 1, 'task_id': 'default-context', 'root': str(self.base),
            **{k: config(self.base, 'pass')[k] for k in ('stage', 'score_expectation', 'metric', 'direction',
                'required_seeds', 'deadline', 'candidate_manifest', 'protocol_hash')},
            'command': ['true'],
            'budget': {'mode': 'active', 'window_seconds': 1, 'hard_limit_seconds': 2,
                       'credit_policy': 'successful_turn'},
        }
        validated = longrun.validate_config(cfg)
        self.assertEqual(validated['context']['max_tokens'], 150000)
        self.assertEqual(validated['context']['compact_at_tokens'], 120000)
        self.assertEqual(validated['context']['reserve_tokens'], 16384)
        self.assertTrue(validated['context']['auto_compact'])

    def test_over_limit_completed_turn_is_compacted_and_credited(self):
        code = PREAMBLE + """from pathlib import Path
ctx=json.loads(Path(os.environ['AUTORESEARCH_CONTEXT_FILE']).read_text())
if G == 0:
 emit('context.usage', used_tokens=5000, conversation_id='chat-'+str(G))
 emit('context.compact', summary='Keep the completed candidate and its evidence.')
 time.sleep(.08)
 emit('turn.completed', credit=True)
else:
 assert ctx['handoff']['summary']=='Keep the completed candidate and its evidence.'
 emit('turn.completed', credit=True)
"""
        self.init(code, context={'max_tokens':4096, 'compact_at_tokens':3000,
                                  'reserve_tokens':256, 'auto_compact':True,
                                  'summary_seconds':.2},
                  budget={'window_seconds':.1, 'hard_limit_seconds':2})
        started = time.monotonic()
        result = self.invoke('run', '--run-id', 'test', '--no-guard')
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertLess(time.monotonic() - started, 2)
        state = self.read()
        self.assertEqual(state['status'], 'COMPLETED')
        self.assertEqual(state['context']['generation'], 1)
        self.assertTrue(json.loads((self.run/'turns/000001/exit.json').read_text())['credited'])
        events = [json.loads(line) for line in (self.run/'events.jsonl').read_text().splitlines()]
        self.assertTrue(any(item['event'] == 'context.compacted' for item in events))

    def test_context_hard_guard_stops_noncooperative_agent_without_summary(self):
        code = PREAMBLE + """emit('context.usage', used_tokens=5000, conversation_id='chat-'+str(G)); time.sleep(5)"""
        self.init(code, context={'max_tokens':4096, 'compact_at_tokens':3000,
                                  'reserve_tokens':256, 'summary_seconds':.2,
                                  'auto_compact':False},
                  budget={'window_seconds':.1, 'hard_limit_seconds':2})
        started = time.monotonic()
        result = self.invoke('run', '--run-id', 'test', '--no-guard')
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertLess(time.monotonic() - started, 2)
        state = self.read()
        self.assertEqual(state['status'], 'WAITING_COMPACTION')
        self.assertEqual(state['context']['generation'], 0)
        self.assertTrue(state['context']['limit_exceeded'])
        events = [json.loads(line) for line in (self.run/'events.jsonl').read_text().splitlines()]
        self.assertTrue(any(item['event'] == 'context.guard_triggered' for item in events))

    def test_auto_compact_fallback_continues_without_agent_summary(self):
        code = PREAMBLE + """if G == 0:
 emit('context.usage', used_tokens=3500, conversation_id='chat-'+str(G))
 time.sleep(5)
else:
 emit('turn.completed', credit=True)
"""
        self.init(code, context={'max_tokens':4096, 'compact_at_tokens':3000,
                                  'reserve_tokens':256, 'summary_seconds':.05,
                                  'auto_compact':True},
                  budget={'window_seconds':.01, 'hard_limit_seconds':2})
        result = self.invoke('run', '--run-id', 'test', '--no-guard')
        self.assertEqual(result.returncode, 0, result.stderr)
        state = self.read()
        self.assertEqual(state['status'], 'COMPLETED')
        self.assertEqual(state['context']['generation'], 1)
        fallback = self.run/'turns/000001/context-fallback.json'
        self.assertTrue(fallback.exists())
        self.assertFalse(json.loads(fallback.read_text())['complete'])

    def test_context_fallback_preserves_resources_for_next_generation(self):
        stopped = self.base/'resource-stopped'
        cleanup_code = f"import os;from pathlib import Path;p=Path({str(stopped)!r});p.write_text('stopped') if os.environ['AUTORESEARCH_RETRY_PENDING']=='0' else None"
        code = PREAMBLE + f"""from pathlib import Path
if G == 0:
 emit('context.usage', used_tokens=5000, conversation_id='chat-'+str(G))
 time.sleep(5)
else:
 assert not Path({str(stopped)!r}).exists(), 'cleanup stopped the shared resource'
 emit('turn.completed', credit=True)
"""
        self.init(code, context={'auto_compact':True},
                  budget={'window_seconds':.01, 'hard_limit_seconds':3},
                  cleanup={'command':[sys.executable, '-B', '-c', cleanup_code], 'timeout_seconds':1})
        result = self.invoke('run', '--run-id', 'test', '--no-guard')
        self.assertEqual(result.returncode, 0, result.stderr)
        state = self.read()
        self.assertEqual(state['status'], 'COMPLETED')
        self.assertEqual(state['context']['generation'], 1)
        first = json.loads((self.run/'turns/000001/cleanup-retry-exit.json').read_text())
        self.assertTrue(first['retry_pending'])
        self.assertTrue(stopped.exists())

    def test_hard_wall_applies_to_active_and_resume(self):
        self.init(PREAMBLE+'time.sleep(5)',budget={'window_seconds':.45,'hard_limit_seconds':.7},
                  turn={'seconds':.7},heartbeat={'stale_after_seconds':3})
        r=self.invoke('run','--run-id','test','--no-guard');s=self.read()
        self.assertEqual(r.returncode,3,r.stderr)
        self.assertEqual(s['status'],'EXPIRED');self.assertEqual(s['budget']['active_seconds'],0)
        self.assertIsNotNone(s['budget']['hard_deadline_at'])
        deadline=s['budget']['hard_deadline_at']
        self.assertNotEqual(self.invoke('run','--run-id','test','--resume').returncode,0)
        self.assertEqual(self.read()['budget']['hard_deadline_at'],deadline)

    def test_wall_target_stops_inflight_turn(self):
        self.init(PREAMBLE+'time.sleep(5)',budget={'mode':'wall','window_seconds':.5},heartbeat={'stale_after_seconds':3})
        started=time.monotonic();r=self.invoke('run','--run-id','test','--no-guard')
        self.assertEqual(r.returncode,0,r.stderr);self.assertEqual(self.read()['status'],'COMPLETED')
        self.assertLess(time.monotonic()-started,2)

    def test_absent_first_heartbeat_retries_until_hard_deadline(self):
        self.init("print('{\"event\":\"heartbeat\"}',flush=True); import time; time.sleep(5)",
                  budget={'window_seconds':.1,'hard_limit_seconds':.7},turn={'seconds':.2},
                  heartbeat={'stale_after_seconds':.2},context={'required':False})
        self.assertEqual(self.invoke('run','--run-id','test','--no-guard').returncode,3)
        state=self.read()
        self.assertEqual(state['status'],'EXPIRED')
        self.assertGreater(state['turn']['number'],1)

    def test_fragmented_json_and_unicode(self):
        code=PREAMBLE+"""import sys
line=json.dumps({'autoresearch':'context.usage','used_tokens':123,'generation':G,'conversation_id':'chat-'+str(G)},ensure_ascii=False)+'\\n'
for part in (line[:30],line[30:]):
 sys.stdout.write(part);sys.stdout.flush();time.sleep(.06)
emit('turn.completed',credit=True,note='中文')
"""
        self.init(code,budget={'window_seconds':.1})
        r=self.invoke('run','--run-id','test','--no-guard')
        self.assertEqual(r.returncode,0,r.stderr);self.assertEqual(self.read()['context']['used_tokens'],123)

    def test_invalid_generation_and_token_reset_fail_closed(self):
        self.init(PREAMBLE+"emit('context.usage',used_tokens=1,conversation_id='chat-'+str(G)); time.sleep(3)",
                  budget={'window_seconds':.1,'hard_limit_seconds':.7},turn={'seconds':.2},
                  policy={'retry_backoff_seconds':.01})
        r=self.invoke('run','--run-id','test','--no-guard')
        self.assertEqual(r.returncode,3)
        self.assertEqual(self.read()['status'],'EXPIRED')
        self.assertGreater(self.read()['turn']['number'],1)

    def test_context_threshold_then_manual_compact_and_reopen(self):
        self.init(PREAMBLE+"emit('context.usage',used_tokens=3500,conversation_id='chat-'+str(G)); time.sleep(3)",
                  context={'auto_compact':False})
        self.assertEqual(self.invoke('run','--run-id','test','--no-guard').returncode,0)
        self.assertEqual(self.read()['status'],'WAITING_COMPACTION')
        summary=self.base/'summary.md';summary.write_text('Keep candidate hash and evidence paths.')
        args=('context','compact','--run-id','test','--generation','0','--summary-file',summary)
        r=self.invoke(*args);self.assertEqual(r.returncode,0,r.stderr)
        self.assertNotEqual(self.invoke(*args).returncode,0)
        r=self.invoke('context','reopen','--run-id','test','--generation','1','--summary-file',summary)
        self.assertEqual(r.returncode,0,r.stderr)
        self.assertEqual(self.read()['context']['generation'],2)
        self.assertEqual(len(list((self.run/'context').glob('snapshot-*.json'))),2)

    def test_auto_compact_handoff_reaches_next_generation(self):
        code=PREAMBLE+"""from pathlib import Path
ctx=json.loads(Path(os.environ['AUTORESEARCH_CONTEXT_FILE']).read_text())
if G==0: emit('context.compact',summary='Preserve this candidate.')
else: assert ctx['handoff']['summary']=='Preserve this candidate.'
time.sleep(.08);emit('turn.completed',credit=True)
"""
        self.init(code,context={'auto_compact':True},budget={'window_seconds':.35})
        r=self.invoke('run','--run-id','test','--no-guard')
        self.assertEqual(r.returncode,0,r.stderr)
        self.assertEqual(self.read()['status'],'COMPLETED');self.assertEqual(self.read()['context']['generation'],1)
        self.assertTrue((self.run/'turns/000001/stdout.log').is_file())

    def test_running_context_mutations_and_duplicate_controller_rejected(self):
        self.init(PREAMBLE+'time.sleep(5)');p=self.launch()
        summary=self.base/'summary.md';summary.write_text('summary')
        for args in [('run','--run-id','test'),('context','reopen','--run-id','test','--generation','0','--summary-file',summary),
                     ('context','compact','--run-id','test','--generation','0','--summary-file',summary,'--force')]:
            self.assertNotEqual(self.invoke(*args).returncode,0)
        self.assertEqual(self.read()['context']['generation'],0)
        self.invoke('stop','--run-id','test','--reason','done');p.wait(timeout=6)

    def test_stop_preserves_summary_and_requires_explicit_resume(self):
        self.init(PREAMBLE+'time.sleep(5)');p=self.launch()
        self.invoke('stop','--run-id','test','--reason','operator test');p.wait(timeout=6)
        self.assertEqual(self.read()['status'],'STOPPED')
        deadline=self.read()['budget']['hard_deadline_at']
        summary=self.base/'summary.md';summary.write_text('handoff after stop')
        r=self.invoke('context','reopen','--run-id','test','--generation','0','--summary-file',summary)
        self.assertEqual(r.returncode,0,r.stderr);self.assertEqual(self.read()['status'],'STOPPED')
        self.assertNotEqual(self.invoke('run','--run-id','test','--no-guard').returncode,0)
        self.assertEqual(self.read()['budget']['hard_deadline_at'],deadline)

    def test_official_stop_is_terminal_and_archivable_in_monitor(self):
        from tools.gpu_monitor import monitor, registry

        self.init(PREAMBLE + 'time.sleep(5)')
        process = self.launch()
        self.assertEqual(self.invoke('stop', '--run-id', 'test', '--reason', 'monitor regression').returncode, 0)
        self.assertEqual(process.wait(timeout=6), 0)
        self.assertEqual(self.read()['status'], 'STOPPED')
        self.assertEqual(json.loads((self.run / 'exit.json').read_text())['exit_code'], 0)
        path = self.base / 'monitor.json'
        path.write_text(json.dumps({'version': 1, 'hosts': {'local': {'transport': 'local'}},
            'state_dir': 'monitor-state',
            'tasks': [{'id': 'stopped', 'host': 'local', 'root': str(self.run), 'uses_gpu': False,
                       'controller': {'type': 'research_handoff', 'run_id': 'test'},
                       'status': {'path': 'state.json', 'key': 'status'},
                       'exit': {'path': 'exit.json', 'key': 'exit_code'},
                       'processes': [{'pid': process.pid, 'contains': [str(ROOT / 'controller.py')]}]}]}))
        observed = monitor.snapshot(monitor.load_config(path))
        task = observed['tasks'][0]
        self.assertEqual(task['state'], 'STOPPED')
        self.assertEqual(task['state_source'], 'declared_state')
        self.assertNotIn('STATUS_CONFLICT', task['alerts'])
        self.assertTrue(monitor.all_tasks_terminal(observed['tasks']))
        watched = subprocess.run([sys.executable, '-B', monitor.__file__, 'watch',
            '--config', str(path), '--auth', str(self.base / 'absent-auth'),
            '--until-terminal'], capture_output=True, text=True, timeout=5)
        self.assertEqual(watched.returncode, 0, watched.stderr)
        self.assertEqual(json.loads((self.base / 'monitor-state/watch.json').read_text())['reason'],
                         'ALL_TASKS_TERMINAL')
        reasons = registry.eligible(observed, time.time())
        self.assertEqual(reasons, {'stopped': 'observed terminal state STOPPED'})
        archived = registry.archive(path, reasons, apply=True)
        self.assertTrue(archived['applied'])
        self.assertEqual(json.loads(path.read_text())['tasks'], [])
        self.assertEqual(json.loads(Path(archived['archive']).read_text())['tasks'][0]['id'], 'stopped')

    def test_controller_sigkill_guard_recovers_no_credit(self):
        self.init(PREAMBLE+'time.sleep(5)');p=self.launch();token=self.read()['turn']['token']
        p.kill();p.wait(timeout=3)
        self.until(lambda: self.read()['status']=='PAUSED')
        self.assertEqual(self.read()['budget']['active_seconds'],0)
        self.assertFalse(scope_members(token));self.assertTrue((self.run/'exit.json').exists())

    def test_frozen_controller_is_killed_by_guard(self):
        self.init(PREAMBLE+'time.sleep(5)',heartbeat={'controller_stale_seconds':.25,'stale_after_seconds':3})
        p=self.launch();token=self.read()['turn']['token'];os.kill(p.pid,signal.SIGSTOP)
        p.wait(timeout=6);self.until(lambda: not self.read().get('controller_pid'))
        self.assertFalse(scope_members(token));self.assertEqual(self.read()['stop_reason'],'controller_stale')
        self.assertEqual(self.read()['turn']['number'],1)

    def test_worker_detects_parent_loss_without_guard(self):
        self.init(PREAMBLE+'time.sleep(5)');p=self.launch(guard=False);token=self.read()['turn']['token']
        p.kill();p.wait(timeout=3);self.until(lambda: not scope_members(token))
        self.assertEqual(self.invoke('recover','--run-id','test').returncode,0)
        self.assertEqual(self.read()['budget']['active_seconds'],0)

    def test_recover_with_audited_cleanup_preserves_missing_worker_exit_and_credit(self):
        cfg = self.init(PREAMBLE+'time.sleep(5)', cleanup={
            'command':[sys.executable, '-c', 'raise SystemExit(9)'], 'timeout_seconds':1})
        p = self.launch(guard=False)
        token = self.read()['turn']['token']
        p.kill(); p.wait(timeout=3)
        self.until(lambda: not scope_members(token))
        turn = Path(self.read()['turn']['dir'])
        (turn/'worker-exit.json').unlink(missing_ok=True)
        before = self.read()
        self.assertNotEqual(self.invoke('recover', '--run-id', 'test').returncode, 0)
        cfg['cleanup']['command'] = [sys.executable, '-c', 'pass']
        target = self.base/'recovery-config.json'
        target.write_text(json.dumps(cfg))
        result = self.invoke('recover', '--run-id', 'test', '--cleanup-config', target,
                             '--reason', 'replace obsolete failed cleanup hook')
        self.assertEqual(result.returncode, 0, result.stderr)
        after = self.read()
        self.assertEqual(after['config_sha256'], before['config_sha256'])
        self.assertEqual(after['budget']['active_seconds'], before['budget']['active_seconds'])
        self.assertEqual(after['budget']['hard_deadline_at'], before['budget']['hard_deadline_at'])
        self.assertFalse((turn/'worker-exit.json').exists())
        self.assertEqual(after['turn']['reason'], 'controller_lost')
        self.assertEqual(after['turn']['elapsed_seconds'], 0)
        receipts = list((self.run/'cleanup-recovery').glob('*/receipt.json'))
        self.assertEqual(len(receipts), 1)
        self.assertTrue(json.loads(receipts[0].read_text())['cleanup_ok'])

    def test_recovery_cleanup_rejects_scientific_or_budget_changes_and_live_controller(self):
        cfg = self.init(PREAMBLE+'time.sleep(5)')
        target = self.base/'recovery-config.json'
        changed = json.loads(json.dumps(cfg))
        changed['budget']['window_seconds'] += 1
        target.write_text(json.dumps(changed))
        result = self.invoke('recover', '--run-id', 'test', '--cleanup-config', target, '--reason', 'test')
        self.assertNotEqual(result.returncode, 0)
        self.assertIn('only cleanup and env', result.stderr)
        target.write_text(json.dumps(cfg))
        p = self.launch(guard=False)
        result = self.invoke('recover', '--run-id', 'test', '--cleanup-config', target, '--reason', 'test')
        self.assertNotEqual(result.returncode, 0)
        self.assertIn('Resource temporarily unavailable', result.stderr)
        self.assertIsNone(p.poll())

    def test_guard_death_stops_current_worker(self):
        self.init(PREAMBLE+'time.sleep(5)');p=self.launch();s=self.read()
        signal_identity(s['guard_pid'],s['guard_start_ticks'],signal.SIGKILL)
        p.wait(timeout=6)
        self.assertEqual(self.read()['stop_reason'],'guard_lost');self.assertFalse(scope_members(s['turn']['token']))

    def test_children_setsid_and_ignored_term_are_reaped_without_harming_peer(self):
        code=PREAMBLE+"""import subprocess,sys
subprocess.Popen([sys.executable,'-c','import signal,time;signal.signal(signal.SIGTERM,signal.SIG_IGN);time.sleep(30)'],start_new_session=True)
time.sleep(.2);emit('turn.completed',credit=True)
"""
        self.init(code,budget={'window_seconds':.15})
        peer=subprocess.Popen([sys.executable,'-c','import time;time.sleep(10)']);self.children.append(peer)
        r=self.invoke('run','--run-id','test','--no-guard')
        self.assertEqual(r.returncode,0,r.stderr);self.assertIsNone(peer.poll())
        self.assertFalse(scope_members(self.read()['turn']['token']))

    def test_output_limit_and_bounded_oversized_line(self):
        self.init(PREAMBLE+"print('X'*2000000,flush=True);time.sleep(5)",output={'max_run_bytes':500000},heartbeat={'stale_after_seconds':3})
        self.assertEqual(self.invoke('run','--run-id','test','--no-guard').returncode,1)
        self.assertEqual(self.read()['stop_reason'],'storage_limit')

    def test_source_config_and_snapshot_tampering(self):
        self.init()
        original=(self.run/'config.json').read_text()
        (self.run/'config.json').write_text(original+' ')
        self.assertNotEqual(self.invoke('run','--run-id','test','--no-guard').returncode,0)
        (self.run/'config.json').write_text(original)
        summary=self.base/'s.md';summary.write_text('ok')
        self.invoke('context','reopen','--run-id','test','--generation','0','--summary-file',summary)
        snapshot=self.run/'context/snapshot-0001.json';value=json.loads(snapshot.read_text());value['summary']='bad';snapshot.write_text(json.dumps(value))
        self.assertEqual(self.invoke('run','--run-id','test','--no-guard').returncode,1)
        self.assertEqual(self.read()['stop_reason'],'controller_error')

    def test_background_launch_ack_and_logs(self):
        self.init(PREAMBLE+'time.sleep(.15);emit("turn.completed",credit=True)',budget={'window_seconds':.1})
        r=self.invoke('start','--run-id','test','--background');self.assertEqual(r.returncode,0,r.stderr)
        launch=json.loads(r.stdout);self.assertEqual(launch['status'],'ACCEPTED')
        self.until(lambda: self.read()['controller_pid'] is None)
        r=self.invoke('doctor','--run-id','test');self.assertEqual(r.returncode,0,r.stderr)
        r=self.invoke('logs','--run-id','test','--stream','stdout');self.assertIn('turn.completed',r.stdout)

    def test_budget_clock_rollback_and_no_reset(self):
        cfg=longrun.validate_config(config(self.base,PREAMBLE))
        state=longrun.new_state(cfg,'test',self.base)
        with mock.patch('longrun.time.monotonic',return_value=100): longrun.begin_run(state,'2026-01-01T00:00:00+00:00')
        deadline=state['budget']['hard_deadline_at'];state['status']='PAUSED'
        with mock.patch('longrun.time.monotonic',return_value=110),mock.patch('longrun.time.time',return_value=1):
            longrun.begin_run(state);self.assertTrue(longrun.budget_view(state)['hard_reached'])
        self.assertEqual(state['budget']['hard_deadline_at'],deadline)

    def test_boot_rebase_preserves_credit_and_deadline(self):
        cfg = self.init(budget={'window_seconds': 2, 'hard_limit_seconds': 20})
        state = self.read()
        longrun.begin_run(state)
        old_boot = 'old-boot-for-rebase-test'
        state['budget'].update(boot_id=old_boot, active_seconds=.4, active_started_at=None,
                               active_monotonic=None)
        state.update(process_boot_id=old_boot, status='EXPIRED', stop_reason='controller_lost',
                     resume_required=True, controller_pid=None, guard_pid=99999999,
                     guard_start_ticks=1)
        longrun.save_state(self.run, state)
        deadline = state['budget']['hard_deadline_at']

        for number, stop in enumerate(('controller_lost', 'authorized_deadline_extension', 'hard_limit'), 1):
            with self.subTest(stop_reason=stop):
                state['stop_reason'] = stop
                longrun.save_state(self.run, state)
                result = controller.rebase_boot_run(self.run, reason='host reboot recovery test')
                rebased = self.read()
                self.assertEqual(rebased['status'], 'PAUSED')
                self.assertEqual(rebased['stop_reason'], 'boot_rebased')
                self.assertTrue(rebased['resume_required'])
                self.assertEqual(rebased['budget']['active_seconds'], .4)
                self.assertEqual(rebased['budget']['hard_deadline_at'], deadline)
                self.assertNotEqual(rebased['budget']['boot_id'], old_boot)
                self.assertEqual(result['recovery']['previous_boot_id'], old_boot)
                self.assertEqual(result['recovery']['previous_stop_reason'], stop)
                recovery = self.run/'recovery'/f'{number:06d}'
                self.assertTrue((recovery/'previous-state.json').exists())
                self.assertTrue((recovery/'receipt.json').exists())

    def test_boot_rebase_rejects_real_deadline_expiry(self):
        from datetime import datetime, timezone
        self.init(budget={'window_seconds': 2, 'hard_limit_seconds': 20})
        state = self.read()
        started = datetime.fromtimestamp(time.time() - 25, timezone.utc).isoformat()
        longrun.begin_run(state, started)
        state['budget'].update(boot_id='old-boot', active_started_at=None, active_monotonic=None)
        state.update(status='EXPIRED', stop_reason='hard_limit', controller_pid=None, guard_pid=None)
        longrun.save_state(self.run, state)
        with self.assertRaisesRegex(longrun.ControllerError, 'original hard deadline has passed'):
            controller.rebase_boot_run(self.run, reason='cannot restart expired research')
        self.assertEqual(self.read()['status'], 'EXPIRED')

    def test_boot_rebase_rejects_unchanged_boot(self):
        self.init(budget={'window_seconds': 2, 'hard_limit_seconds': 20})
        state = self.read()
        longrun.begin_run(state)
        state.update(status='EXPIRED', stop_reason='hard_limit', controller_pid=None, guard_pid=None)
        longrun.save_state(self.run, state)
        with self.assertRaisesRegex(longrun.ControllerError, 'changed host boot identity'):
            controller.rebase_boot_run(self.run, reason='unchanged host cannot rebase')

    def test_pid_reuse_identity_does_not_signal(self):
        peer=subprocess.Popen([sys.executable,'-c','import time;time.sleep(5)']);self.children.append(peer)
        ticks=process_start_ticks(peer.pid)
        self.assertFalse(signal_identity(peer.pid,ticks+1,signal.SIGTERM))
        self.assertFalse(pid_matches(peer.pid,ticks,'wrong-boot'))
        self.assertIsNone(peer.poll())

    def test_strict_config_and_disk_root_rejection(self):
        for patch in ({'typo':1},{'command':'shell string'},{'heartbeat':{'required':'false'}},
                      {'budget':{'window_seconds':4,'hard_limit_seconds':50000}},
                      {'context':{'max_tokens':4096.2}}, {'transport':{'kind':'ssh'}}):
            value=config(self.base,PREAMBLE,**patch)
            with self.assertRaises(longrun.ControllerError): longrun.validate_config(value)
        value=config(self.base,PREAMBLE)
        del value['context']['max_tokens']
        self.assertEqual(longrun.validate_config(value)['context']['max_tokens'], 150000)
        with self.assertRaises(longrun.ControllerError): longrun.check_storage('/',self.base)

    def test_bundle_whitelist_permissions_tampering_and_self_contained(self):
        output=self.base/'bundle';bundle.build(output)
        self.assertTrue(bundle.verify(output)['valid'])
        self.assertTrue(os.access(output/'templates/launch_controller.sh',os.X_OK))
        r=subprocess.run([sys.executable,'-B',str(output/'controller.py'),'--help'],capture_output=True)
        self.assertEqual(r.returncode,0,r.stderr)
        (output/'extra').write_text('extra');self.assertFalse(bundle.verify(output)['valid']);(output/'extra').unlink()
        (output/'core/worker.py').write_text('tampered');self.assertFalse(bundle.verify(output)['valid'])
        manifest=json.loads((output/bundle.MANIFEST).read_text());manifest['files']={};(output/bundle.MANIFEST).write_text(json.dumps(manifest))
        self.assertFalse(bundle.verify(output)['valid'])

    def test_ssh_argv_quotes_paths_and_never_replays_unknown_mutation(self):
        cfg={'host':'example.invalid','user':'research','port':22,'python':'python3','controller_dir':'/data/release a',
             'state_dir':'/data/state','data_mount':'/data','connect_timeout_seconds':1,'command_timeout_seconds':2,
             'identity_file':None,'known_hosts_file':None}
        args=remote.ssh_command(cfg,['python3','/data/a b/ctl.py','--reason','$(touch /bad)'])
        import shlex
        self.assertEqual(shlex.split(args[-1]),['python3','/data/a b/ctl.py','--reason','$(touch /bad)'])
        with mock.patch('remote.subprocess.run',side_effect=subprocess.TimeoutExpired('ssh',2)) as call:
            with self.assertRaisesRegex(longrun.ControllerError,'UNKNOWN'):
                remote.call(cfg,['python3'],{'action':'start'})
            self.assertEqual(call.call_count,1)

    def test_cleanup_hook_is_scoped_idempotent_and_receipted(self):
        hook = [sys.executable, '-c', "import os,pathlib; p=pathlib.Path(os.environ['AUTORESEARCH_TURN_DIR'])/'released'; p.write_text('released')"]
        self.init(PREAMBLE+"time.sleep(.12);emit('turn.completed',credit=True)", budget={'window_seconds':.1},
                  cleanup={'command':hook,'timeout_seconds':.3})
        r=self.invoke('run','--run-id','test','--no-guard')
        self.assertEqual(r.returncode,0,r.stderr)
        td=Path(self.read()['turn']['dir'])
        self.assertEqual((td/'released').read_text(),'released')
        self.assertEqual(len(list(td.glob('cleanup-attempt-*.json'))),1)
        self.assertTrue(json.loads((td/'cleanup-exit.json').read_text())['ok'])

    def test_cleanup_hook_failure_blocks_resume(self):
        self.init(PREAMBLE+"time.sleep(.05);emit('turn.completed',credit=True)",
                  cleanup={'command':[sys.executable,'-c','raise SystemExit(9)'],'timeout_seconds':.2})
        self.assertEqual(self.invoke('run','--run-id','test','--no-guard').returncode,1)
        self.assertEqual(self.read()['stop_reason'],'cleanup_incomplete')
        self.assertNotEqual(self.invoke('run','--run-id','test','--resume','--no-guard').returncode,0)
        self.assertEqual(self.read()['turn']['number'],1)

    def test_cleanup_timeout_leaves_receipt_and_no_worker(self):
        self.init(PREAMBLE+"time.sleep(.05);emit('turn.completed',credit=True)",
                  cleanup={'command':[sys.executable,'-c','import time;time.sleep(30)'],'timeout_seconds':.1})
        self.assertEqual(self.invoke('run','--run-id','test','--no-guard').returncode,1)
        state=self.read();td=Path(state['turn']['dir'])
        self.assertEqual(json.loads((td/'cleanup-exit.json').read_text())['reason'],'timeout')
        self.assertEqual(state['stop_reason'],'cleanup_incomplete')
        self.assertFalse(scope_members(state['turn']['token']))

    def test_worker_sigkill_cleans_detached_child_before_retry(self):
        self.init(PREAMBLE+"time.sleep(10)",heartbeat={'stale_after_seconds':3,'controller_stale_seconds':5})
        p=self.launch();state=self.read();token=state['turn']['token']
        signal_identity(state['turn']['pid'],state['turn']['pid_start_ticks'],signal.SIGKILL)
        self.until(lambda:self.read()['turn']['number']==2 and self.read()['turn']['status']=='RUNNING')
        self.assertEqual(self.invoke('stop','--run-id','test','--reason','test_stop').returncode,0)
        p.wait(timeout=6)
        self.assertEqual(self.read()['stop_reason'],'operator_stop')
        self.assertEqual(self.read()['turn']['number'],2)
        self.assertEqual(json.loads((self.run/'turns/000001/exit.json').read_text())['reason'],'worker_exit_missing')
        self.assertEqual(json.loads((self.run/'turns/000002/exit.json').read_text())['reason'],'operator_stop')
        self.assertFalse(scope_members(token))

    def test_context_missing_summary_keeps_old_generation(self):
        self.init(PREAMBLE+"emit('context.usage',used_tokens=3500,conversation_id='chat-'+str(G));time.sleep(5)",
                  context={'auto_compact':False})
        self.assertEqual(self.invoke('run','--run-id','test','--no-guard').returncode,0)
        self.assertEqual(self.read()['status'],'WAITING_COMPACTION')
        self.assertEqual(self.read()['context']['generation'],0)
        self.assertFalse(list((self.run/'context').glob('snapshot-*.json')))

    def test_expired_paused_window_cannot_launch_more_work(self):
        self.init(PREAMBLE+"time.sleep(10)",budget={'window_seconds':.4,'hard_limit_seconds':.8},turn={'seconds':.8})
        p=self.launch();self.invoke('stop','--run-id','test','--reason','pause');p.wait(timeout=5)
        before=self.read();time.sleep(.85)
        result=self.invoke('run','--run-id','test','--resume','--no-guard')
        self.assertEqual(result.returncode,3,result.stderr)
        self.assertEqual(self.read()['turn']['number'],before['turn']['number'])
        self.assertEqual(self.read()['budget']['hard_deadline_at'],before['budget']['hard_deadline_at'])

    def test_context_snapshot_crash_does_not_overwrite_existing_evidence(self):
        self.init();path=self.run/'context/snapshot-0001.json';path.write_text('orphan evidence')
        summary=self.base/'summary.md';summary.write_text('new snapshot')
        r=self.invoke('context','reopen','--run-id','test','--generation','0','--summary-file',summary)
        self.assertEqual(r.returncode,0,r.stderr)
        self.assertEqual(path.read_text(),'orphan evidence');self.assertEqual(self.read()['context']['generation'],2)

    def test_recover_on_completed_run_preserves_exit_contract(self):
        self.init(budget={'window_seconds':.1})
        self.invoke('run','--run-id','test','--no-guard')
        before=(self.run/'exit.json').read_bytes()
        self.assertEqual(self.invoke('recover','--run-id','test').returncode,0)
        self.assertEqual((self.run/'exit.json').read_bytes(),before)

    def test_context_generation_reuses_conversation_unless_explicitly_replaced(self):
        self.init()
        self.assertEqual(self.invoke('context','report','--run-id','test','--generation','0',
                         '--conversation-id','old','--used-tokens','100').returncode,0)
        summary=self.base/'summary.md';summary.write_text('new conversation')
        self.assertEqual(self.invoke('context','reopen','--run-id','test','--generation','0',
                         '--summary-file',summary).returncode,0)
        state = self.read()
        self.assertEqual(state['context']['conversation_id'], 'old')
        self.assertEqual(self.invoke('context','report','--run-id','test','--generation','1',
                            '--conversation-id','old','--used-tokens','1').returncode,0)
        self.assertEqual(self.invoke('context','reopen','--run-id','test','--generation','1',
                         '--conversation-id','new','--summary-file',summary).returncode,0)
        self.assertNotEqual(self.invoke('context','report','--run-id','test','--generation','2',
                            '--conversation-id','old','--used-tokens','1').returncode,0)
        self.assertEqual(self.invoke('context','report','--run-id','test','--generation','2',
                            '--conversation-id','new','--used-tokens','1').returncode,0)

    def test_repeated_method_summary_emits_bounded_no_progress_signal(self):
        self.init()
        run = controller.LongRunController(self.run, with_guard=False)
        for turn in (1, 2, 3):
            run.state['turn']['number'] = turn
            run.record_method_summary({'method_summary': 'same method'})
        run.state['turn']['number'] = 4
        run.record_method_summary({'method_summary': 'changed method'})
        run.save()
        progress = self.read()['progress']
        self.assertGreaterEqual(progress['method_summary_observations'], 3)
        self.assertEqual(progress['consecutive_same_method_summary'], 1)
        self.assertFalse(progress['no_progress'])
        self.assertEqual(progress['no_progress_alerts'], 1)
        events = [json.loads(line) for line in (self.run / 'events.jsonl').read_text().splitlines()]
        alerts = [item for item in events if item['event'] == 'agent.no_progress']
        self.assertEqual(len(alerts), 1)
        self.assertEqual(alerts[0]['consecutive'], 3)

    def test_repeated_context_summary_emits_no_progress_signal(self):
        self.init()
        summary = self.base / 'summary.md'
        summary.write_text('Retained the original method')
        for generation in (0, 1, 2):
            self.assertEqual(self.invoke('context', 'reopen', '--run-id', 'test',
                             '--generation', str(generation), '--summary-file', summary).returncode, 0)
        state = self.read()
        self.assertEqual(state['progress']['context_summary_observations'], 3)
        self.assertEqual(state['progress']['consecutive_same_context_summary'], 3)
        self.assertTrue(state['progress']['no_progress'])
        events = [json.loads(line) for line in (self.run / 'events.jsonl').read_text().splitlines()]
        alerts = [item for item in events if item['event'] == 'agent.no_progress' and item.get('source') == 'context_summary']
        self.assertEqual(len(alerts), 1)

    def test_persisted_exit_records_stop_target_boundary(self):
        self.init(budget={'window_seconds': 36000, 'hard_limit_seconds': 36001})
        state = self.read()
        state.update(status='STOPPED', stop_reason='operator_stop', resume_required=True)
        state['budget']['active_seconds'] = 35999.0
        longrun.save_state(self.run, state)
        controller.persist_exit(self.run, state)
        result = json.loads((self.run / 'exit.json').read_text())
        self.assertFalse(result['target_reached'])
        self.assertFalse(result['target_reached_at_stop'])
        self.assertTrue(result['resume_required'])

    def test_background_resume_uses_new_guard_without_resetting_deadline(self):
        self.init(PREAMBLE+"time.sleep(.2);emit('turn.completed',credit=True)",budget={'window_seconds':.5})
        p=self.launch();self.invoke('stop','--run-id','test','--reason','resume-test');p.wait(timeout=6)
        deadline=self.read()['budget']['hard_deadline_at']
        r=self.invoke('start','--run-id','test','--resume','--background')
        self.assertEqual(r.returncode,0,r.stderr)
        self.until(lambda:self.read()['controller_pid'] is None)
        self.assertEqual(self.read()['status'],'COMPLETED')
        self.assertEqual(self.read()['budget']['hard_deadline_at'],deadline)
        self.assertEqual(len(list((self.run/'attempts').glob('*/exit.json'))),2)

    def test_remote_rpc_detaches_worker_from_short_control_connection(self):
        self.init(PREAMBLE+"time.sleep(.3);emit('turn.completed',credit=True)",budget={'window_seconds':.15})
        values=vars(controller.build_parser().parse_args(['--state-dir',str(self.state),
                    'start','--run-id','test','--background']))
        payload={'args':values,'data_mount':str(self.base)}
        # A local subprocess represents one SSH request. Only storage probing is
        # stubbed; the cloud RPC/controller/guard/worker paths are real.
        code="import sys;sys.path.insert(0,sys.argv[1]);from core import rpc;rpc.check_storage=lambda *a:None;raise SystemExit(rpc.main())"
        r=subprocess.run([sys.executable,'-B','-c',code,str(ROOT)],input=json.dumps(payload),
                         text=True,capture_output=True,timeout=5)
        self.assertEqual(r.returncode,0,r.stderr)
        self.assertEqual(json.loads(r.stdout)['status'],'ACCEPTED')
        self.until(lambda:self.read()['controller_pid'] is None)
        self.assertEqual(self.read()['status'],'COMPLETED')

    def test_rpc_read_request_uses_cloud_state(self):
        self.init()
        from core import rpc
        payload={'args':vars(controller.build_parser().parse_args(['--state-dir',str(self.state),'status','--run-id','test'])),
                 'data_mount':str(self.base)}
        import io
        out=io.StringIO()
        stdin=mock.Mock(buffer=io.BytesIO(json.dumps(payload).encode()))
        # Only the mount probe is simulated here; controller status reads real disk state.
        with mock.patch.object(rpc,'check_storage'),mock.patch.object(sys,'stdin',stdin),mock.patch.object(sys,'stdout',out):
            self.assertEqual(rpc.main(),0)
        self.assertEqual(json.loads(out.getvalue())['run_id'],'test')


if __name__=='__main__': unittest.main()
