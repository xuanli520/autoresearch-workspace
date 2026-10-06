"""Shared process ownership and self-contained controller release contracts."""
from __future__ import annotations

import json
import hashlib
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from tools.gpu_scheduler.common import processes as scheduler_processes
from tools.process_control import processes
from tools.research_handoff import bundle
from tools.research_handoff.core import processes as handoff_processes

SCRATCH = Path(__file__).resolve().parents[3] / "notes/gpu-scheduler-v1/scratch"


class SharedProcessBundleTests(unittest.TestCase):
    def test_tools_share_one_process_implementation(self):
        self.assertIs(handoff_processes, processes)
        self.assertIs(scheduler_processes, processes)
        self.assertEqual(bundle.render()["core/processes.py"], Path(processes.__file__).read_bytes())

    def test_source_hashes_pin_import_shim_and_shared_implementation(self):
        hashes = bundle.source_hashes()
        self.assertEqual(hashes["core/processes.py"], hashlib.sha256(
            (bundle.ROOT / "core/processes.py").read_bytes()).hexdigest())
        self.assertEqual(hashes["shared/processes.py"], hashlib.sha256(
            Path(processes.__file__).read_bytes()).hexdigest())
        self.assertIn("shared/__init__.py", hashes)
        with mock.patch.object(Path, "read_bytes", autospec=True) as read:
            # Source hash generation reads the shim separately from render().
            read.side_effect = lambda path: b"changed shim" if path == bundle.ROOT / "core/processes.py" else b"source"
            with mock.patch.object(bundle, "render", return_value={"core/processes.py": b"source"}):
                changed = bundle.source_hashes()
            self.assertNotEqual(changed["core/processes.py"], hashes["core/processes.py"])

    def test_standalone_bundle_can_rebuild_and_complete_demo(self):
        SCRATCH.mkdir(parents=True, exist_ok=True)
        with tempfile.TemporaryDirectory(prefix="shared-bundle-", dir=SCRATCH) as temporary:
            root = Path(temporary)
            release = root / "release"
            bundle.build(release)
            self.assertTrue(bundle.verify(release)["valid"])
            env = os.environ.copy()
            env.pop("PYTHONPATH", None)

            def invoke(*args):
                result = subprocess.run([sys.executable, "-B", *map(str, args)],
                                        cwd=root, env=env, capture_output=True, text=True, timeout=15)
                self.assertEqual(result.returncode, 0, result.stderr + result.stdout)
                return json.loads(result.stdout) if result.stdout.strip() else None

            invoke(release / "bundle.py", "--output", root / "rebuilt")
            self.assertTrue(invoke(release / "bundle.py", "--check", root / "rebuilt")["valid"])
            controller = release / "controller.py"
            common = [controller, "--state-dir", root / "state"]
            invoke(*common, "init", "--config", release / "templates/demo.config.json", "--run-id", "smoke")
            invoke(*common, "run", "--run-id", "smoke")
            state = json.loads((root / "state/runs/smoke/state.json").read_text())
            self.assertEqual(state["status"], "COMPLETED")
            self.assertGreaterEqual(state["context"]["generation"], 1)


if __name__ == "__main__":
    unittest.main()
