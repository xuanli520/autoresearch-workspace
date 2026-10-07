"""Frontend sources over the official collector; no second remote polling loop."""
import copy
import fcntl
import json
from pathlib import Path
import threading
import time
import sys

try:
    from .presentation import freshness
    from .privacy import public_record
except ImportError:
    from presentation import freshness
    from privacy import public_record


def _monitor():
    if __package__:
        from . import monitor
    else:
        import monitor
    return monitor


def collector_running(state):
    """Observe the existing lock without creating state or changing metadata."""
    try:
        with (Path(state) / 'watch.lock').open('r') as lock:
            try:
                fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
                return False
            except BlockingIOError:
                return True
    except FileNotFoundError:
        return False


class SnapshotSource:
    mode = 'background'

    def __init__(self, state, *, max_hours=12, max_polls=None, until_terminal=False, task_ids=()):
        self.state = Path(state)
        self.max_hours, self.max_polls, self.until_terminal = max_hours, max_polls, until_terminal
        self.task_ids = tuple(task_ids)
        self.stop_event, self.refresh_event = threading.Event(), threading.Event()

    def stop(self):
        self.stop_event.set()
        self.refresh_event.set()

    def refresh(self):
        self.refresh_event.set()

    def run(self, on_update, on_state):
        end = time.monotonic() + self.max_hours * 3600
        fingerprint, counted, count, latest = None, None, 0, {}
        reason = 'MONITOR_TIME_LIMIT'
        while time.monotonic() < end and not self.stop_event.is_set():
            info = {'mode': self.mode, 'state': 'UNKNOWN', 'collecting': False}
            try:
                meta = json.loads((self.state / 'watch.json').read_text())
                info.update({k: meta[k] for k in ('state', 'last_poll_at', 'deadline_at', 'polls',
                                                 'task_ids', 'interval_seconds', 'reason') if k in meta})
            except (OSError, ValueError):
                info['cache_error'] = '采集器元数据暂不可读'
            info['collector_alive'] = collector_running(self.state)
            force = self.refresh_event.is_set()
            self.refresh_event.clear()
            try:
                stat = (self.state / 'latest.json').stat()
                candidate = (stat.st_ino, stat.st_mtime_ns, stat.st_size)
                if candidate != fingerprint or force:
                    data = json.loads((self.state / 'latest.json').read_text())
                    if not isinstance(data, dict) or not isinstance(data.get('tasks'), list) or not data.get('collected_at'):
                        raise ValueError('invalid snapshot')
                    latest, fingerprint = public_record(data), candidate
                    generation = data['collected_at']
                    if generation != counted:
                        counted, count = generation, count + 1
                    on_update(latest, {}, info)
            except (OSError, ValueError):
                info['cache_error'] = '快照暂不可读，保留最后有效画面'
            on_state(info)
            if self.max_polls and count >= self.max_polls:
                reason = 'POLL_LIMIT'
                break
            _, stale = freshness(latest, time.time())
            tasks = [t for t in latest.get('tasks', []) if not self.task_ids or t.get('id') in self.task_ids]
            complete_scope = not (set(self.task_ids) - {t.get('id') for t in tasks})
            if self.until_terminal and not stale and complete_scope and tasks and _monitor().all_tasks_terminal(tasks):
                reason = 'ALL_TASKS_TERMINAL'
                break
            self.refresh_event.wait(min(1, max(0, end - time.monotonic())))
        if self.stop_event.is_set():
            reason = 'LOCAL_INTERFACE_EXIT'
        return {'mode': self.mode, 'state': 'STOPPED', 'reason': reason}


class CollectorSource(SnapshotSource):
    mode = 'foreground'

    def __init__(self, cfg, state, *, auth_path, config_path, interval, max_hours=12,
                 max_polls=None, until_terminal=False, task_ids=(), token=None,
                 config_check_interval=60, auth_check_interval=60, interval_override=None):
        super().__init__(state, max_hours=max_hours, max_polls=max_polls, until_terminal=until_terminal)
        self.cfg = copy.deepcopy(cfg)
        self.auth_path, self.config_path = auth_path, config_path
        self.task_ids, self.interval, self.token = task_ids, interval, token
        self.config_check_interval, self.auth_check_interval = config_check_interval, auth_check_interval
        self.interval_override = interval_override
        # Textual captures stdout later; freeze the real terminal capability now.
        self.operator_tty = sys.stdin.isatty() and sys.stdout.isatty()

    def run(self, on_update, on_state):
        monitor = _monitor()
        if collector_running(self.state):
            return self._attach(on_update, on_state)
        on_state({'mode': self.mode, 'state': 'STARTING', 'collecting': True})
        active_hosts = {t['host'] for t in self.cfg['tasks'] if not self.task_ids or t['id'] in self.task_ids}
        if any(self.cfg['hosts'][name].get('transport', 'ssh') == 'ssh' for name in active_hosts):
            auth = monitor.load_auth(self.auth_path)
            for name in active_hosts:
                if self.cfg['hosts'][name].get('transport', 'ssh') == 'ssh':
                    self.cfg['hosts'][name] = monitor.apply_auth(self.cfg['hosts'][name], auth)
            monitor.prepare_ssh({'hosts': {name: self.cfg['hosts'][name] for name in active_hosts}})
        if self.stop_event.is_set():
            return {'mode': self.mode, 'state': 'STOPPED', 'reason': 'LOCAL_INTERFACE_EXIT'}
        try:
            return monitor.watch(self.cfg, self.state, self.interval, self.max_hours, self.max_polls,
                                 self.until_terminal, self.token, self.auth_path, self.config_path,
                                 self.task_ids, self.config_check_interval, self.auth_check_interval,
                                 self.interval_override, on_update=on_update, on_state=on_state,
                                 stop_event=self.stop_event, refresh_event=self.refresh_event,
                                 operator_tty=self.operator_tty)
        except ValueError:
            if collector_running(self.state):
                return self._attach(on_update, on_state)
            raise

    def _attach(self, on_update, on_state):
        # Another official collector won the lock. Reuse it instead of probing twice.
        self.mode = 'background'
        return super().run(on_update, on_state)
