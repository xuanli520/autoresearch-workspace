import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock

from tools.gpu_scheduler.docker_cleanup import cleanup


class DockerCleanupTests(unittest.TestCase):
    def test_only_exact_owned_projects_are_removed_and_rechecked(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "trial__env.json").write_text(json.dumps({"project": "trial__env"}))
            docker = Mock(side_effect=["a" * 12 + "\n", "", ""])
            receipt = cleanup(root, docker, root / "receipt.out")
            query = ("ps", "-aq", "--filter", "label=com.docker.compose.project=trial__env")
            self.assertEqual(docker.call_args_list[0].args, query)
            self.assertEqual(docker.call_args_list[1].args, ("rm", "-f", "a" * 12))
            self.assertEqual(docker.call_args_list[2].args, query)
            self.assertTrue(receipt["cleanup_ok"])

    def test_bad_project_is_rejected_before_docker_is_called(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "bad.json").write_text(json.dumps({"project": "*"}))
            docker = Mock()
            with self.assertRaises(ValueError):
                cleanup(root, docker, root / "receipt.out")
            docker.assert_not_called()

    def test_residual_container_does_not_produce_success_receipt(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "trial.json").write_text(json.dumps({"project": "trial"}))
            docker = Mock(side_effect=["a" * 12, "", "b" * 12])
            with self.assertRaises(RuntimeError):
                cleanup(root, docker, root / "receipt.out")
            self.assertFalse((root / "receipt.out").exists())


if __name__ == "__main__":
    unittest.main()
