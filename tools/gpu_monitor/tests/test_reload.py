"""Stable reload checks using private fixtures, without SSH or training."""
import copy
import json
import os
from pathlib import Path
import sys
import tempfile
import unittest
from contextlib import redirect_stdout
from io import StringIO
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import monitor
import reload as reload_module
from reload import AuthReloader, ConfigReloader


class ConfigReloadTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.path = self.root / 'tasks.json'
        self.source = {'version': 1, 'hosts': {'local': {'transport': 'local'}},
                       'tasks': [self.task('one'), self.task('two')], 'state_dir': '.state'}
        self.write(self.source)
        self.initial = monitor.load_config(self.path)
        self.reloader = ConfigReloader(self.path, self.initial, monitor.load_config,
                                       wall_clock=lambda: 1000)

    def task(self, task_id):
        return {'id': task_id, 'host': 'local', 'root': str(self.root),
                'streams': [{'id': 'main', 'path': f'{task_id}.jsonl'}]}

    def write(self, source, atomic=False):
        target = self.path.with_suffix('.new') if atomic else self.path
        target.write_text(json.dumps(source), encoding='utf-8')
        if atomic:
            os.replace(target, self.path)

    def test_unchanged_file_uses_only_stat(self):
        with patch.object(reload_module, '_stable_read') as read:
            for poll in range(2, 20):
                self.assertEqual(self.reloader.check(poll), [])
        read.assert_not_called()
        self.assertEqual(self.reloader.metadata()['revision'], 1)

    def test_atomic_replace_applies_task_host_and_stream_changes_next_poll(self):
        changed = copy.deepcopy(self.source)
        changed['hosts']['other'] = {'transport': 'local'}
        changed['tasks'][0]['streams'][0]['path'] = 'replacement.jsonl'
        changed['tasks'].append(self.task('three'))
        self.write(changed, atomic=True)
        events = self.reloader.check(7)
        self.assertEqual(events[0]['event'], 'CONFIG_RELOADED')
        self.assertEqual(events[0]['effective_poll'], 7)
        self.assertEqual(events[0]['changes'], {'task_ids': ['one', 'three'],
                                               'host_ids': ['other'],
                                               'stream_ids': ['one/main', 'three/main']})
        self.assertEqual(self.reloader.current['tasks'][0]['streams'][0]['path'], 'replacement.jsonl')
        self.assertEqual(self.reloader.metadata()['revision'], 2)
        self.assertNotEqual(events[0]['old_sha256'], events[0]['sha256'])

    def test_in_place_write_is_detected(self):
        changed = copy.deepcopy(self.source)
        changed['interval_seconds'] = 2
        self.write(changed)
        self.assertEqual(self.reloader.check(3)[0]['event'], 'CONFIG_RELOADED')
        self.assertEqual(self.reloader.current['interval_seconds'], 2)

    def test_edit_between_initial_load_and_reloader_is_not_lost(self):
        changed = copy.deepcopy(self.source)
        changed['interval_seconds'] = 3
        self.write(changed)
        reloader = ConfigReloader(self.path, self.initial, monitor.load_config)
        self.assertEqual(reloader.check(1)[0]['event'], 'CONFIG_RELOADED')
        self.assertEqual(reloader.current['interval_seconds'], 3)

    def test_half_json_keeps_previous_config_and_same_fingerprint_is_not_read_again(self):
        self.path.write_bytes(b'{"version": 1, "tasks":')
        events = self.reloader.check(2)
        self.assertEqual(events[0]['error'], 'INVALID_JSON')
        self.assertIs(self.reloader.current, self.initial)
        with patch.object(reload_module, '_stable_read') as read:
            self.assertEqual(self.reloader.check(3), [])
        read.assert_not_called()
        self.write(self.source, atomic=True)
        self.assertEqual(self.reloader.check(4), [])
        self.assertEqual(self.reloader.metadata()['reload_state'], 'ACTIVE')

    def test_same_invalid_sha_and_same_valid_sha_do_not_repeat_events(self):
        invalid = copy.deepcopy(self.source)
        invalid['tasks'].append(self.task('one'))
        self.write(invalid)
        self.assertEqual(self.reloader.check(2)[0]['error'], 'INVALID_CONFIG')
        self.write(invalid, atomic=True)
        self.assertEqual(self.reloader.check(3), [])
        self.write(self.source, atomic=True)
        self.assertEqual(self.reloader.check(4), [])
        self.assertEqual(self.reloader.metadata()['revision'], 1)

    def test_disappearing_file_is_retried_and_recovery_preserves_revision(self):
        self.path.unlink()
        self.assertEqual(self.reloader.check(2)[0]['error'], 'FILE_MISSING')
        self.assertEqual(self.reloader.check(3), [])
        self.write(self.source)
        self.assertEqual(self.reloader.check(4), [])
        self.assertEqual(self.reloader.metadata()['reload_state'], 'ACTIVE')
        self.assertIsNone(self.reloader.metadata()['last_reload_error'])

    def test_candidate_changed_during_read_retries_next_check(self):
        changed = copy.deepcopy(self.source)
        changed['interval_seconds'] = 3
        self.write(changed)
        stable_read = reload_module._stable_read
        with patch.object(reload_module, '_stable_read', side_effect=[
                reload_module._UnstableFile(), stable_read(self.path, reload_module._fingerprint(self.path))]):
            self.assertEqual(self.reloader.check(2)[0]['error'], 'FILE_CHANGED_DURING_READ')
            self.assertEqual(self.reloader.check(3)[0]['event'], 'CONFIG_RELOADED')
        self.assertEqual(self.reloader.current['interval_seconds'], 3)

    def test_process_settings_reject_migration(self):
        for field, value in [('state_dir', 'another-state'), ('version', 2)]:
            with self.subTest(field=field):
                changed = copy.deepcopy(self.source)
                changed[field] = value
                self.write(changed)
                events = self.reloader.check(2)
                self.assertEqual(events[0]['error'], 'RESTART_REQUIRED')
                self.assertIs(self.reloader.current, self.initial)
                self.assertEqual(self.reloader.metadata()['reload_state'], 'RESTART_REQUIRED')
        self.assertEqual(self.reloader.check(3, self.root / 'other.json')[0]['error'], 'RESTART_REQUIRED')
        self.assertEqual(self.reloader.path, self.path)

    def test_fixed_selection_records_missing_selected_task_and_removed_event(self):
        selected = ConfigReloader(self.path, self.initial, monitor.load_config, task_ids=('one',))
        changed = copy.deepcopy(self.source)
        changed['tasks'] = [self.task('two'), self.task('three')]
        self.write(changed, atomic=True)
        events = selected.check(8)
        self.assertEqual(selected.selected_config()['tasks'], [])
        self.assertEqual(selected.metadata()['missing_task_ids'], ['one'])
        self.assertEqual(events[1]['event'], 'CONFIG_TASK_REMOVED')
        self.assertEqual(events[1]['task_id'], 'one')
        self.assertEqual(selected.task_ids, ('one',))

    def test_validation_exceptions_never_expose_rejected_values(self):
        secret = 'PRIVATE-REJECTED-CONFIG-VALUE'
        changed = copy.deepcopy(self.source)
        changed['interval_seconds'] = 2
        self.write(changed)
        self.reloader._loader = lambda *_args, **_kwargs: (_ for _ in ()).throw(ValueError(secret))
        events = self.reloader.check(2)
        self.assertEqual(events[0]['error'], 'INVALID_CONFIG')
        self.assertNotIn(secret, json.dumps(events) + json.dumps(self.reloader.metadata()))

    def test_real_validator_rejects_bad_regex_path_and_controller(self):
        mutations = [lambda cfg: cfg['tasks'][0]['streams'][0].update(format='regex', pattern='['),
                     lambda cfg: cfg['tasks'][0]['streams'][0].update(path='../outside'),
                     lambda cfg: cfg['tasks'][0].update(controller={'type': 'custom', 'run_id': 'run'})]
        for mutate in mutations:
            changed = copy.deepcopy(self.source)
            mutate(changed)
            self.write(changed)
            self.assertEqual(self.reloader.check(2)[0]['error'], 'INVALID_CONFIG')
            self.assertIs(self.reloader.current, self.initial)


class AuthReloadTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.path = self.root / 'auth.txt'
        self.path.write_text('ip: 127.0.0.1\nuser: observer\npassword: PRIVATE-PASSWORD\nport: 22\n')
        self.auth = monitor.load_auth(self.path)
        self.cfg = {'hosts': {'remote': monitor.apply_auth({'transport': 'ssh'}, self.auth),
                              'local': {'transport': 'local'}}}
        self.reloader = AuthReloader(self.path, self.auth, monitor.load_auth, monitor.apply_auth)

    def test_unchanged_auth_is_not_read_at_each_check(self):
        with patch.object(reload_module, '_stable_read') as read:
            for _ in range(10):
                self.assertEqual(self.reloader.check(self.cfg), [])
        read.assert_not_called()

    def test_bad_auth_preserves_current_credentials_and_emits_safe_error_once(self):
        self.path.write_text('ip: PRIVATE-IP-WRONG\nuser: observer\npassword: PRIVATE-PASSWORD\nport: PRIVATE-PORT\n')
        events = self.reloader.check(self.cfg)
        self.assertEqual(events[0]['error'], 'INVALID_AUTH')
        self.assertEqual(self.cfg['hosts']['remote']['password'], 'PRIVATE-PASSWORD')
        self.assertEqual(self.reloader.current, self.auth)
        self.assertNotIn('PRIVATE-', json.dumps(events) + json.dumps(self.reloader.metadata()))
        self.assertEqual(self.reloader.check(self.cfg), [])

    def test_valid_auth_change_applies_and_invalid_utf8_is_safe(self):
        self.path.write_text('ip: 127.0.0.2\nuser: observer\npassword: ROTATED-PASSWORD\nport: 2222\n')
        events = self.reloader.check(self.cfg)
        self.assertEqual(events[0]['event'], 'AUTH_RELOADED')
        self.assertEqual(self.cfg['hosts']['remote']['hostname'], '127.0.0.2')
        self.assertEqual(self.cfg['hosts']['remote']['port'], 2222)
        self.assertEqual(self.cfg['hosts']['local'], {'transport': 'local'})
        self.assertNotIn('ROTATED-PASSWORD', json.dumps(events))
        self.path.write_bytes(b'\xff\xfe')
        self.assertEqual(self.reloader.check(self.cfg)[0]['error'], 'INVALID_AUTH')
        self.assertEqual(self.reloader.current['password'], 'ROTATED-PASSWORD')

    def test_auth_can_overlay_new_config_without_rereading_file(self):
        new_cfg = {'hosts': {'new': {'transport': 'ssh'}}}
        with patch.object(reload_module, '_stable_read') as read:
            self.reloader.apply_to(new_cfg)
        read.assert_not_called()
        self.assertEqual(new_cfg['hosts']['new']['password'], 'PRIVATE-PASSWORD')

    def test_auth_edit_between_initial_load_and_reloader_is_not_lost(self):
        self.path.write_text('ip: 127.0.0.2\nuser: observer\npassword: ROTATED-PASSWORD\n')
        reloader = AuthReloader(self.path, self.auth, monitor.load_auth, monitor.apply_auth)
        self.assertEqual(reloader.check(self.cfg)[0]['event'], 'AUTH_RELOADED')
        self.assertEqual(self.cfg['hosts']['remote']['hostname'], '127.0.0.2')

    def test_apply_failure_does_not_partially_change_hosts_or_current_auth(self):
        self.path.write_text('ip: 127.0.0.2\nuser: observer\npassword: ROTATED-PASSWORD\n')
        self.cfg['hosts']['bad'] = {'transport': 'ssh'}
        before = copy.deepcopy(self.cfg)

        def apply(host, auth):
            if host is self.cfg['hosts']['bad']:
                raise ValueError('ROTATED-PASSWORD')
            return monitor.apply_auth(host, auth)

        self.reloader._apply_auth = apply
        events = self.reloader.check(self.cfg)
        self.assertEqual(events[0]['error'], 'INVALID_AUTH')
        self.assertEqual(self.cfg, before)
        self.assertEqual(self.reloader.current, self.auth)


class WatchReloadIntegrationTests(unittest.TestCase):
    def test_fast_probe_clock_keeps_config_and_auth_checks_low_frequency(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            path = root / 'tasks.json'
            auth_path = root / 'auth.txt'
            auth_path.write_text('ip: 127.0.0.1\nuser: observer\npassword: PRIVATE-PASSWORD\n')
            config = {'version': 1, 'hosts': {'local': {'transport': 'local'}},
                      'interval_seconds': 1,
                      'tasks': [{'id': 'one', 'host': 'local', 'root': str(root)}]}
            path.write_text(json.dumps(config))
            cfg = monitor.load_config(path)
            collected_ids = []

            class Clock:
                now = 0.0
                edited = False

                def monotonic(self):
                    return self.now

                def sleep(self, seconds):
                    self.now += seconds
                    if self.now >= 2 and not self.edited:
                        config['tasks'].append({'id': 'two', 'host': 'local', 'root': str(root)})
                        replacement = path.with_suffix('.new')
                        replacement.write_text(json.dumps(config))
                        os.replace(replacement, path)
                        self.edited = True

            clock = Clock()

            def snapshot(current, previous):
                collected_ids.append([task['id'] for task in current['tasks']])
                return {'collected_at': monitor.utc(1000 + clock.now), 'tasks': [], 'alerts': []}

            with patch.object(monitor.time, 'monotonic', clock.monotonic), \
                    patch.object(monitor.time, 'sleep', clock.sleep), \
                    patch.object(monitor, 'snapshot', snapshot), \
                    patch.object(monitor, 'load_config', wraps=monitor.load_config) as config_load, \
                    patch.object(monitor, 'load_auth', wraps=monitor.load_auth) as auth_load, \
                    redirect_stdout(StringIO()):
                monitor.watch(cfg, root / '.state', 1, 1, 65, False,
                              config_path=path, auth_path=auth_path,
                              config_check_interval=60, auth_check_interval=60)
            self.assertEqual(len(collected_ids), 65)
            self.assertTrue(all(ids == ['one'] for ids in collected_ids[:60]))
            self.assertTrue(all(ids == ['one', 'two'] for ids in collected_ids[60:]))
            self.assertEqual(config_load.call_count, 2)
            self.assertEqual(auth_load.call_count, 1)
            latest = json.loads((root / '.state/latest.json').read_text())
            self.assertEqual(latest['config']['revision'], 2)
            self.assertEqual(latest['config']['effective_poll'], 61)
            watch_info = json.loads((root / '.state/watch.json').read_text())
            self.assertEqual(watch_info['reason'], 'POLL_LIMIT')


if __name__ == '__main__':
    unittest.main()
