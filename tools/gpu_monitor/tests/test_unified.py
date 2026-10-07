"""End-to-end contracts for the single collector and transient display."""
import copy
from contextlib import redirect_stdout
from io import StringIO
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
import monitor
from privacy import public_record
from presentation import cell_width, render_unified


class Terminal(StringIO):
    def isatty(self):
        return True


class UnifiedTests(unittest.TestCase):
    def test_empty_snapshot_terminal_golden(self):
        output = Terminal()
        with redirect_stdout(output):
            render_unified({'collected_at': '2026-10-07T00:00:00+00:00'}, 'never', width=180)
        self.assertIn('10-07 08:00:00', output.getvalue())
        self.assertIn('■ 运行（含待结算）', output.getvalue())
        self.assertNotIn('计时中', output.getvalue())

    def fixture(self):
        buckets = [{'state': 'pending', 'data_gap': []}] * 48
        return {'collected_at': '2026-10-07T00:00:00+00:00', 'interval_seconds': 60,
            'summary': {'critical_alerts': 0, 'warning_alerts': 1, 'gpu_total': 1,
                        'gpu_occupied': 1, 'scheduler': {'running': 1, 'queued': 1,
                                                       'oldest_wait_seconds': 1800}},
            'hosts': {'a': {'endpoint_id': 'endpoint-1', 'aliases': ['a', 'b', 'c'],
                'state': 'OK', 'gpu_count': 1, 'scheduler_summary': {'queued': 1},
                'gpus': [{'index': 0, 'uuid': 'GPU-1', 'name': 'GPU',
                    'measured': {'memory_used_mib': 1000, 'memory_total_mib': 8000,
                                 'utilization_pct': 70, 'temperature_c': 50},
                    'reserved': {'memory_mib': 2000, 'compute_units': 50},
                    'running_tasks': [{'task_id': 'task', 'state': 'RUNNING',
                                       'memory_used_mib': 1000, 'source': 'receipt', 'confidence': 'high'}],
                    'unknown_processes': [], 'unknown_memory_mib': 0,
                    'external_queue_summary': {'count': 2, 'resources': {'memory_mib': 1000},
                        'oldest_wait_seconds': 1800, 'blocking_reasons': ['insufficient_ram_mib']}}]}},
            'tasks': [{'id': 'task', 'state': 'RUNNING', 'current_turn': 3,
                'streams': [{'id': 's1', 'timeline_12h': buckets}, {'id': 's2', 'timeline_12h': buckets}],
                'alerts': ['SUMMARY_STAGNANT'], 'timeline_12h': buckets,
                'timing': {'live_elapsed_seconds': 3600, 'pending_seconds': 1800,
                           'credited_effective_seconds': 7200, 'calculation_state': 'pending_settlement'},
                'budget_remaining_seconds': 1000,
                'configured_job_ids': ['expired-job'],
                'active_job': {'job_id': 'job-current', 'state': 'QUEUED', 'reason': 'insufficient_ram_mib'}}],
            'alerts': [{'id': 'SUMMARY_STAGNANT', 'task_id': 'task', 'severity': 'warning',
                'state': 'resolved', 'first_seen': '2026-10-06T22:00:00+00:00',
                'observed_count': 22, 'source': 'research_handoff', 'evidence': {}, 'data_gap': False}]}

    def test_all_surfaces_and_tty_overlay_render_once(self):
        output = Terminal()
        overlay = {'task': {'state': 'VERIFIED', 'best': 0.23456789, 'latest': 0.34567891,
                           'B': 0.12345678, 'R': 0.87654321, 'direction': 'min', 'trend': '-', 'unchanged': False}}
        with redirect_stdout(output):
            render_unified(self.fixture(), 'never', overlay, width=120)
        text = output.getvalue()
        for value in ('scheduler', 'GPU-1', '日志流 s1', '日志流 s2', '12h', '本轮待确认估计', '累计已确认',
                      'job-current', '历史登记', '已恢复', 'SUMMARY_STAGNANT',
                      'insufficient_ram_mib', '外部队列 2', '最佳', 'B=', 'R=', '退步', '■ 运行'):
            self.assertIn(value, text)
        self.assertEqual(text.count('■' * 48), 1)
        self.assertNotIn('\033[', text)

    def test_narrow_width_and_degradation_keep_fields(self):
        data = self.fixture()
        data['hosts']['unavailable'] = {'state': 'UNREACHABLE', 'error': 'permission denied', 'gpus': []}
        for width in (32, 60, 120):
            output = Terminal()
            with redirect_stdout(output):
                render_unified(data, 'never', width=width)
            text = output.getvalue()
            self.assertTrue(all(cell_width(line) <= width for line in text.splitlines()))
            compact = ''.join(text.splitlines())
            for token in ('日志流 s1', '日志流 s2', '累计已确认', 'job-current', 'permission denied', '已恢复'):
                self.assertIn(token, compact)

    def test_non_tty_ignores_overlay_even_if_passed_by_caller(self):
        output = StringIO()
        with redirect_stdout(output):
            render_unified(self.fixture(), 'never', {'task': {'state': 'VERIFIED', 'best': 0.246813579,
                'latest': 0.246813579, 'direction': 'min', 'trend': '='}})
        self.assertNotIn('0.246813579', output.getvalue())

    def test_three_aliases_share_one_probe_and_unique_gpu_and_process(self):
        host = {'transport': 'ssh', 'hostname': 'host', 'user': 'user', 'port': 22, 'password': 'secret'}
        tasks = [{'id': name, 'host': name, 'root': '/example', 'gpu_container_ids': ['a' * 64]} for name in ('a', 'b', 'c')]
        tasks[1]['gpu_container_ids'] = []
        tasks[2]['gpu_container_ids'] = []
        cfg = {'hosts': {name: dict(host) for name in ('a', 'b', 'c')}, 'tasks': tasks, 'timeout_seconds': 1}
        app = {'gpu_uuid': 'GPU-1', 'pid': '33', 'memory_used_mib': '100', 'container_ids': ['a' * 64]}
        raw = {'observed_at': 1000, 'boot_id': 'boot', 'gpu': {'rows': [{'uuid': 'GPU-1'}] * 2},
            'gpu_processes': {'rows': [app, copy.deepcopy(app)]},
            'tasks': {name: {'files': {}, 'processes': [], 'identity_errors': [],
                'disk_free_bytes': None, 'scheduler_view': {}} for name in ('a', 'b', 'c')}}
        with patch.object(monitor, 'probe_host', return_value=raw) as probe:
            data = monitor.snapshot(cfg)
        self.assertEqual(probe.call_count, 1)
        self.assertEqual(len(data['hosts']), 1)
        view = next(iter(data['hosts'].values()))
        self.assertEqual(view['aliases'], ['a', 'b', 'c'])
        self.assertEqual(view['probe_count'], 1)
        self.assertEqual(len(view['gpus']), 1)
        self.assertEqual(len(view['gpus'][0]['running_tasks']), 1)
        self.assertEqual(view['gpus'][0]['running_tasks'][0]['memory_used_mib'], 100)

    def test_cli_rejects_legacy_display_before_config_or_auth_access(self):
        for arguments in (['watch', '--view', 'agents'], ['watch', '--view', 'gpu'],
                          ['watch', '--view', 'tasks'], ['status']):
            result = subprocess.run([sys.executable, str(ROOT / 'monitor.py'), *arguments,
                                     '--config', '/missing.json'], capture_output=True, text=True)
            self.assertNotEqual(result.returncode, 0)
            self.assertIn('watch', result.stderr)
            self.assertNotIn('无法读取凭据', result.stderr)

    def test_public_record_omits_scores_raw_logs_passwords_and_private_paths(self):
        data = public_record({'config': {'path': '/trusted/tasks.json'},
            'tasks': [{'tail': ['scientific_score=123.456'], 'latest': {'metric': 123.456, 'step': 2},
                'error': 'denied /private/reference/model.bin password=secret',
                'password': 'secret', 'scientific_score': 123.456}], 'score': 123.456}, ['secret'])
        text = json.dumps(data)
        self.assertNotIn('123.456', text)
        self.assertNotIn('secret', text)
        self.assertNotIn('/private', text)
        self.assertEqual(data['config']['path'], '/trusted/tasks.json')
        self.assertEqual(data['tasks'][0]['latest']['step'], 2)


if __name__ == '__main__':
    unittest.main()
