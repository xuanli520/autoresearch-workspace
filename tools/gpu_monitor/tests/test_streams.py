"""Bounded incremental log parsing checks, using only local temporary files."""
import base64
import json
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import monitor
import probe


class StreamTests(unittest.TestCase):
    def setUp(self):
        monitor._STREAM_CACHES.clear()
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.path = self.root / 'train.jsonl'
        self.spec = {'id': 'train', 'path': 'train.jsonl',
                     'fields': {'step': 'step', 'metric': 'loss', 'elapsed_seconds': 'elapsed'},
                     'direction': 'min', 'total_steps': 100}

    def parse(self, old=None, now=100, limit=65536, spec=None):
        return monitor.parse_stream(spec or self.spec, probe.read_file(self.root, self.path.name, limit, tail=True),
                                    old or {}, now, True, 900)

    def test_unchanged_window_does_not_reparse_rows(self):
        self.path.write_text('{"step":1,"loss":2}\n{"step":2,"loss":1}\n')
        old = self.parse()
        with patch.object(monitor, '_parse_stream_line', wraps=monitor._parse_stream_line) as parse_line:
            result = self.parse(old, now=110)
        parse_line.assert_not_called()
        self.assertEqual(result['latest']['step'], 2)
        self.assertEqual(result['best_in_tail'], 1)

    def test_append_parses_only_new_records(self):
        self.path.write_text('{"step":1,"loss":2,"elapsed":1}\n')
        old = self.parse()
        with self.path.open('a') as file:
            file.write('{"step":2,"loss":1,"elapsed":2}\n')
        with patch.object(monitor, '_parse_stream_line', wraps=monitor._parse_stream_line) as parse_line:
            result = self.parse(old, now=101)
        self.assertEqual(parse_line.call_count, 1)
        self.assertEqual(result['step_per_second'], 1)
        self.assertEqual(result['metric_delta_in_tail'], -1)

    def test_split_utf8_and_incomplete_json_are_resumed_by_byte_offset(self):
        first = '{"step":1,"loss":2,"label":"'.encode() + b'\xe4'
        self.path.write_bytes(first)
        old = self.parse()
        self.assertEqual(old['latest'], {})
        with self.path.open('ab') as file:
            file.write(b'\xb8\xad"}\n{"step":2,"loss":1}')
        result = self.parse(old)
        self.assertEqual(result['latest']['step'], 1)
        with self.path.open('ab') as file:
            file.write(b'\n')
        with patch.object(monitor, '_parse_stream_line', wraps=monitor._parse_stream_line) as parse_line:
            result = self.parse(result)
        self.assertEqual(parse_line.call_count, 1)
        self.assertEqual(result['latest']['step'], 2)

    def test_complete_record_requires_newline_and_handles_crlf(self):
        self.path.write_bytes(b'{"step":1,"loss":2}\r\n{"step":2,"loss":1}')
        old = self.parse()
        self.assertEqual(old['latest']['step'], 1)
        with self.path.open('ab') as file:
            file.write(b'\r\n')
        self.assertEqual(self.parse(old)['latest']['step'], 2)

    def test_window_prunes_old_records_and_best_metric(self):
        lines = [json.dumps({'step': step, 'loss': value}) + '\n'
                 for step, value in [(1, -100), (2, 2), (3, 3), (4, 4)]]
        self.path.write_text(''.join(lines[:2]))
        old = self.parse(limit=len((lines[0] + lines[1]).encode()))
        with self.path.open('a') as file:
            file.write(''.join(lines[2:]))
        limit = len(''.join(lines[1:]).encode())
        result = self.parse(old, limit=limit)
        self.assertEqual(result['best_in_tail'], 2)
        self.assertEqual(result['metric_delta_in_tail'], 2)
        cache = monitor._STREAM_CACHES[result['_cache_key']]
        self.assertEqual(len(cache['data']), limit)
        self.assertEqual([row['row']['step'] for row in cache['records']], [2, 3, 4])

    def test_tail_boundary_keeps_first_complete_line(self):
        self.path.write_text('prefix\n{"step":1}\n{"step":2}\n')
        limit = len('{"step":1}\n{"step":2}\n')
        entry = probe.read_file(self.root, self.path.name, limit, tail=True)
        self.assertEqual(entry['offset'], len('prefix\n'))
        self.assertEqual(base64.b64decode(entry['raw_b64']), b'{"step":1}\n{"step":2}\n')
        result = self.parse(limit=limit)
        self.assertEqual(result['latest']['step'], 2)
        self.assertEqual(len(monitor._STREAM_CACHES[result['_cache_key']]['records']), 2)

    def test_tail_drops_partial_first_record_and_giant_line(self):
        self.path.write_text('x' * 40 + '\n{"step":2}\n')
        entry = probe.read_file(self.root, self.path.name, 20, tail=True)
        self.assertEqual(entry['text'], '{"step":2}\n')
        self.assertEqual(entry['offset'], 41)
        self.path.write_bytes(b'x' * 100)
        entry = probe.read_file(self.root, self.path.name, 20, tail=True)
        self.assertEqual(entry['text'], '')
        self.assertEqual(entry['offset'], entry['read_end'])

    def test_rotation_truncation_and_same_size_rewrite_reset_cache(self):
        self.path.write_text('{"step":10,"loss":1}\n')
        old = self.parse()
        for action in ('rewrite', 'truncate', 'rotate'):
            with self.subTest(action=action):
                if action == 'rotate':
                    self.path.rename(self.root / 'rotated.log')
                self.path.write_text('{"step":20,"loss":2}\n' if action == 'rewrite' else '{"step":1}\n')
                with patch.object(monitor, '_parse_stream_line', wraps=monitor._parse_stream_line) as parse_line:
                    result = self.parse(old, now=110)
                self.assertEqual(parse_line.call_count, 1)
                self.assertEqual(result['latest']['step'], 20 if action == 'rewrite' else 1)
                self.assertEqual(result['progress_since'], 110)
                old = result

    def test_configuration_change_invalidates_cache(self):
        self.path.write_text('{"step":1,"loss":2,"accuracy":9}\n')
        old = self.parse()
        spec = {**self.spec, 'fields': {'step': 'step', 'metric': 'accuracy'}}
        with patch.object(monitor, '_parse_stream_line', wraps=monitor._parse_stream_line) as parse_line:
            result = self.parse(old, spec=spec)
        self.assertEqual(parse_line.call_count, 1)
        self.assertEqual(result['best_in_tail'], 9)

    def test_configuration_change_after_restart_does_not_keep_completion(self):
        self.path.write_text('finished\n')
        old = self.parse(spec={**self.spec, 'format': 'text', 'complete_pattern': 'finished'})
        self.assertTrue(old['phase_complete'])
        monitor._STREAM_CACHES.clear()
        result = self.parse(old, spec={**self.spec, 'format': 'text'})
        self.assertFalse(result['phase_complete'])

    def test_gap_between_windows_preserves_phase_but_reparses_current_rows(self):
        self.path.write_text('{"step":1}\nfinished\n')
        spec = {**self.spec, 'complete_pattern': 'finished'}
        old = self.parse(spec=spec, limit=30)
        with self.path.open('a') as file:
            file.write('no metric\n' * 20 + '{"step":2}\n')
        result = self.parse(old, spec=spec, limit=30)
        self.assertTrue(result['phase_complete'])
        self.assertEqual(result['latest']['step'], 2)
        rows = monitor._STREAM_CACHES[result['_cache_key']]['records']
        self.assertEqual([row['row']['step'] for row in rows if row['row']], [2])

    def test_custom_error_patterns_severity_window_and_disable_defaults(self):
        self.path.write_text('CUDA out of memory\nCUSTOM_FAIL\nnew progress\n')
        spec = {**self.spec, 'format': 'text', 'error_patterns': [
            {'pattern': 'CUSTOM_FAIL', 'severity': 'warning'}], 'error_window_lines': 2}
        result = self.parse(spec=spec)
        self.assertEqual(result['error_matches'], [{'pattern': 'CUSTOM_FAIL', 'severity': 'warning'}])
        self.assertIn('ERROR_LOG', result['alerts'])
        with self.path.open('a') as file:
            file.write('more progress\n')
        result = self.parse(result, spec=spec)
        self.assertEqual(result['error_matches'], [])
        self.assertNotIn('ERROR_LOG', result['alerts'])
        result = self.parse(spec={**spec, 'error_patterns': []})
        self.assertNotIn('ERROR_LOG', result['alerts'])

    def test_default_errors_detect_partial_last_line(self):
        self.path.write_text('CUDA out of memory')
        result = self.parse()
        self.assertIn('ERROR_LOG', result['alerts'])
        self.assertEqual(result['error_matches'][0]['severity'], 'critical')

    def test_missing_log_and_lru_cache_are_bounded(self):
        old = self.parse()
        self.assertTrue(old['missing'])
        self.path.write_text('{"step":1}\n')
        self.assertEqual(self.parse(old)['latest']['step'], 1)
        with patch.object(monitor, 'MAX_STREAM_CACHE_ENTRIES', 3):
            for index in range(8):
                self.parse(spec={**self.spec, 'id': str(index)})
        self.assertEqual(len(monitor._STREAM_CACHES), 3)

    def test_restart_without_memory_cache_rebuilds_valid_window(self):
        self.path.write_text('{"step":1,"loss":2}\n')
        old = self.parse()
        monitor._STREAM_CACHES.clear()
        result = self.parse(old)
        self.assertEqual(result['latest']['step'], 1)
        self.assertEqual(result['best_in_tail'], 2)


if __name__ == '__main__':
    unittest.main()
