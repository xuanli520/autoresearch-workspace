import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from core.longrun import ControllerError, validate_config


def config(budget):
    return {
        "version": 1,
        "stage": "diagnostic", "score_expectation": "not_expected", "metric": "runtime", "direction": "max",
        "required_seeds": [], "deadline": "2099-01-01T00:00:00Z",
        "candidate_manifest": "candidate.manifest.json", "protocol_hash": "0" * 64,
        "command": ["true"],
        "budget": budget,
        "context": {"max_tokens": 32768, "compact_at_tokens": 26000, "reserve_tokens": 4096},
    }


class ExtendedDeadlineTests(unittest.TestCase):
    def test_over_twelve_hours_requires_explicit_opt_in(self):
        with self.assertRaises(ControllerError):
            validate_config(config({"window_seconds": 40000, "hard_limit_seconds": 54001}))

        validated = validate_config(config({
            "window_seconds": 40000,
            "hard_limit_seconds": 54001,
            "allow_extended_hard_limit": True,
        }))
        self.assertEqual(validated["budget"]["hard_limit_seconds"], 54001)

    def test_extended_deadline_is_bounded_to_three_days(self):
        validated = validate_config(config({
            "window_seconds": 40000,
            "hard_limit_seconds": 90000,
            "allow_extended_hard_limit": True,
        }))
        self.assertEqual(validated["budget"]["hard_limit_seconds"], 90000)
        validated = validate_config(config({
            "window_seconds": 40000,
            "hard_limit_seconds": 201000,
            "allow_extended_hard_limit": True,
        }))
        self.assertEqual(validated["budget"]["hard_limit_seconds"], 201000)
        with self.assertRaises(ControllerError):
            validate_config(config({
                "window_seconds": 40000,
                "hard_limit_seconds": 259201,
                "allow_extended_hard_limit": True,
            }))

    def test_extension_flag_must_be_boolean(self):
        with self.assertRaises(ControllerError):
            validate_config(config({
                "window_seconds": 40000,
                "hard_limit_seconds": 54001,
                "allow_extended_hard_limit": "yes",
            }))


if __name__ == "__main__":
    unittest.main()
