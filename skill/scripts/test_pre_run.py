#!/usr/bin/env python3
"""在TMPDIR的独立最小夹具中验证阶段边界，结束自动清理。"""
import copy
import json
import os
from pathlib import Path
import tempfile
import unittest

from check_package import Inspection, strict_json


class PreRunTests(unittest.TestCase):
    def setUp(self):
        base = Path(os.environ.get("TMPDIR", tempfile.gettempdir())) / "zcode-scratch/autoresearch-package-skills"
        base.mkdir(parents=True, exist_ok=True)
        self.temp = tempfile.TemporaryDirectory(prefix="pre-run-", dir=base)
        self.root = Path(self.temp.name)
        for rel in ("workspace/reference", "workspace/harbor_task/environment", "workspace/harbor_task/tests", "expert_evidence", "optimization_evidence"):
            (self.root / rel).mkdir(parents=True, exist_ok=True)
        for rel in ("workspace/harbor_task/instruction.md", "workspace/harbor_task/task.toml", "workspace/harbor_task/environment/Dockerfile", "workspace/harbor_task/tests/Dockerfile", "workspace/harbor_task/tests/test.sh", "expert_evidence/专家作业说明文档.md", "optimization_evidence/训练证据说明.md"):
            (self.root / rel).write_text("synthetic fixture\n")
        self.write("expert_evidence/expert_annotation.json", {"status": "NOT_RUN"})
        self.write("expert_evidence/run_summary.json", {"status": "NOT_RUN", "agent_execution": [
            {"effective_seconds": None, "effective_rounds": None},
            {"effective_seconds": None, "effective_rounds": None}]})
        for name in ("trajectory_codex", "trajectory_seed"):
            self.write("expert_evidence/" + name + ".json", {"status": "NOT_RUN", "reason": "尚未授权启动长跑", "rounds": []})
        self.write("optimization_evidence/comparison_summary.json", {"status": "COMPLETE", "seeds": [1, 2, 3]})
        for role in ("baseline", "reference"):
            for seed in (1, 2, 3):
                rel = "optimization_evidence/" + role + "_runs/seed_" + str(seed)
                self.write(rel + "/result.json", {"schema_version": "fixture", "status": "COMPLETE", "role": role,
                    "seed": seed, "task_type": "non_training", "method": {}, "protocol": {},
                    "training": "NOT_APPLICABLE", "execution": {}, "metrics": {}, "quality_gate": {}, "artifacts": {}})
                (self.root / rel / "run.log").write_text("synthetic stdout/stderr\n")
        self.ledger = {"schema_version": "package-ledger.v1", "package_digest": None,
                       "pending_long_run": ["trajectory_codex", "trajectory_seed"], "items": [
            {"id": name, "layer": "F", "target": "expert_evidence/" + name + ".json", "required_stage": "post_run",
             "status": "合理待生成", "value": None, "reason": "G03后长程前，尚未执行", "sources": [],
             "action": "另行授权后由受管工具生成", "authorization_required": True}
            for name in ("trajectory_codex", "trajectory_seed")]}
        self.write("qa-ledger.json", self.ledger)

    def tearDown(self):
        self.temp.cleanup()

    def write(self, rel, data):
        path = self.root / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(data, ensure_ascii=False, allow_nan=False))

    def report(self, mode="pre_run"):
        return Inspection(self.root, mode).run()

    def failures(self, report):
        return [row for row in report["findings"] if row["status"] == "不通过"]

    def test_pending_is_allowed(self):
        report = self.report()
        self.assertEqual(self.failures(report), [])
        self.assertEqual(sum(row["status"] == "合理待生成" for row in report["findings"]), 2)
        self.assertIsNone(report["summary"]["admission_decision"])

    def test_real_missing_is_not_allowed(self):
        (self.root / "optimization_evidence/baseline_runs/seed_1/run.log").unlink()
        self.assertTrue(any(row["check"] == "E03" for row in self.failures(self.report())))

    def test_post_run_cannot_hide_score(self):
        ledger = copy.deepcopy(self.ledger)
        ledger["items"][0]["id"] = "verifier_reward"
        self.write("qa-ledger.json", ledger)
        self.assertTrue(any(row["check"] == "A06" for row in self.failures(self.report())))

    def test_not_run_positive_duration_or_zero_is_false_measurement(self):
        for number in (36000, 0):
            self.write("expert_evidence/run_summary.json", {"status": "NOT_RUN", "agent_execution": [{"effective_seconds": number}]})
            self.assertTrue(any(row["check"] == "F03" for row in self.failures(self.report())))

    def test_strict_json_nan_duplicate_and_overflow(self):
        for raw in ('{"x":NaN}', '{"x":1,"x":2}', '{"x":1e999}'):
            with self.assertRaises(ValueError):
                strict_json(raw)

    def test_external_symlink_is_not_followed(self):
        (self.root / "outside-link").symlink_to(self.root.parent)
        self.assertTrue(any(row["check"] == "G02" for row in self.failures(self.report())))
        self.assertNotIn("outside-link", self.report()["facts"]["snapshot"]["files"])

    def test_ledger_path_escape(self):
        ledger = copy.deepcopy(self.ledger)
        ledger["items"][0]["target"] = "../outside.json"
        self.write("qa-ledger.json", ledger)
        self.assertTrue(any(row["check"] == "A05" for row in self.failures(self.report())))

    def test_private_hidden_allowed_public_hidden_rejected(self):
        self.write("expert_evidence/archived/hidden_assets/cases.json", {"synthetic": True})
        self.assertEqual(self.failures(self.report()), [])
        self.write("workspace/harbor_task/environment/hidden_assets/cases.json", {"synthetic": True})
        self.assertTrue(any(row["check"] == "G02" for row in self.failures(self.report())))

    def test_delivery_tests_hidden_assets_must_be_empty(self):
        self.write("workspace/harbor_task/tests/hidden_assets/cases.json", {"synthetic": True})
        self.assertTrue(any(row["check"] == "D04" for row in self.failures(self.report())))

    def test_historical_rounds_are_not_forgery(self):
        ledger = copy.deepcopy(self.ledger)
        ledger["pending_long_run"] = []
        for item in ledger["items"]:
            item["status"] = "通过"
        self.write("qa-ledger.json", ledger)
        for name in ("trajectory_codex", "trajectory_seed"):
            self.write("expert_evidence/" + name + ".json", {"rounds": [{"round": 1}]})
        self.write("expert_evidence/run_summary.json", {"status": "HISTORICAL", "agent_execution": [{"effective_seconds": 36000}]})
        self.assertEqual(self.failures(self.report()), [])
        self.assertEqual(self.report()["facts"]["trajectories"]["trajectory_codex"]["state"], "HISTORICAL_PRESENT")

    def test_pre_submit_rejects_pending(self):
        self.assertTrue(any(row["check"] in {"F04", "F05"} for row in self.failures(self.report("pre_submit"))))

    def test_hash_is_stable_and_read_only(self):
        before = self.report()["facts"]["snapshot"]
        after = self.report()["facts"]["snapshot"]
        self.assertEqual(before, after)
        self.assertFalse((self.root / "__pycache__").exists())

    def test_model_training_enforces_artifact(self):
        rel = "optimization_evidence/baseline_runs/seed_1/result.json"
        data = json.loads((self.root / rel).read_text())
        data["task_type"] = "model_training"
        self.write(rel, data)
        self.assertTrue(any(row["check"] == "E04" for row in self.failures(self.report())))
        self.assertTrue(any(row["check"] == "E05" for row in self.report()["findings"]))

    def test_bad_layer_types_are_reported_not_crash(self):
        for value in ("AB", "", None, [], 1):
            ledger = copy.deepcopy(self.ledger)
            ledger["items"][0]["layer"] = value
            self.write("qa-ledger.json", ledger)
            self.assertTrue(any(row["check"] == "A05" for row in self.failures(self.report())))

    def test_structure_permissions_change_separate_digest(self):
        before = self.report()["facts"]
        (self.root / "workspace/reference").chmod(0o700)
        after = self.report()["facts"]
        self.assertEqual(before["snapshot"]["sha256"], after["snapshot"]["sha256"])
        self.assertNotEqual(before["structure_manifest"]["sha256"], after["structure_manifest"]["sha256"])

    def test_ledger_digest_outside_package(self):
        (self.root / "qa-ledger.json").unlink()
        ledger = copy.deepcopy(self.ledger)
        ledger["package_digest"] = self.report()["facts"]["snapshot"]["sha256"]
        external = self.root.parent / (self.root.name + "-ledger.json")
        try:
            external.write_text(json.dumps(ledger))
            self.assertFalse(any(row["check"] == "H04" for row in self.failures(Inspection(self.root, "pre_run", external).run())))
            ledger["package_digest"] = "0" * 64
            external.write_text(json.dumps(ledger))
            self.assertTrue(any(row["check"] == "H04" for row in self.failures(Inspection(self.root, "pre_run", external).run())))
        finally:
            external.unlink(missing_ok=True)

    def test_pending_cannot_prepopulate_best_method(self):
        path = self.root / "expert_evidence/best_method/method.py"
        path.parent.mkdir()
        path.write_text("synthetic candidate\n")
        self.assertTrue(any(row["check"] == "F04" for row in self.failures(self.report())))

    def test_pending_cannot_claim_typical_round_or_reload(self):
        for key, value in (("typical_round_minutes", 5), ("best_method_rerun_passed", True)):
            self.write("expert_evidence/run_summary.json", {"status": "NOT_RUN", "agent_execution": [{key: value}]})
            self.assertTrue(any(row["check"] == "F03" for row in self.failures(self.report())))

    def test_pending_validation_cannot_claim_best_passed(self):
        self.write("expert_evidence/run_summary.json", {"status": "NOT_RUN", "validation": {"best_method_rerun_passed": True}})
        self.assertTrue(any(row["check"] == "F03" for row in self.failures(self.report())))

    def test_historical_rounds_conflict_with_pending_ledger(self):
        self.write("expert_evidence/trajectory_codex.json", {"rounds": [{"round": 1}]})
        self.write("expert_evidence/run_summary.json", {"status": "HISTORICAL"})
        self.assertTrue(any(row["check"] == "F04" for row in self.failures(self.report())))

    def test_global_pending_consistency(self):
        ledger = copy.deepcopy(self.ledger)
        ledger["pending_long_run"] = ["trajectory_codex"]
        self.write("qa-ledger.json", ledger)
        self.assertTrue(any(row["check"] == "A06" for row in self.failures(self.report())))

    def test_post_run_id_cannot_hide_different_target(self):
        ledger = copy.deepcopy(self.ledger)
        ledger["items"][0]["target"] = "workspace/harbor_task/tests/test.sh"
        self.write("qa-ledger.json", ledger)
        self.assertTrue(any(row["check"] == "A06" for row in self.failures(self.report())))

    def test_artifact_hash_and_path_tamper_rejected(self):
        from check_package import sha256
        rel = "optimization_evidence/baseline_runs/seed_1/result.json"
        data = json.loads((self.root / rel).read_text())
        log = "optimization_evidence/baseline_runs/seed_1/run.log"
        data["artifacts"] = {"path": log, "sha256": sha256(self.root / log), "bytes": (self.root / log).stat().st_size}
        self.write(rel, data)
        self.assertFalse(any(row["check"] == "E04" for row in self.failures(self.report())))
        data["artifacts"]["sha256"] = "0" * 64
        self.write(rel, data)
        self.assertTrue(any(row["check"] == "E04" for row in self.failures(self.report())))
        data["artifacts"]["path"] = "../outside-model"
        self.write(rel, data)
        self.assertTrue(any(row["check"] == "E04" for row in self.failures(self.report())))

    def test_untrusted_schema_types_do_not_crash(self):
        rel = "optimization_evidence/baseline_runs/seed_1/result.json"
        data = json.loads((self.root / rel).read_text())
        data.update(status={}, task_type=[])
        self.write(rel, data)
        self.write("expert_evidence/trajectory_codex.json", {"status": [], "rounds": []})
        self.write("expert_evidence/run_summary.json", {"status": {}})
        report = self.report()
        self.assertTrue(any(row["check"] == "E02" and row["status"] == "证据不足" for row in report["findings"]))


if __name__ == "__main__":
    unittest.main(verbosity=2)
