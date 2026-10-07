"""Interaction and lifecycle regressions; local fixtures, no remote/GPU work."""
import copy
import fcntl
import io
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
import monitor
from presentation import LEGEND, freshness, is_agent, resource_lines, score_line, timeline
from sessions import CollectorSource, SnapshotSource, collector_running

try:
    from ui import DetailView, MonitorApp, SnapshotReady, TaskScreen
    from textual.widgets import Collapsible, DataTable, Static
except ImportError:
    MonitorApp = None


def fixture(count=20):
    tasks = [{'id': f'agent-{i:02d}', 'label': f'研究任务 {i:02d}', 'host': 'local',
              'state': 'RUNNING', 'processes': [], 'current_turn': i, 'alerts': [],
              'advice': [],
              'controller': {'type': 'research_handoff', 'run_id': f'run-{i}'},
              'timing': {'credited_effective_seconds': 3600, 'pending_seconds': 120,
                         'live_elapsed_seconds': 180, 'credit_policy': 'reported',
                         'remaining_wall_seconds': 7200, 'waiting_gpu': False},
              'timeline_12h': [{'state': 'pending', 'data_gap': []}] * 48} for i in range(count)]
    tasks.extend([{'id': 'general-training', 'state': 'RUNNING', 'alerts': [], 'host': 'local',
                   'scheduler': {'type': 'gpu_scheduler', 'job_ids': ['job-1']}, 'processes': []},
                  {'id': 'general-queued', 'state': 'QUEUED', 'alerts': [], 'host': 'local', 'processes': [], 'advice': []}])
    tasks[count]['advice'] = []
    return {'collected_at': monitor.utc(), 'interval_seconds': 60, 'tasks': tasks, 'alerts': [],
            'summary': {'gpu_total': 1, 'gpu_occupied': 1, 'collection_seconds': 8.972,
                        'scheduler': {'running': 2, 'queued': 3}},
            'hosts': {'local': {'state': 'OK', 'host_resources': {'ram_available_mib': 0,
                     'ram_total_mib': 32000, 'cpu_count': 16, 'load_1m': 2.497},
                     'gpus': [{'index': 0, 'name': 'NVIDIA GeForce RTX 5090', 'uuid': 'GPU-long-' + 'a'*60,
                               'measured': {'memory_used_mib': 16000, 'memory_total_mib': 32000,
                                            'utilization_pct': 14, 'temperature_c': 43},
                               'reserved': {'memory_mib': 30000, 'compute_units': 75,
                                            'ram_mib': 16000, 'cpu_cores': 4},
                               'blocking_reason': 'insufficient_memory_reservation-' + 'b'*100}]}}}


class FakeSource:
    mode = 'background'

    def __init__(self, data=None):
        self.data = data or fixture()
        self.stop_event = threading.Event()
        self.refresh_count = 0

    def run(self, on_update, on_state):
        on_update(self.data, {}, {'mode': self.mode, 'collector_alive': True})
        self.stop_event.wait(20)
        return {'mode': self.mode, 'state': 'STOPPED', 'reason': 'LOCAL_INTERFACE_EXIT'}

    def stop(self):
        self.stop_event.set()

    def refresh(self):
        self.refresh_count += 1


class DisplayProjectionTests(unittest.TestCase):
    def test_activity_legend_does_not_claim_credit_or_gpu_work(self):
        self.assertEqual(timeline({'timeline_12h': [{'state': 'pending'}, {'state': 'credited'}]})[-2:],
                         [('■', 'green'), ('■', 'green')])
        self.assertNotIn('计时中', LEGEND)
        self.assertTrue(is_agent(fixture(1)['tasks'][0]))
        self.assertFalse(is_agent(fixture(1)['tasks'][1]))
        self.assertFalse(is_agent({'controller': {'type': 'other', 'run_id': 'x'}}))
        self.assertEqual([t['id'] for t in monitor.aggregate_agents(fixture(1)['tasks'])], ['agent-00'])
        self.assertIn('RAM 可用 0/32000', '\n'.join(resource_lines(fixture(1))))
        self.assertIn('退步（最小化）', score_line({'state': 'VERIFIED', 'best': .1, 'latest': .2,
                                                 'direction': 'min', 'trend': '-'}))

    def test_freshness_and_missing_data_are_not_zero(self):
        now = time.time()
        data = {'collected_at': monitor.utc(now - 121), 'interval_seconds': 60}
        self.assertTrue(freshness(data, now)[1])
        self.assertEqual(freshness({}, now), (None, True))
        self.assertEqual(timeline({'timeline_12h': [{'state': 'pending', 'data_gap': ['denied']}]})[-1][0], '?')


class SourceTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.state = Path(self.temp.name)

    def test_background_reuse_never_probes_or_changes_collector(self):
        lock = (self.state / 'watch.lock').open('w')
        self.addCleanup(lock.close)
        fcntl.flock(lock, fcntl.LOCK_EX)
        metadata = {'state': 'RUNNING', 'token': 'owned-by-background', 'polls': 12}
        monitor.atomic_json(self.state / 'watch.json', metadata)
        monitor.atomic_json(self.state / 'latest.json', fixture(1))
        source = SnapshotSource(self.state, max_polls=1)
        updates = []
        before = (self.state / 'watch.json').read_bytes()
        with patch.object(monitor, 'snapshot', side_effect=AssertionError('remote probe')):
            result = source.run(lambda *args: updates.append(args), lambda info: None)
        self.assertEqual(result['reason'], 'POLL_LIMIT')
        self.assertEqual(updates[0][1], {})
        self.assertEqual((self.state / 'watch.json').read_bytes(), before)
        self.assertTrue(collector_running(self.state))

    def test_background_refresh_not_counted_as_new_poll_and_invalid_cache_preserved(self):
        monitor.atomic_json(self.state / 'watch.json', {'state': 'STOPPED'})
        monitor.atomic_json(self.state / 'latest.json', fixture(1))
        source = SnapshotSource(self.state, max_polls=2)
        updates, errors = [], []
        def update(data, overlay, info):
            updates.append(data)
            (self.state / 'latest.json').write_text('{broken')
            source.refresh()
        def state(info):
            if info.get('cache_error'):
                errors.append(info)
                source.stop()
        result = source.run(update, state)
        self.assertEqual(result['reason'], 'LOCAL_INTERFACE_EXIT')
        self.assertEqual(len(updates), 1)
        self.assertTrue(errors)

    def test_stale_terminal_snapshot_does_not_finish_until_terminal(self):
        data = fixture(1)
        for task in data['tasks']:
            task['state'] = 'COMPLETED'
        data['collected_at'] = monitor.utc(time.time() - 3600)
        monitor.atomic_json(self.state / 'latest.json', data)
        source = SnapshotSource(self.state, until_terminal=True)
        result = source.run(lambda *args: None, lambda info: source.stop())
        self.assertEqual(result['reason'], 'LOCAL_INTERFACE_EXIT')

    def test_until_terminal_respects_selection_and_missing_tasks(self):
        data = fixture(1)
        data['tasks'][0]['state'] = 'COMPLETED'
        monitor.atomic_json(self.state / 'latest.json', data)
        source = SnapshotSource(self.state, until_terminal=True, task_ids=['agent-00'])
        result = source.run(lambda *args: None, lambda info: None)
        self.assertEqual(result['reason'], 'ALL_TASKS_TERMINAL')
        source = SnapshotSource(self.state, until_terminal=True, task_ids=['missing'])
        result = source.run(lambda *args: None, lambda info: source.stop())
        self.assertEqual(result['reason'], 'LOCAL_INTERFACE_EXIT')

    def test_foreground_stop_finishes_metadata_and_slow_probe_refresh_coalesces(self):
        cfg = {'hosts': {'local': {'transport': 'local'}}, 'tasks': [], 'interval_seconds': 60,
               'timeout_seconds': 30}
        stop, refresh = threading.Event(), threading.Event()
        updates = []
        def probe(current, previous):
            refresh.set()  # repeated key presses while a probe is in flight
            refresh.set()
            return fixture(1)
        def update(data, overlay, info):
            updates.append(data)
            stop.set()
        with patch.object(monitor, 'snapshot', side_effect=probe) as probe_call, \
                patch('sys.stdout', io.StringIO()) as output:
            result = monitor.watch(cfg, self.state, 60, 1, None, False,
                                   on_update=update, stop_event=stop, refresh_event=refresh)
        self.assertEqual(probe_call.call_count, 1)
        self.assertEqual(output.getvalue(), '')
        self.assertEqual(result['reason'], 'LOCAL_INTERFACE_EXIT')
        self.assertFalse(collector_running(self.state))
        self.assertEqual(json.loads((self.state / 'watch.json').read_text())['state'], 'STOPPED')

    def test_collector_source_race_attaches_without_auth_or_remote(self):
        source = CollectorSource({'hosts': {}, 'tasks': []}, self.state, auth_path='/missing',
                                 config_path='/missing', interval=60, max_polls=1)
        with (self.state / 'watch.lock').open('w') as lock:
            fcntl.flock(lock, fcntl.LOCK_EX)
            monitor.atomic_json(self.state / 'latest.json', fixture(1))
            with patch.object(monitor, 'load_auth', side_effect=AssertionError('read credential')), \
                 patch.object(monitor, 'watch', side_effect=AssertionError('second collector')):
                result = source.run(lambda *args: None, lambda info: None)
        self.assertEqual(result['mode'], 'background')

    def test_cli_json_does_not_import_textual(self):
        code = "import sys; from tools.gpu_monitor import monitor; assert 'textual' not in sys.modules; print('ok')"
        result = subprocess.run([sys.executable, '-B', '-c', code], cwd=ROOT.parents[1], capture_output=True, text=True)
        self.assertEqual(result.returncode, 0, result.stderr)


@unittest.skipIf(MonitorApp is None, 'Install requirements-tui.txt for interactive tests')
class TuiTests(unittest.IsolatedAsyncioTestCase):
    async def test_slow_collection_keeps_keyboard_and_resize_responsive(self):
        class SlowSource(FakeSource):
            mode = 'foreground'

            def run(self, on_update, on_state):
                on_state({'mode': self.mode, 'state': 'RUNNING', 'collecting': True})
                self.stop_event.wait(20)
                return {'mode': self.mode, 'state': 'STOPPED'}
        source = SlowSource()
        app = MonitorApp(source)
        async with app.run_test(size=(100, 30)) as pilot:
            try:
                await pilot.pause()
                await pilot.press('/', 'r')
                await pilot.pause()
                self.assertEqual(app.filter_text, 'r')
                await pilot.press('escape', 'r', '?')
                await pilot.pause()
                self.assertEqual(source.refresh_count, 1)
                await pilot.resize_terminal(40, 15)
                await pilot.pause()
                await pilot.press('escape', 'q')
                await pilot.pause()
                self.assertTrue(source.stop_event.is_set())
            finally:
                source.stop()

    async def test_responsive_borders_and_classification(self):
        source = FakeSource()
        app = MonitorApp(source, color='never')
        async with app.run_test(size=(180, 50)) as pilot:
            try:
                await pilot.pause()
                self.assertEqual(len(app._row_ids['tasks']), 20)
                self.assertEqual(len(app._row_ids['general-tasks']), 2)
                self.assertTrue(app.base('#general', Collapsible).collapsed)
                for width, height in ((40, 15), (80, 24), (100, 30), (120, 40), (180, 50)):
                    await pilot.resize_terminal(width, height)
                    await pilot.pause()
                    for selector in ('#resources', '#task-pane', '#status', '#alerts'):
                        region = app.base(selector).region
                        self.assertLessEqual(region.right, width, selector)
                        self.assertLessEqual(region.bottom, height, selector)
                    self.assertTrue(all(strip.cell_length <= width for strip in app.screen._compositor.render_strips()))
                    self.assertEqual(app.base('#detail').display, width >= 80)
            finally:
                source.stop()

    async def test_selection_scroll_filter_general_and_modal_refresh(self):
        source = FakeSource()
        app = MonitorApp(source)
        async with app.run_test(size=(100, 30)) as pilot:
            try:
                await pilot.pause()
                await pilot.press(*(['down'] * 10))
                await pilot.pause()
                selected = app.selected_id
                y = app.base('#tasks', DataTable).scroll_y
                data = copy.deepcopy(source.data)
                data['tasks'][10]['current_turn'] = 99
                app.post_message(SnapshotReady(data, {}, {'mode': 'background', 'collector_alive': True}))
                await pilot.pause()
                self.assertEqual(app.selected_id, selected)
                self.assertEqual(app.base('#tasks', DataTable).scroll_y, y)
                await pilot.press('enter')
                await pilot.pause()
                self.assertIsInstance(app.screen, TaskScreen)
                data['tasks'][10]['current_turn'] = 100
                app.post_message(SnapshotReady(data, {}, {'mode': 'background'}))
                await pilot.pause()
                detail = str(app.screen.query_one('.detail-text', Static).render())
                self.assertIn('100', detail)
                await pilot.press('escape', '/')
                await pilot.press(*list('general'))
                await pilot.pause()
                self.assertEqual(app._row_ids['tasks'], [])
                self.assertEqual(len(app._row_ids['general-tasks']), 2)
                self.assertFalse(app.base('#general', Collapsible).collapsed)
                await pilot.press('escape', 'r', '?')
                await pilot.pause()
                self.assertEqual(source.refresh_count, 1)
                await pilot.press('escape', 'q')
                await pilot.pause()
                self.assertTrue(source.stop_event.is_set())
            finally:
                source.stop()

    async def test_private_overlay_only_in_foreground_and_safe_text(self):
        source = FakeSource(fixture(1))
        source.mode = 'foreground'
        app = MonitorApp(source)
        async with app.run_test(size=(120, 40)) as pilot:
            try:
                await pilot.pause()
                overlay = {'agent-00': {'state': 'VERIFIED', 'best': .246813579, 'latest': .246813579,
                                         'direction': 'min', 'trend': '='}}
                data = copy.deepcopy(source.data)
                data['tasks'][0]['label'] = '[red]literal'
                data['scientific_score'] = 12345
                app.post_message(SnapshotReady(data, overlay, {'mode': 'foreground'}))
                overlay.clear()
                await pilot.pause()
                self.assertEqual(app.overlay['agent-00']['best'], .246813579)
                self.assertNotIn('scientific_score', app.data)
                self.assertEqual('[red]literal', app.base('#tasks', DataTable).get_cell('agent-00', '0').plain)
                app.post_message(SnapshotReady(data, app.overlay, {'mode': 'background'}))
                await pilot.pause()
                self.assertEqual(app.overlay, {})
                self.assertNotIn('0.246814', str(app.base('#detail').query_one('.detail-text', Static).render()))
            finally:
                source.stop()


if __name__ == '__main__':
    unittest.main()
