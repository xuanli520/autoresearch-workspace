"""Failure-oriented checks; no training, external network or installed GPU needed."""
import copy
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import time
import unittest
from contextlib import redirect_stdout
from io import StringIO
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import monitor
import probe


class MonitorTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.task = {'id': 'demo', 'host': 'local', 'root': str(self.root),
                     'status': {'path': 'status.json', 'key': 'state'}, 'processes': [],
                     'streams': [{'id': 'train', 'path': 'train.log', 'fields': {'step': 'step', 'metric': 'loss', 'elapsed_seconds': 'elapsed'},
                                  'total_steps': 100, 'stale_seconds': 60}]}
        self.raw = {'files': {}, 'processes': [], 'identity_errors': [], 'disk_free_bytes': 10 * 1024 ** 3}
        self.host = {'observed_at': 1000, 'boot_id': 'boot1', 'gpu': {'rows': []},
                     'gpu_processes': {'rows': []}, 'tasks': {'demo': self.raw}}

    def log(self, text, mtime=1000):
        self.raw['files']['train.log'] = {'text': text, 'size': len(text), 'inode': 3, 'mtime': mtime}

    def test_aggregate_agents_preserves_official_identities(self):
        tasks = [{'id': 'a', 'host': 'local', 'state': 'RUNNING', 'alerts': [],
                  'processes': [{'pid': 9}],
                  'controller': {'type': 'research_handoff', 'run_id': 'run-1'},
                  'scheduler': {'type': 'gpu_scheduler', 'job_ids': ['job-1']}}]
        agents = monitor.aggregate_agents(tasks)
        self.assertEqual(agents[0]['controller']['run_id'], 'run-1')
        self.assertEqual(agents[0]['scheduler']['job_ids'], ['job-1'])

    def test_marker_registration_is_rejected(self):
        path = self.root / 'config.json'
        config = {'version': 1, 'hosts': {'local': {'transport': 'local'}},
                  'tasks': [{**self.task, 'stop': {'mode': 'marker', 'path': 'STOP'}}]}
        path.write_text(json.dumps(config))
        with self.assertRaisesRegex(ValueError, 'custom stop contracts'):
            monitor.load_config(path)

    def test_evaluate_keeps_registry_identities_in_live_and_unreachable_views(self):
        self.task['controller'] = {'type': 'research_handoff', 'run_id': 'run-1'}
        self.task['scheduler'] = {'type': 'gpu_scheduler', 'job_ids': ['request-1']}
        self.process(); self.declared('RUNNING'); self.log('')
        for host in (self.host, {'error': 'connection timeout'}):
            with self.subTest(unreachable=bool(host.get('error'))):
                result = monitor.evaluate(self.task, host)
                agents = monitor.aggregate_agents([result])
                self.assertEqual(len(agents), 1)
                self.assertEqual(agents[0]['controller']['run_id'], 'run-1')
                self.assertEqual(agents[0]['scheduler']['job_ids'], ['request-1'])
                self.assertEqual(agents[0]['state'], result['state'])

    def process(self):
        self.raw['processes'] = [{'pid': 99, 'state': 'S', 'start_ticks': 123}]

    def declared(self, state):
        self.raw['files']['status.json'] = {'text': json.dumps({'state': state})}

    def test_stale_running_record_is_not_alive_or_success(self):
        self.declared('RUNNING')
        self.log('')
        result = monitor.evaluate(self.task, self.host)
        self.assertEqual(result['state'], 'EXITED_WITHOUT_RESULT')

    def test_candidate_uid_gpu_process_is_owned_by_exact_container_cgroup(self):
        self.process(); self.declared('RUNNING'); self.log('')
        self.task['gpu_container_ids'] = ['a' * 64]
        self.host['gpu_processes']['rows'] = [
            {'pid': '123', 'gpu_uuid': 'GPU-demo', 'container_ids': ['a' * 64]},
            {'pid': '124', 'gpu_uuid': 'GPU-demo', 'container_ids': ['b' * 64]},
        ]
        result = monitor.evaluate(self.task, self.host)
        self.assertEqual([r['belongs_to_task'] for r in result['gpu_processes']], [True, False])

    def test_cpu_task_does_not_report_unrelated_gpu_failure(self):
        self.process(); self.declared('RUNNING'); self.log('')
        self.task['uses_gpu'] = False
        self.host['gpu'] = {'error': 'no GPU available'}
        self.host['gpu_processes']['rows'] = [{'pid': '123', 'gpu_uuid': 'GPU-other'}]
        result = monitor.evaluate(self.task, self.host)
        self.assertNotIn('GPU_UNAVAILABLE', result['alerts'])
        self.assertEqual(result['gpu_processes'], [])

    def test_connection_loss_does_not_reuse_live_state(self):
        old = {'state': 'RUNNING', 'last_success_at': 'old'}
        result = monitor.evaluate(self.task, {'error': 'timeout'}, old)
        self.assertEqual(result['state'], 'UNREACHABLE')
        self.assertEqual(result['last_success_at'], 'old')
        self.assertIsNone(result['observed_at'])
        self.assertFalse(result['processes'])

    def test_completed_with_residual_process_is_conflict(self):
        self.process(); self.declared('DONE'); self.log('')
        result = monitor.evaluate(self.task, self.host)
        self.assertEqual(result['state'], 'COMPLETED')
        self.assertIn('STATUS_CONFLICT', result['alerts'])

    def test_exit_code_and_missing_exit_are_distinct(self):
        self.task['exit'] = {'path': 'worker.json', 'key': 'exit_code'}
        self.raw['files']['worker.json'] = {'text': '{"exit_code": 124}'}
        self.log('')
        self.assertEqual(monitor.evaluate(self.task, self.host)['state'], 'FAILED')
        self.raw['files']['worker.json'] = {'text': '{"exit_code": 0}'}
        self.assertEqual(monitor.evaluate(self.task, self.host)['state'], 'COMPLETED')

    def test_success_label_cannot_hide_nonzero_exit(self):
        self.declared('DONE'); self.log('')
        self.task['exit'] = {'path': 'worker.json', 'key': 'exit_code'}
        self.raw['files']['worker.json'] = {'text': '{"exit_code": 1}'}
        result = monitor.evaluate(self.task, self.host)
        self.assertEqual(result['state'], 'FAILED')
        self.assertIn('STATUS_CONFLICT', result['alerts'])

    def test_partial_json_nan_and_monotonic_eta(self):
        self.process()
        self.log('{"step": 10, "loss": 2, "elapsed": 5}\n{"step": 20, "loss": NaN, "elapsed": 10}\n{"step":')
        result = monitor.evaluate(self.task, self.host)
        self.assertIn('NONFINITE', result['alerts'])
        self.assertEqual(result['streams'][0]['latest']['step'], 20)
        self.assertEqual(result['streams'][0]['eta_seconds'], 40)
        json.loads(monitor.dump(result), parse_constant=lambda x: self.fail('nonstandard JSON'))

    def test_stall_detects_live_log_without_step_advancement(self):
        self.process(); self.log('{"step": 10}\n')
        old = monitor.evaluate(self.task, self.host)
        self.host['observed_at'] = 1100
        self.raw['files']['train.log']['mtime'] = 1100
        result = monitor.evaluate(self.task, self.host, old)
        self.assertIn('STALLED_PROGRESS', result['alerts'])
        self.assertNotIn('STALE_LOG', result['alerts'])

    def test_log_rotation_resets_stall_window(self):
        self.process(); self.log('{"step": 10}\n')
        old = monitor.evaluate(self.task, self.host)
        self.host['observed_at'] = 1100
        self.raw['files']['train.log'].update(inode=4, mtime=1100)
        self.assertNotIn('STALLED_PROGRESS', monitor.evaluate(self.task, self.host, old)['alerts'])

    def test_missing_log_remains_missing_across_polls_then_appears(self):
        self.process()
        self.raw['files']['train.log'] = {'missing': True}
        old = monitor.evaluate(self.task, self.host)
        self.host['observed_at'] = 1060
        result = monitor.evaluate(self.task, self.host, old)
        self.assertTrue(result['streams'][0]['missing'])
        self.log('{"step": 1}\n', mtime=1060)
        result = monitor.evaluate(self.task, self.host, result)
        self.assertEqual(result['streams'][0]['latest']['step'], 1)

    def test_budget_is_absolute_and_completed_does_not_expire(self):
        self.process(); self.log(''); self.task['deadline_at'] = monitor.utc(950)
        self.assertIn('DEADLINE_EXCEEDED', monitor.evaluate(self.task, self.host)['alerts'])
        self.raw['processes'] = []; self.declared('DONE')
        self.assertNotIn('DEADLINE_EXCEEDED', monitor.evaluate(self.task, self.host)['alerts'])

    def test_effective_budget_ignores_expired_absolute_deadline(self):
        self.process(); self.log(''); self.task['deadline_at']=monitor.utc(950)
        self.task['status']['key']='agents.sol.status'
        self.task['launch']={'path':'watchdog.json'}
        self.task['deadline_file']={'path':'watchdog.json','key':'deadline_at'}
        self.raw['files']['watchdog.json']={'text':json.dumps({'budget_mode':'effective','deadline_at':None,'wall_limit_seconds':None})}
        state={'budget_mode':'effective','effective_target_seconds':39600,'effective_limit_seconds':43200,
               'agents':{'sol':{'status':'RESEARCHING','effective_seconds':35000}}}
        self.raw['files']['status.json']={'text':json.dumps(state)}
        result=monitor.evaluate(self.task,self.host)
        self.assertEqual(result['budget_remaining_seconds'],8200)
        self.assertEqual(result['effective_target_remaining_seconds'],4600)
        self.assertIsNone(result['deadline_at'])
        self.assertNotIn('DEADLINE_EXCEEDED',result['alerts'])
        self.assertNotIn('ETA_OVER_BUDGET',result['alerts'])
        self.assertNotIn('OBSERVATION_ERROR',result['alerts'])
        self.raw['processes']=[];state['agents']['sol']['status']='STOPPED'
        self.raw['files']['status.json']={'text':json.dumps(state)}
        self.assertIn('EFFECTIVE_TARGET_NOT_REACHED',monitor.evaluate(self.task,self.host)['alerts'])

    def test_invalid_effective_counter_is_reported_not_replaced_by_wall_time(self):
        self.process();self.log('');self.task['status']['key']='agents.seed.status'
        self.raw['files']['status.json']={'text':json.dumps({'budget_mode':'effective','effective_target_seconds':39600,
            'effective_limit_seconds':43200,'agents':{'seed':{'status':'RUNNING','effective_seconds':None}}})}
        result=monitor.evaluate(self.task,self.host)
        self.assertIn('OBSERVATION_ERROR',result['alerts'])
        self.assertIsNone(result['budget_remaining_seconds'])

    def test_official_active_budget_keeps_24h_deadline_and_confirmed_credit(self):
        self.process(); self.log('')
        self.task['controller'] = {'type': 'research_handoff', 'run_id': 'run-1'}
        state = {'state': 'RUNNING', 'controller': 'autoresearch-longrun', 'run_id': 'run-1',
                 'budget': {'mode': 'active', 'active_seconds': 7200, 'window_seconds': 36000,
                            'hard_limit_seconds': 86400, 'hard_deadline_at': monitor.utc(5000)},
                 'turn': {'number': 3}, 'heartbeat': {'stale': False}}
        self.raw['files']['status.json'] = {'text': json.dumps(state)}
        result = monitor.evaluate(self.task, self.host)
        self.assertEqual(result['effective_seconds'], 7200)
        self.assertEqual(result['effective_target_remaining_seconds'], 28800)
        self.assertEqual(result['budget_remaining_seconds'], 4000)
        self.assertEqual(result['deadline_at'], monitor.utc(5000))
        self.assertNotIn('OBSERVATION_ERROR', result['alerts'])
        projected = monitor.aggregate_agents([result])[0]
        self.assertEqual(projected['effective_seconds'], 7200)
        self.assertEqual(projected['current_turn'], 3)
        state['budget']['hard_deadline_at'] = monitor.utc(950)
        self.raw['files']['status.json'] = {'text': json.dumps(state)}
        self.assertIn('DEADLINE_EXCEEDED', monitor.evaluate(self.task, self.host)['alerts'])

    def test_null_controller_is_not_official_and_does_not_crash(self):
        self.process(); self.log('')
        self.task['controller'] = None
        state = {'state': 'RUNNING', 'controller': 'autoresearch-longrun', 'run_id': 'run-1',
                 'budget': {'mode': 'active', 'active_seconds': 7200, 'window_seconds': 36000,
                            'hard_limit_seconds': 86400, 'hard_deadline_at': monitor.utc(5000)}}
        self.raw['files']['status.json'] = {'text': json.dumps(state)}
        result = monitor.evaluate(self.task, self.host)
        self.assertNotEqual(result['budget_mode'], 'active')
        self.assertNotIn('controller', result)
        self.assertEqual(monitor.aggregate_agents([result]), [])

    def test_official_missing_credit_stays_unknown_and_stopped_short_is_reported(self):
        self.log('')
        self.task['controller'] = {'type': 'research_handoff', 'run_id': 'run-1'}
        state = {'state': 'STOPPED', 'controller': 'autoresearch-longrun', 'run_id': 'run-1',
                 'budget': {'mode': 'active', 'active_seconds': None, 'window_seconds': 36000,
                            'hard_limit_seconds': 86400, 'hard_deadline_at': monitor.utc(5000)}}
        self.raw['files']['status.json'] = {'text': json.dumps(state)}
        result = monitor.evaluate(self.task, self.host)
        self.assertIsNone(result['effective_seconds'])
        self.assertIn('OBSERVATION_ERROR', result['alerts'])
        state['budget']['active_seconds'] = 100
        self.raw['files']['status.json'] = {'text': json.dumps(state)}
        result = monitor.evaluate(self.task, self.host)
        self.assertIn('EFFECTIVE_TARGET_NOT_REACHED', result['alerts'])

    def test_text_regex_and_pending_stage(self):
        spec = {'id': 'adapt', 'path': 'log', 'format': 'regex', 'pattern': r'adapt (?P<step>\d+) loss (?P<metric>\S+)', 'total_steps': 50}
        entry = {'text': 'adapt 10 loss 0.5\n', 'mtime': 900, 'size': 20, 'inode': 3}
        parsed = monitor.parse_stream(spec, entry, {}, 1000, True, 60)
        self.assertEqual(parsed['latest']['metric'], .5)
        self.assertIn('STALE_LOG', parsed['alerts'])
        entry['text'] = 'features 10 / 100\n'
        self.assertNotIn('STALE_LOG', monitor.parse_stream(spec, entry, {}, 1000, True, 60)['alerts'])

    def test_phase_transition_without_last_step_log_does_not_stall(self):
        spec = {'id': 'adapt', 'path': 'log', 'format': 'regex', 'pattern': r'adapt (?P<step>\d+)',
                'total_steps': 1250, 'complete_pattern': r'(?m)^features '}
        entry = {'text': 'adapt 1200\nfeatures 0 / 2000\n', 'mtime': 1000, 'size': 40, 'inode': 3}
        old = monitor.parse_stream(spec, entry, {}, 1000, True, 60)
        entry.update(text='adapt 1200\n', mtime=1200, size=100)
        result = monitor.parse_stream(spec, entry, old, 1200, True, 60)
        self.assertTrue(result['phase_complete'])
        self.assertEqual(result['latest']['step'], 1200)
        self.assertNotIn('STALLED_PROGRESS', result['alerts'])

    def test_file_tail_is_bounded_and_symlink_cannot_escape(self):
        (self.root / 'large').write_bytes(b'x' * 300 + b'\nlast\n')
        result = probe.read_file(self.root, 'large', 50, tail=True)
        self.assertTrue(result['truncated'])
        self.assertEqual(result['text'], 'last\n')
        self.assertIn('error', probe.read_file(self.root, 'large', 50))
        (self.root / 'escape').symlink_to('/etc/passwd')
        self.assertIn('error', probe.read_file(self.root, 'escape', 100))
        os.mkfifo(self.root / 'pipe')
        self.assertIn('error', probe.read_file(self.root, 'pipe', 100))

    def test_probe_timeout_is_data_not_exception(self):
        cfg = {'tail_bytes': 1024, 'metadata_bytes': 1024, 'timeout_seconds': 1}
        with patch('monitor.subprocess.run', side_effect=subprocess.TimeoutExpired('test', 1)):
            result = monitor.probe_host({'transport': 'local'}, [self.task], cfg)
        self.assertIn('TimeoutExpired', result['error'])

    def test_ssh_command_has_explicit_host_config_and_disables_mux(self):
        command = monitor.ssh_command({'hostname': 'gpu.example', 'user': 'ubuntu', 'port': 2222,
                                       'connect_timeout_seconds': 7, 'options': []}, 'python3 -')
        self.assertIn('ControlPath=none', command)
        self.assertIn('ControlMaster=no', command)
        self.assertIn('StrictHostKeyChecking=accept-new', command)
        self.assertIn(f'UserKnownHostsFile={monitor.KNOWN_HOSTS}', command)
        self.assertEqual(command[command.index('-p') + 1], '2222')
        self.assertNotIn('-i', command)
        self.assertEqual(command[-2:], ['ubuntu@gpu.example', 'python3 -'])

    def test_password_auth_disables_key_auth_and_not_batch_mode(self):
        command = monitor.ssh_command({'hostname': 'gpu.example', 'user': 'ubuntu', 'port': 22},
                                      'python3 -', password_auth=True)
        self.assertIn('BatchMode=no', command)
        self.assertIn('NumberOfPasswordPrompts=1', command)
        self.assertIn('PubkeyAuthentication=no', command)
        self.assertNotIn('test-secret', command)

    def test_load_auth_accepts_labelled_chinese_file(self):
        path = self.root / 'auth.txt'
        path.write_text('IP地址：\n1.2.3.4\n用户名：\nubuntu\n密 码：\np:w:rd:123\n登录端口：\n2222\n')
        auth = monitor.load_auth(path)
        self.assertEqual(auth, {'host': '1.2.3.4', 'user': 'ubuntu',
                                'password': 'p:w:rd:123', 'port': 2222})

    def test_load_auth_supports_same_line_and_default_port(self):
        path = self.root / 'auth.txt'
        path.write_text('ip: 5.6.7.8\nuser: alice\npassword: secret\n')
        self.assertEqual(monitor.load_auth(path),
                         {'host': '5.6.7.8', 'user': 'alice', 'password': 'secret', 'port': 22})

    def test_load_auth_reports_missing_fields_and_file(self):
        path = self.root / 'auth.txt'
        path.write_text('IP地址：1.2.3.4\n')
        with self.assertRaisesRegex(ValueError, '缺少字段'):
            monitor.load_auth(path)
        with self.assertRaisesRegex(ValueError, '无法读取凭据文件'):
            monitor.load_auth(self.root / 'absent.txt')

    def test_apply_auth_overrides_host_and_rejects_legacy_keys(self):
        host = {'transport': 'ssh', 'hostname': 'old'}
        merged = monitor.apply_auth(host, {'host': '1.2.3.4', 'user': 'ubuntu',
                                           'password': 'pw', 'port': 2222})
        self.assertEqual((merged['hostname'], merged['user'], merged['port'], merged['password']),
                         ('1.2.3.4', 'ubuntu', 2222, 'pw'))
        for stale in ('target', 'password_env', 'identity_file'):
            with self.assertRaisesRegex(ValueError, 'legacy SSH authentication'):
                monitor.apply_auth({**host, stale: 'unsupported'},
                                   {'host': '1.2.3.4', 'user': 'ubuntu', 'password': 'pw', 'port': 2222})

    def test_refresh_auth_follows_edits_and_tolerates_invalid_file(self):
        path = self.root / 'auth.txt'
        path.write_text('IP地址：1.1.1.1\n用户名：ubuntu\n密码：one\n')
        cfg = {'hosts': {'gpu': {'transport': 'ssh', 'hostname': 'stale'}}}
        monitor.refresh_auth(cfg, path)
        self.assertEqual(cfg['hosts']['gpu']['password'], 'one')
        path.write_text('IP地址：2.2.2.2\n用户名：ubuntu\n密码：two\n登录端口：2200\n')
        monitor.refresh_auth(cfg, path)
        self.assertEqual((cfg['hosts']['gpu']['hostname'], cfg['hosts']['gpu']['password'],
                          cfg['hosts']['gpu']['port']), ('2.2.2.2', 'two', 2200))
        path.write_text('密码：写入一半')
        monitor.refresh_auth(cfg, path)
        self.assertEqual(cfg['hosts']['gpu']['password'], 'two')

    def test_askpass_reads_secret_from_fifo_path(self):
        secrets_dir = tempfile.mkdtemp(prefix='askpass-test-')
        fifo_path = os.path.join(secrets_dir, 'password.fifo')
        os.mkfifo(fifo_path, 0o600)
        fifo_fd = os.open(fifo_path, os.O_RDWR)
        try:
            os.write(fifo_fd, b'test-secret\n')
            env = os.environ.copy()
            env['AUTORESEARCH_SSH_PASSWORD_FIFO'] = fifo_path
            env['AUTORESEARCH_SSH_PASSWORD_FIFO_IDENTITY'] = monitor.inspect_fifo(fifo_path)
            result = subprocess.run([str(Path(monitor.__file__).with_name('askpass.py'))],
                                    env=env, capture_output=True, check=True)
        finally:
            os.close(fifo_fd)
            shutil.rmtree(secrets_dir, ignore_errors=True)
        self.assertEqual(result.stdout, b'test-secret\n')

    def test_run_ssh_uses_askpass_path_and_keeps_secret_out_of_env(self):
        fake_ssh = r'''import os, subprocess, sys
if any('test-only-secret' in value for value in os.environ.values()):
    sys.exit(18)
if 'AUTORESEARCH_SSH_PASSWORD_FD' in os.environ:
    sys.exit(19)
answer = subprocess.run([os.environ['SSH_ASKPASS']], capture_output=True)
sys.stdout.buffer.write(answer.stdout)
sys.stderr.buffer.write(answer.stderr)
sys.exit(answer.returncode)
'''
        host = {'password': 'test-only-secret'}
        result = monitor.run_ssh(host, [sys.executable, '-c', fake_ssh], 'probe input', 5)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stdout, 'test-only-secret\n')

    def test_run_ssh_without_password_rejects_other_authentication(self):
        with patch('monitor.subprocess.run') as run:
            with self.assertRaisesRegex(ValueError, 'password is required from auth.txt'):
                monitor.run_ssh({}, ['ssh', 'gpu.example'], '', 1)
        run.assert_not_called()

    def test_ssh_auth_errors_explain_credentials_or_helper_without_secrets(self):
        cfg = {'tail_bytes': 1024, 'metadata_bytes': 1024, 'timeout_seconds': 1,
               'connection_attempts': 2, 'retry_delay_seconds': 0}
        host = {'transport': 'ssh', 'hostname': 'gpu.example', 'user': 'ubuntu',
                'password': 'test-only-secret'}
        cases = [
            ('ubuntu@gpu.example: Permission denied (publickey,password).', 'SSH 认证被服务端拒绝'),
            ('ssh_askpass: exec(/usr/bin/python3 /tmp/askpass.py): No such file or directory\n'
             'ubuntu@gpu.example: Permission denied (publickey,password).', 'SSH askpass helper 启动失败'),
        ]
        for stderr, expected_hint in cases:
            with self.subTest(stderr=stderr), \
                    patch('monitor.run_ssh', return_value=
                          subprocess.CompletedProcess('ssh', 255, '', stderr)):
                result = monitor.probe_host(host, [self.task], cfg)
            self.assertIn(expected_hint, result['error'])
            if expected_hint == 'SSH 认证被服务端拒绝':
                self.assertIn('auth.txt', result['error'])
            self.assertNotIn('test-only-secret', result['error'])

    def test_ssh_timeout_retries_then_succeeds(self):
        cfg = {'tail_bytes': 1024, 'metadata_bytes': 1024, 'timeout_seconds': 1,
               'connection_attempts': 2, 'retry_delay_seconds': 0}
        success = subprocess.CompletedProcess('ssh', 0, '{"tasks": {}, "observed_at": 1}', '')
        with patch('monitor.run_ssh', side_effect=[subprocess.TimeoutExpired('ssh', 1), success]) as run:
            result = monitor.probe_host({'transport': 'ssh', 'hostname': 'gpu.example', 'user': 'ubuntu',
                                         'password': 'test-only-secret'},
                                        [self.task], cfg)
        self.assertEqual(result['observed_at'], 1)
        self.assertEqual(run.call_count, 2)

    def test_local_probe_uses_literal_paths_not_shell(self):
        weird = self.root / "with space ' $(touch SHOULD_NOT_EXIST)"
        weird.mkdir()
        (weird / 'status.json').write_text('{"state":"DONE"}')
        self.task['root'] = str(weird)
        result = monitor.probe_host({'transport': 'local'}, [self.task], {'tail_bytes': 1024, 'metadata_bytes': 1024, 'timeout_seconds': 25})
        self.assertEqual(monitor.evaluate(self.task, result)['state'], 'COMPLETED')
        self.assertFalse((self.root / 'SHOULD_NOT_EXIST').exists())

    def sleeper(self):
        p = subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(120)', 'gpu-monitor-test-owned'],
                             cwd=self.root, start_new_session=True)
        def cleanup():
            if p.poll() is None:
                p.terminate()
            p.wait(timeout=3)
        self.addCleanup(cleanup)
        # Avoid racing the fork/exec startup.
        for _ in range(100):
            rows, _ = probe.process_table()
            info = next((row for row in rows if row['pid'] == p.pid), None)
            if info and 'gpu-monitor-test-owned' in info['_command']:
                return p
            time.sleep(.01)
        self.fail('test sleeper failed to initialize')

    def test_pid_reuse_signature_mismatch_is_not_live(self):
        child = self.sleeper()
        self.task['processes'] = [{'pid': child.pid, 'contains': ['definitely-wrong-command'], 'cwd': '.'}]
        with patch('probe.gpu_query', return_value={'rows': []}):
            result = probe.collect({'tasks': [self.task]})
        raw = result['tasks']['demo']
        self.assertFalse(raw['processes'])
        self.assertEqual(raw['identity_errors'][0]['pid'], child.pid)

    def test_custom_stop_contract_is_rejected(self):
        self.task['stop'] = {'mode': 'process_groups', 'targets': []}
        with self.assertRaisesRegex(ValueError, 'custom stop contracts'):
            monitor.stop_task({}, self.task, 'test', True, self.root)

    def test_stop_requires_official_controller(self):
        cfg = {'hosts': {'local': {'transport': 'local'}}, 'timeout_seconds': 1}
        with self.assertRaisesRegex(ValueError, 'official controller'):
            monitor.stop_task(cfg, self.task, 'test', False, self.root)

    def test_background_lock_handoff_and_monitor_only_stop(self):
        cfg = {'version': 1, 'hosts': {'local': {'transport': 'local'}}, 'tasks': [self.task],
               'interval_seconds': 1, 'state_dir': 'state'}
        path = self.root / 'config.json'; path.write_text(json.dumps(cfg))
        auth = self.root / 'auth.txt'
        auth.write_text('IP地址：127.0.0.1\n用户名：tester\n密码：secret\n')
        base = [sys.executable, str(Path(monitor.__file__))]
        def cli(action):
            return subprocess.run(base + [action, '--config', str(path), '--auth', str(auth)],
                                  text=True, capture_output=True, timeout=10)
        def stop_cleanup():
            cli('stop-monitor')
        self.addCleanup(stop_cleanup)
        self.assertEqual(cli('maintain').returncode, 0)
        state = self.root / 'state'
        original = json.loads((state / 'watch.json').read_text())
        self.assertEqual(cli('maintain').returncode, 0)
        self.assertEqual(json.loads((state / 'watch.json').read_text())['pid'], original['pid'])
        self.assertEqual(cli('stop-monitor').returncode, 0)
        for _ in range(100):
            if not monitor.monitor_alive(state):
                break
            time.sleep(.05)
        self.assertFalse(monitor.monitor_alive(state))
        stopped = json.loads((state / 'watch.json').read_text())
        self.assertEqual(stopped['reason'], 'LOCAL_MONITOR_STOP_REQUESTED')
        self.assertTrue((state / 'latest.json').exists())
        self.assertTrue(list(state.glob('observations-*.jsonl')))

    def test_config_rejects_duplicate_ids_and_escaping_paths(self):
        cfg = {'version': 1, 'hosts': {'local': {'transport': 'local'}}, 'tasks': [self.task]}
        path = self.root / 'config.json'
        cfg['tasks'].append(copy.deepcopy(self.task)); path.write_text(json.dumps(cfg))
        with self.assertRaises(ValueError): monitor.load_config(path)
        cfg['tasks'].pop(); self.task['status']['path'] = '../escape'; path.write_text(json.dumps(cfg))
        with self.assertRaises(ValueError): monitor.load_config(path)

    def test_human_render_is_spaced_wrapped_and_color_controllable(self):
        self.process(); self.log('{"step": 20, "loss": 0.123456, "elapsed": 10, "critic_loss": 1.2}\n')
        data = monitor.evaluate(self.task, self.host)
        output = StringIO()
        with redirect_stdout(output):
            monitor.render({'collected_at': 'now', 'tasks': [data]}, color='never')
        text = output.getvalue()
        self.assertIn('GPU / scheduler', text)
        self.assertIn('Agent / 12h', text)
        self.assertIn('live', text)
        self.assertIn('credited', text)
        self.assertNotIn('\033[', text)
        self.assertGreaterEqual(text.count('\n'), 8)

        output = StringIO()
        with redirect_stdout(output):
            monitor.render({'collected_at': 'now', 'tasks': [data]}, color='always')
        self.assertIn('\033[', output.getvalue())

    def test_default_render_is_gpu_first_and_groups_tasks_by_gpu(self):
        output = StringIO()
        gpu_data = {'collected_at': 'now', 'hosts': {'cloud': {
            'state': 'OK', 'gpu_count': 1, 'gpus': [{
                'index': '0', 'name': 'RTX 5090', 'memory_used_mib': '8000',
                'memory_total_mib': '32607', 'utilization_pct': '92', 'temperature_c': '70',
                'running_tasks': [{'task_id': 'run-a', 'label': '训练 A', 'state': 'RUNNING',
                                   'pid': '123', 'memory_used_mib': '7800'}],
                'unknown_processes': [{'pid': '456', 'memory_used_mib': '200'}],
            }],
        }}, 'tasks': []}
        with redirect_stdout(output):
            monitor.render(gpu_data, color='never')
        text = output.getvalue()
        self.assertIn('GPU / scheduler', text)
        self.assertIn('GPU0 RTX 5090', text)
        self.assertIn('run-a', text)
        self.assertIn('未归属', text)
        self.assertNotIn('PID 456', text)
        self.assertNotIn('AutoResearch GPU 任务详情', text)

    def test_unreachable_host_renders_unknown_not_zero_gpu_or_task_counts(self):
        data = {'collected_at': 'now', 'hosts': {'shared-gpu': {
            'state': 'UNREACHABLE', 'error': 'probe exit 255: Permission denied',
            'gpu_count': None, 'gpus': [],
        }}, 'tasks': [{'host': 'shared-gpu', 'state': 'UNREACHABLE'}]}
        output = StringIO()
        with redirect_stdout(output):
            monitor.render(data, color='never')
        text = output.getvalue()
        self.assertIn('GPU 0/?', text)
        self.assertIn('UNREACHABLE', text)
        self.assertIn('GPU unavailable / unknown', text)
        self.assertNotIn('GPU 0 张', text)
        self.assertNotIn('运行任务 0 个', text)


if __name__ == '__main__':
    unittest.main()
