"""Stable, low-frequency reloads; callers own the independent check clocks."""
from __future__ import annotations

import copy
import datetime as dt
import hashlib
import json
from pathlib import Path
import time
from typing import Any, Callable


def _utc(value: float) -> str:
    return dt.datetime.fromtimestamp(value, dt.timezone.utc).isoformat()


def _fingerprint(path: Path) -> tuple[int, ...]:
    stat = path.stat()
    return (stat.st_dev, stat.st_ino, stat.st_size, stat.st_mtime_ns, stat.st_ctime_ns)


class _UnstableFile(Exception):
    pass


def _stable_read(path: Path, before: tuple[int, ...]) -> bytes:
    data = path.read_bytes()
    if _fingerprint(path) != before:
        raise _UnstableFile
    return data


def _source_config(cfg: dict[str, Any]) -> dict[str, Any]:
    source = copy.deepcopy({key: value for key, value in cfg.items() if not key.startswith('_')})
    for host in source.get('hosts', {}).values():
        if host.get('transport', 'ssh') == 'ssh':
            for key in ('hostname', 'user', 'port', 'password'):
                host.pop(key, None)
    return source


def _changed_ids(before: dict, after: dict) -> list[str]:
    return sorted(key for key in before.keys() | after.keys() if before.get(key) != after.get(key))


class ConfigReloader:
    """Swap only validated candidates and keep process identity settings fixed.

    ``load_config`` accepts ``(path, raw_bytes=...)`` so validation operates on
    the exact bytes whose SHA was measured, without reopening a changing file.
    ``current`` is the unfiltered runtime configuration; credentials may be
    overlaid there without contaminating the immutable comparison copy.
    """

    def __init__(self, path, current_cfg, load_config: Callable, task_ids=(),
                 auth_getter=None, wall_clock=time.time):
        self._path = Path(path).absolute()
        self._task_ids = tuple(dict.fromkeys(task_ids or ()))
        self._loader = load_config
        self._auth_getter = auth_getter
        self._wall_clock = wall_clock
        self.current = current_cfg
        self._source = _source_config(current_cfg)
        self._state_dir = (self._path.parent / current_cfg.get('state_dir', '.state')).resolve()
        self._version = current_cfg.get('version')
        self._last_fingerprint = None
        self._sha = None
        self._failed_keys: set[tuple] = set()
        self._revision = 1
        self._loaded_at = _utc(self._wall_clock())
        self._last_reload_at = None
        self._last_checked_at = None
        self._reload_state = 'ACTIVE'
        self._last_error = None
        self._effective_poll = 1
        self._changes = {'task_ids': [], 'host_ids': [], 'stream_ids': []}
        # Tie the startup SHA to the supplied config; an edit between main's
        # load and this constructor must still be noticed at the next check.
        try:
            fingerprint = _fingerprint(self._path)
            raw = _stable_read(self._path, fingerprint)
            candidate = self._loader(self._path, raw_bytes=raw)
            if _source_config(candidate) == self._source:
                self._last_fingerprint = fingerprint
                self._sha = hashlib.sha256(raw).hexdigest()
        except Exception:
            pass

    @property
    def path(self) -> Path:
        return self._path

    @property
    def task_ids(self) -> tuple[str, ...]:
        return self._task_ids

    def selected_config(self) -> dict[str, Any]:
        if not self._task_ids:
            return self.current
        selected = dict(self.current)
        selected['tasks'] = [task for task in self.current['tasks'] if task['id'] in self._task_ids]
        return selected

    def metadata(self) -> dict[str, Any]:
        ids = {task['id'] for task in self.current['tasks']}
        return {'path': str(self._path), 'sha256': self._sha,
                'revision': self._revision, 'loaded_at': self._loaded_at,
                'last_reload_at': self._last_reload_at,
                'last_checked_at': self._last_checked_at,
                'reload_state': self._reload_state, 'last_reload_error': self._last_error,
                'task_count': len(self.selected_config()['tasks']),
                'host_count': len(self.current['hosts']),
                'missing_task_ids': sorted(set(self._task_ids) - ids),
                'effective_poll': self._effective_poll,
                'changes': copy.deepcopy(self._changes)}

    def _failure(self, code: str, key: tuple, poll: int, sha=None) -> list[dict]:
        self._reload_state = 'RESTART_REQUIRED' if code == 'RESTART_REQUIRED' else 'FAILED'
        self._last_error = code
        if key in self._failed_keys:
            return []
        self._failed_keys.add(key)
        event = {'event': 'CONFIG_RELOAD_FAILED', 'time': _utc(self._wall_clock()),
                 'path': str(self._path), 'sha256': sha, 'revision': self._revision,
                 'error': code, 'effective_poll': poll}
        return [event]

    def check(self, poll_number: int, path=None) -> list[dict]:
        self._last_checked_at = _utc(self._wall_clock())
        if path is not None and Path(path).absolute() != self._path:
            return self._failure('RESTART_REQUIRED', ('path',), poll_number)
        try:
            fingerprint = _fingerprint(self._path)
        except OSError as exc:
            self._last_fingerprint = None
            code = 'FILE_MISSING' if isinstance(exc, FileNotFoundError) else 'FILE_UNREADABLE'
            return self._failure(code, (code,), poll_number)
        if fingerprint == self._last_fingerprint:
            return []
        try:
            raw = _stable_read(self._path, fingerprint)
        except _UnstableFile:
            return self._failure('FILE_CHANGED_DURING_READ', ('unstable', fingerprint), poll_number)
        except OSError as exc:
            self._last_fingerprint = None
            code = 'FILE_MISSING' if isinstance(exc, FileNotFoundError) else 'FILE_UNREADABLE'
            return self._failure(code, (code,), poll_number)
        self._last_fingerprint = fingerprint
        sha = hashlib.sha256(raw).hexdigest()
        if sha == self._sha:
            self._reload_state, self._last_error = 'ACTIVE', None
            return []
        try:
            raw_config = json.loads(raw)
        except (ValueError, UnicodeError):
            return self._failure('INVALID_JSON', ('sha', sha), poll_number, sha)
        if isinstance(raw_config, dict) and raw_config.get('version') != self._version:
            return self._failure('RESTART_REQUIRED', ('sha', sha), poll_number, sha)
        try:
            candidate = self._loader(self._path, raw_bytes=raw)
            candidate_state = (self._path.parent / candidate.get('state_dir', '.state')).resolve()
            if candidate_state != self._state_dir:
                return self._failure('RESTART_REQUIRED', ('sha', sha), poll_number, sha)
            auth = self._auth_getter() if self._auth_getter else None
            source = _source_config(candidate)
            if auth is not None:
                candidate = self._loader(self._path, auth=auth, raw_bytes=raw)
        except Exception:
            # Validators can include rejected values in exception text. Never
            # copy that text into snapshots or event logs.
            return self._failure('INVALID_CONFIG', ('sha', sha), poll_number, sha)
        before_tasks = {task['id']: task for task in self._source['tasks']}
        after_tasks = {task['id']: task for task in source['tasks']}
        before_streams = {f"{task['id']}/{stream['id']}": stream
                          for task in self._source['tasks'] for stream in task.get('streams', [])}
        after_streams = {f"{task['id']}/{stream['id']}": stream
                         for task in source['tasks'] for stream in task.get('streams', [])}
        changes = {'task_ids': _changed_ids(before_tasks, after_tasks),
                   'host_ids': _changed_ids(self._source['hosts'], source['hosts']),
                   'stream_ids': _changed_ids(before_streams, after_streams)}
        old_sha = self._sha
        self._sha, self._source, self.current = sha, source, candidate
        self._revision += 1
        self._effective_poll = poll_number
        self._last_reload_at = _utc(self._wall_clock())
        self._reload_state, self._last_error = 'ACTIVE', None
        self._changes = changes
        events = [{'event': 'CONFIG_RELOADED', 'time': self._last_reload_at,
                   'path': str(self._path), 'old_sha256': old_sha, 'sha256': sha,
                   'revision': self._revision, 'changes': copy.deepcopy(changes),
                   'effective_poll': poll_number}]
        for task_id in sorted(before_tasks.keys() - after_tasks.keys()):
            events.append({'event': 'CONFIG_TASK_REMOVED', 'time': self._last_reload_at,
                           'task_id': task_id, 'revision': self._revision,
                           'effective_poll': poll_number})
        return events


class AuthReloader:
    """Read credentials only after a changed fingerprint; expose no content SHA."""

    def __init__(self, path, current_auth, load_auth: Callable, apply_auth: Callable,
                 purge_host_key=None):
        self.path = Path(path).absolute()
        self.current = current_auth
        self._loader = load_auth
        self._apply_auth = apply_auth
        self._purge_host_key = purge_host_key
        self._last_fingerprint = None
        self._sha = None
        self._failed_keys: set[tuple] = set()
        self._revision = 1
        self._reload_state = 'ACTIVE'
        self._last_error = None
        if current_auth is not None:
            try:
                fingerprint = _fingerprint(self.path)
                raw = _stable_read(self.path, fingerprint)
                if self._loader(self.path, raw_bytes=raw) == current_auth:
                    self._last_fingerprint = fingerprint
                    self._sha = hashlib.sha256(raw).digest()
            except Exception:
                pass

    def apply_to(self, cfg: dict) -> None:
        if self.current is None:
            return
        updates = {}
        for name, host in cfg['hosts'].items():
            if host.get('transport', 'ssh') == 'ssh':
                updates[name] = self._apply_auth(host, self.current)
        for name, updated in updates.items():
            host = cfg['hosts'][name]
            if (self._purge_host_key and
                    any(host.get(key) != updated.get(key) for key in ('hostname', 'user', 'port'))):
                self._purge_host_key(updated)
        cfg['hosts'].update(updates)

    def metadata(self) -> dict:
        return {'revision': self._revision, 'reload_state': self._reload_state,
                'last_reload_error': self._last_error}

    def _failure(self, code: str, key: tuple) -> list[dict]:
        self._reload_state, self._last_error = 'FAILED', code
        if key in self._failed_keys:
            return []
        self._failed_keys.add(key)
        return [{'event': 'AUTH_RELOAD_FAILED', 'time': _utc(time.time()), 'error': code}]

    def check(self, cfg: dict) -> list[dict]:
        try:
            fingerprint = _fingerprint(self.path)
        except OSError as exc:
            self._last_fingerprint = None
            code = 'FILE_MISSING' if isinstance(exc, FileNotFoundError) else 'FILE_UNREADABLE'
            return self._failure(code, (code,))
        if fingerprint == self._last_fingerprint:
            return []
        try:
            raw = _stable_read(self.path, fingerprint)
        except _UnstableFile:
            return self._failure('FILE_CHANGED_DURING_READ', ('unstable', fingerprint))
        except OSError as exc:
            self._last_fingerprint = None
            code = 'FILE_MISSING' if isinstance(exc, FileNotFoundError) else 'FILE_UNREADABLE'
            return self._failure(code, (code,))
        self._last_fingerprint = fingerprint
        sha = hashlib.sha256(raw).digest()
        if sha == self._sha:
            self._reload_state, self._last_error = 'ACTIVE', None
            return []
        previous = self.current
        try:
            candidate = self._loader(self.path, raw_bytes=raw)
            self.current = candidate
            self.apply_to(cfg)
        except Exception:
            self.current = previous
            return self._failure('INVALID_AUTH', ('sha', sha))
        self._sha = sha
        self._revision += 1
        self._reload_state, self._last_error = 'ACTIVE', None
        return [{'event': 'AUTH_RELOADED', 'time': _utc(time.time()), 'revision': self._revision}]
