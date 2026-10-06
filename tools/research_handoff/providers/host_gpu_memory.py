"""Trusted host NVML enforcement for candidate UIDs in one container namespace."""
from __future__ import annotations

import ctypes
import hashlib
import json
import math
import os
import signal
import threading
import time
from pathlib import Path


class MemoryInfo(ctypes.Structure):
    _fields_ = [(name, ctypes.c_ulonglong) for name in ('total', 'free', 'used')]


class ProcessInfo(ctypes.Structure):
    _fields_ = [('pid', ctypes.c_uint), ('usedGpuMemory', ctypes.c_ulonglong),
                ('gpuInstanceId', ctypes.c_uint), ('computeInstanceId', ctypes.c_uint)]


class Nvml:
    def __init__(self, uuid):
        self.lib = ctypes.CDLL('libnvidia-ml.so.1')
        self._bind('nvmlInit_v2', [])
        self._bind('nvmlShutdown', [])
        self._bind('nvmlDeviceGetHandleByUUID', [ctypes.c_char_p, ctypes.POINTER(ctypes.c_void_p)])
        self._bind('nvmlDeviceGetMemoryInfo', [ctypes.c_void_p, ctypes.POINTER(MemoryInfo)])
        self._bind('nvmlDeviceGetComputeRunningProcesses_v2',
                   [ctypes.c_void_p, ctypes.POINTER(ctypes.c_uint), ctypes.POINTER(ProcessInfo)])
        self.check(self.lib.nvmlInit_v2(), 'init')
        self.handle = ctypes.c_void_p()
        try:
            self.check(self.lib.nvmlDeviceGetHandleByUUID(uuid.encode('ascii'), ctypes.byref(self.handle)), 'UUID')
        except BaseException:
            self.close()
            raise

    def _bind(self, name, types):
        f = getattr(self.lib, name)
        f.argtypes = types
        f.restype = ctypes.c_int

    @staticmethod
    def check(rc, operation):
        if rc != 0:
            raise RuntimeError(f'NVML {operation} failed: {rc}')

    def sample(self):
        memory = MemoryInfo()
        self.check(self.lib.nvmlDeviceGetMemoryInfo(self.handle, ctypes.byref(memory)), 'memory')
        for _ in range(4):
            count = ctypes.c_uint(0)
            rc = self.lib.nvmlDeviceGetComputeRunningProcesses_v2(self.handle, ctypes.byref(count), None)
            if rc not in (0, 7):
                self.check(rc, 'process count')
            if count.value == 0:
                if rc != 0:
                    raise RuntimeError('NVML inconsistent empty process query')
                return {'total_bytes': int(memory.total), 'used_bytes': int(memory.used), 'process_bytes': {}}
            capacity = count.value + 8
            records = (ProcessInfo * capacity)()
            count.value = capacity
            rc = self.lib.nvmlDeviceGetComputeRunningProcesses_v2(self.handle, ctypes.byref(count), records)
            if rc == 7:
                continue
            self.check(rc, 'process records')
            if count.value > capacity:
                raise RuntimeError('NVML returned excessive process count')
            processes = {}
            for item in records[:count.value]:
                if item.usedGpuMemory == 2**64 - 1:
                    raise RuntimeError(f'NVML process memory unavailable for PID {item.pid}')
                processes[int(item.pid)] = int(item.usedGpuMemory)
            return {'total_bytes': int(memory.total), 'used_bytes': int(memory.used), 'process_bytes': processes}
        raise RuntimeError('NVML process list did not stabilize')

    def close(self):
        self.lib.nvmlShutdown()


def atomic_json(path, document):
    temporary = path.with_suffix('.tmp')
    temporary.write_text(json.dumps(document, allow_nan=False) + '\n')
    temporary.chmod(0o600)
    os.replace(temporary, path)


def identity(pid):
    proc = Path('/proc') / str(pid)
    fields = (proc/'stat').read_text().rsplit(')', 1)[1].split()
    return {'pid': pid, 'start_ticks': int(fields[19]),
            'namespace': os.readlink(proc/'ns/pid')}


def candidates(namespace, uid):
    result = {}
    for proc in Path('/proc').glob('[0-9]*'):
        try:
            status = dict(line.split(':', 1) for line in (proc/'status').read_text().splitlines() if ':' in line)
            if int(status['Uid'].split()[0]) != uid or os.readlink(proc/'ns/pid') != namespace:
                continue
            record = identity(int(proc.name))
            record['namespace_pid'] = int(status['NSpid'].split()[-1])
            record['gpu_device_open'] = False
            for fd in (proc/'fd').iterdir():
                try:
                    target = os.readlink(fd)
                    if target.startswith('/dev/nvidia'):
                        record['gpu_device_open'] = True
                except FileNotFoundError:
                    pass
            result[int(proc.name)] = record
        except (FileNotFoundError, ProcessLookupError):
            pass
    return result


def terminate(records):
    killed = []
    for pid, record in records.items():
        try:
            current = identity(pid)
            if all(current[key] == record[key] for key in ('pid', 'start_ticks', 'namespace')):
                os.kill(pid, signal.SIGKILL)
                killed.append(pid)
        except (FileNotFoundError, ProcessLookupError):
            pass
    return killed


class HostGpuMemoryGuard:
    """No device-wide delta participates in the candidate memory decision."""
    def __init__(self, container_pid, uuid, output, policy, backend=None):
        if os.geteuid() != 0:
            raise RuntimeError('host GPU guard requires root')
        if policy != {'candidate_uid': 1002, 'max_fraction': 0.2, 'sample_interval_ms': 100,
                      'registration_grace_seconds': 2, 'fail_closed': True}:
            raise ValueError('unsupported host GPU enforcement policy')
        self.policy = policy
        self.output = Path(output)
        self.output.mkdir(parents=True, exist_ok=True, mode=0o700)
        if self.output.stat().st_uid != 0 or self.output.stat().st_mode & 0o077:
            raise RuntimeError('host guard output must be root-owned mode 0700')
        self.namespace = identity(container_pid)['namespace']
        self.backend = backend or Nvml(uuid)
        self.uuid = uuid
        self.stop_event = threading.Event()
        self.first = threading.Event()
        self.pending = {}
        self.state = {'status': 'STARTING', 'policy': policy, 'gpu_uuid': uuid,
                      'container_namespace': self.namespace, 'enforcement': 'host-nvml-container-uid-tree',
                      'source_sha256': hashlib.sha256(Path(__file__).read_bytes()).hexdigest(),
                      'peak_candidate_bytes': 0, 'registered_process_samples': 0, 'sample_count': 0}

    def _measure(self, sample, records, now):
        accounted = []
        for pid, record in records.items():
            memory = sample['process_bytes'].get(pid)
            if memory is None:
                key = (pid, record['start_ticks'])
                if record['gpu_device_open']:
                    since = self.pending.setdefault(key, now)
                    if now - since >= self.policy['registration_grace_seconds']:
                        raise RuntimeError(f'candidate PID {pid} has GPU descriptors but no NVML memory sample')
                    state = 'AWAITING_NVML_REGISTRATION'
                else:
                    self.pending.pop(key, None)
                    state = 'NO_GPU_DEVICE_OPEN'
            else:
                self.pending.pop((pid, record['start_ticks']), None)
                self.state['registered_process_samples'] += 1
                state = 'MEASURED'
            accounted.append({**record, 'used_bytes': memory, 'measurement_status': state})
        return accounted

    def _run(self):
        records = {}
        try:
            with (self.output/'samples.jsonl').open('x') as log:
                while not self.stop_event.is_set():
                    records = candidates(self.namespace, self.policy['candidate_uid'])
                    sample = self.backend.sample()
                    now = time.monotonic()
                    measured = self._measure(sample, records, now)
                    total = sum(v['used_bytes'] for v in measured if v['used_bytes'] is not None)
                    limit = math.floor(sample['total_bytes'] * self.policy['max_fraction'])
                    if limit <= 0:
                        raise RuntimeError('invalid NVML total GPU memory')
                    self.state.update(status='WATCHING', heartbeat_epoch=time.time(), limit_bytes=limit,
                                      candidate_bytes=total, candidates=measured,
                                      device_used_bytes=sample['used_bytes'], sample_count=self.state['sample_count']+1,
                                      peak_candidate_bytes=max(self.state['peak_candidate_bytes'], total))
                    log.write(json.dumps(self.state) + '\n')
                    log.flush()
                    if total > limit:
                        self.state.update(status='TERMINATED', violation=True)
                        atomic_json(self.output/'status.json', self.state)
                        self.state['killed_host_pids'] = terminate(records)
                    atomic_json(self.output/'status.json', self.state)
                    self.first.set()
                    if self.state['status'] == 'TERMINATED':
                        break
                    self.stop_event.wait(self.policy['sample_interval_ms']/1000)
        except BaseException as exc:
            try:
                records.update(candidates(self.namespace, self.policy['candidate_uid']))
            except BaseException:
                pass
            self.state.update(status='ERROR', error=f'{type(exc).__name__}: {exc}', heartbeat_epoch=time.time())
            atomic_json(self.output/'status.json', self.state)
            self.state['killed_host_pids'] = terminate(records)
            atomic_json(self.output/'status.json', self.state)
            self.first.set()
        finally:
            if self.state['status'] == 'WATCHING':
                self.state.update(status='STOPPED', heartbeat_epoch=time.time())
                atomic_json(self.output/'status.json', self.state)
            self.backend.close()

    def start(self):
        self.thread = threading.Thread(target=self._run, name='trusted-host-gpu-guard', daemon=True)
        self.thread.start()
        if not self.first.wait(5) or self.state['status'] != 'WATCHING':
            raise RuntimeError(f'host GPU guard startup failed: {self.state}')

    def close(self):
        self.stop_event.set()
        self.thread.join(timeout=5)
        if self.thread.is_alive():
            terminate(candidates(self.namespace, self.policy['candidate_uid']))
            raise RuntimeError('host GPU guard did not stop')
