import ctypes
import unittest
from tools.research_handoff.providers.host_gpu_memory import HostGpuMemoryGuard, Nvml


class HostGpuMemoryTests(unittest.TestCase):
    def guard(self):
        guard = HostGpuMemoryGuard.__new__(HostGpuMemoryGuard)
        guard.policy = {'registration_grace_seconds': 2}
        guard.pending = {}
        guard.state = {'registered_process_samples': 0}
        return guard

    def test_missing_gpu_measurement_never_becomes_zero(self):
        guard = self.guard()
        records = {17: {'pid': 17, 'start_ticks': 5, 'gpu_device_open': True}}
        measured = guard._measure({'process_bytes': {}}, records, 10)
        self.assertIsNone(measured[0]['used_bytes'])
        self.assertEqual(measured[0]['measurement_status'], 'AWAITING_NVML_REGISTRATION')
        with self.assertRaisesRegex(RuntimeError, 'no NVML memory sample'):
            guard._measure({'process_bytes': {}}, records, 12)

    def test_non_gpu_process_is_explicitly_unmeasured(self):
        measured = self.guard()._measure({'process_bytes': {}},
                                        {17: {'start_ticks': 5, 'gpu_device_open': False}}, 10)
        self.assertIsNone(measured[0]['used_bytes'])
        self.assertEqual(measured[0]['measurement_status'], 'NO_GPU_DEVICE_OPEN')

    def test_candidate_tree_and_external_usage_are_distinct(self):
        guard = self.guard()
        records = {pid: {'start_ticks': 5, 'gpu_device_open': True} for pid in (17, 18)}
        first = guard._measure({'used_bytes': 100000, 'process_bytes': {17: 100, 18: 150, 99: 99900}}, records, 10)
        second = guard._measure({'used_bytes': 250, 'process_bytes': {17: 100, 18: 150}}, records, 11)
        self.assertEqual(sum(v['used_bytes'] for v in first), 250)
        self.assertEqual(sum(v['used_bytes'] for v in second), 250)

    def test_process_api_error_fails_closed(self):
        class FakeLib:
            def nvmlDeviceGetMemoryInfo(self, handle, pointer):
                pointer._obj.total = 1000
                return 0

            def nvmlDeviceGetComputeRunningProcesses_v2(self, *args):
                return 3

        backend = Nvml.__new__(Nvml)
        backend.lib, backend.handle = FakeLib(), ctypes.c_void_p()
        with self.assertRaisesRegex(RuntimeError, 'process count failed: 3'):
            backend.sample()


if __name__ == '__main__':
    unittest.main()
