"""Legacy history migration uses recorded identities and real observation bounds."""
import copy
import json
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import diagnostics
import monitor
import probe
from presentation import timeline as display


class TimelineRecoveryTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.state = Path(self.temp.name)

    def snapshot(self, at, state='RUNNING', scope='same-run'):
        return {'collected_at': monitor.utc(at), 'interval_seconds': 60,
                'diagnostics': {'version': 1, 'tasks': {'task': {'scope_fingerprint': scope}}},
                'tasks': [{'id': 'task', 'state': state, 'observed_at': monitor.utc(at + 6), 'alerts': [],
                           'timing': {'credit_policy': 'reported'},
                           'timeline_12h': [{'start_at': monitor.utc(at - 894), 'end_at': monitor.utc(at + 6),
                                            'category': 'pending', 'data_gap': ['no_registered_history']}]}]}

    def history(self, observations):
        for row in observations:
            with (self.state / ('observations-' + row['collected_at'][:10] + '.jsonl')).open('a') as stream:
                stream.write(json.dumps(row) + '\n')
        monitor.atomic_json(self.state / 'latest.json', observations[-1])

    def test_legacy_pollution_recovers_without_changing_original_files(self):
        self.history([self.snapshot(10000 + 60 * i) for i in range(6)])
        before = {p: p.read_bytes() for p in self.state.iterdir()}
        current = monitor.load_latest(self.state)
        task = current['tasks'][0]
        self.assertIn(('■', 'green'), display(task))
        self.assertEqual(sum(b['known_seconds'] for b in task['timeline_12h']), 301)
        self.assertEqual(task['timeline_recovery']['observations'], 6)
        self.assertEqual(before, {p: p.read_bytes() for p in self.state.iterdir()})

    def test_unreachable_observation_gap_remains_exact(self):
        self.history([self.snapshot(10000), self.snapshot(10060, 'UNREACHABLE'), self.snapshot(10120)])
        task = monitor.load_latest(self.state)['tasks'][0]
        segments = [s for b in task['timeline_12h'] for s in b['segments']]
        self.assertEqual(sum(diagnostics.epoch(s['end_at']) - diagnostics.epoch(s['start_at']) for s in segments
                             if 'endpoint_unreachable' in s['data_gap']), 60)
        self.assertEqual(display(task)[-1][0], '■')

    def test_registration_change_and_large_sampling_hole_do_not_invent_history(self):
        self.history([self.snapshot(10000, scope='old-run'), self.snapshot(10100), self.snapshot(15000)])
        task = monitor.load_latest(self.state)['tasks'][0]
        self.assertEqual(sum(b['known_seconds'] for b in task['timeline_12h']), 2)
        self.assertEqual(task['timeline_recovery']['observations'], 2)

    def test_invalid_identity_is_unknown_despite_running_process(self):
        observations = [self.snapshot(10000), self.snapshot(10060)]
        observations[-1]['tasks'][0]['alerts'] = ['CONTROLLER_IDENTITY_MISMATCH']
        self.history(observations)
        self.assertEqual(display(monitor.load_latest(self.state)['tasks'][0])[-1][0], '?')

    def test_cross_midnight_and_torn_tail_are_supported(self):
        observations = [self.snapshot(86370), self.snapshot(86430), self.snapshot(86490)]
        self.history(observations)
        with (self.state / 'observations-1970-01-02.jsonl').open('a') as stream:
            stream.write('{torn')
        task = monitor.load_latest(self.state)['tasks'][0]
        self.assertEqual(sum(b['known_seconds'] for b in task['timeline_12h']), 121)

    def test_v2_snapshot_does_not_rescan_observations(self):
        latest = self.snapshot(10000)
        latest['diagnostics']['version'] = 2
        monitor.atomic_json(self.state / 'latest.json', latest)
        with patch.object(monitor, 'restore_timelines', side_effect=AssertionError('repeat migration')):
            self.assertEqual(monitor.load_latest(self.state), latest)

    def test_probe_records_completion_time_after_all_file_reads(self):
        with patch.object(probe.time, 'time', side_effect=[100, 110]), \
             patch.object(probe, 'process_table', return_value=([], [])), \
             patch.object(probe, 'gpu_query', return_value={'rows': []}), \
             patch.object(probe, '_host_resources', return_value={}):
            result = probe.collect({'tasks': []})
        self.assertEqual(result['observed_at'], 100)
        self.assertEqual(result['collected_at'], 110)


if __name__ == '__main__':
    unittest.main()
