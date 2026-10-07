"""Task adapter failure events, without model, Docker or scheduler calls."""
from __future__ import annotations

import hashlib
import importlib.util
import json
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from tools.research_handoff.core.completion import CompletionContractError, EvidenceError


WORKSPACE = Path(__file__).resolve().parents[3]


def load_adapters(task):
    directory = next(WORKSPACE.glob(f"autoresearch_*/{task}/ops/adapters"))
    if task == "auto0802":
        directory /= "harbor_research"
    modules = {}
    with patch.object(sys, "path", [str(directory), *sys.path]), patch.dict(sys.modules):
        for name in ("effective_time", "research_turn", "scored_research"):
            spec = importlib.util.spec_from_file_location(name, directory / (name + ".py"))
            module = importlib.util.module_from_spec(spec)
            sys.modules[name] = module
            spec.loader.exec_module(module)
            modules[name] = module
    return modules


class ScoredFailureReportingTests(unittest.TestCase):
    def test_completion_contract_error_reaches_main_handler(self):
        for task in ("auto0802", "auto0804"):
            with self.subTest(task=task):
                modules = load_adapters(task)
                runner, scored = modules["research_turn"], modules["scored_research"]
                error = CompletionContractError("SCIENTIFIC_CONTRACT_MISMATCH", "different protocol")
                with patch.dict(sys.modules, {"scored_research": scored}), \
                     patch.object(sys, "argv", ["research_turn", "run", "--config", "unused", "--group", "gpt"]), \
                     patch.object(runner, "read", return_value={"formal_scoring": True}), \
                     patch.object(scored, "run", side_effect=error), \
                     patch.object(runner, "report_evidence_failure") as report:
                    self.assertEqual(runner.main(), 70)
                    self.assertIs(report.call_args.args[2], error)

    def test_failure_classification_preserves_score_and_hashed_original(self):
        cases = (
            (CompletionContractError("SOURCE_DEADLINE_EXCEEDS_PARENT", "incompatible deadline"),
             "completion_contract_error", "contract", -0.125),
            (EvidenceError("FINAL_SCORE_INVALID", "RELOAD_MISSING", "no reload evidence"),
             "deterministic_evidence_failure", "evidence", None),
        )
        for task in ("auto0802", "auto0804"):
            for error, reason, category, score in cases:
                with self.subTest(task=task, reason=reason), tempfile.TemporaryDirectory() as temporary:
                    modules = load_adapters(task)
                    runner = modules["research_turn"]
                    root, turn = Path(temporary), Path(temporary) / "turn"
                    turn.mkdir()
                    latest = root / "agents/gpt/latest.json"
                    latest.parent.mkdir(parents=True)
                    original = json.dumps({"method_summary": "Trained and scored the candidate.", "score": -0.125})
                    latest.write_text(original)
                    config = {"stage_root": str(root), "agents": {"gpt": {"run_id": "original-run"}}}
                    with patch.dict(sys.modules, {"scored_research": modules["scored_research"]}), \
                         patch.dict(os.environ, {"AUTORESEARCH_TURN_DIR": str(turn), "AUTORESEARCH_TURN": "342"}), \
                         patch.object(runner, "emit") as emit, patch("builtins.print"):
                        runner.report_evidence_failure(config, "gpt", error)
                        event = emit.call_args.kwargs
                        self.assertEqual(emit.call_args.args, ("turn.failed",))
                        self.assertEqual(event["reason"], reason)
                        self.assertEqual(event["failure_class"], category)
                        self.assertIs(event["retryable"], False)
                        self.assertEqual(event["score"], score)
                        evidence = Path(event["failure_evidence"])
                        before = evidence.read_bytes()
                        self.assertEqual(hashlib.sha256(before).hexdigest(), event["failure_evidence_sha256"])
                        self.assertEqual(json.loads(before)["failure_category"], reason)
                        runner.report_evidence_failure(config, "gpt", error)
                        self.assertEqual(evidence.read_bytes(), before)
                    self.assertEqual(latest.read_text(), original)


if __name__ == "__main__":
    unittest.main()
