#!/usr/bin/env python3
"""用临时夹具验证路由/覆盖/hash/上游硬合同；不执行任何题目内容。"""
import copy
import json
import os
from pathlib import Path
import tempfile
import unittest

from check_package import Inspection, sha256
from review_contract import SUITE, check_layer, routes


class ReviewContractTests(unittest.TestCase):
    def setUp(self):
        base = Path(os.environ.get("TMPDIR", tempfile.gettempdir())) / "zcode-scratch/autoresearch-package-skills"
        base.mkdir(parents=True, exist_ok=True)
        self.tmp = tempfile.TemporaryDirectory(prefix="review-contract-", dir=base)
        self.root = Path(self.tmp.name) / "package"
        self.root.mkdir()
        (self.root / "source.txt").write_text("synthetic evidence\n")
        self.output = Path(self.tmp.name) / "review"
        self.output.mkdir()
        self.rows = routes()
        self.snapshot = Inspection(self.root, "pre_run").run()["facts"]["snapshot"]

    def tearDown(self):
        self.tmp.cleanup()

    def report(self, layer="A"):
        route = next(r for r in self.rows if r["layer"] == layer)
        return {"schema_version": "package-layer-review.v1", "mode": "pre_run", "layer": layer,
                "package_digest": self.snapshot["sha256"],
                "definition": {"deliverables": ["synthetic"], "acceptance": ["fixture"], "risks": []},
                "skill": {"path": route["path"], "sha256": route["sha256"]},
                "source_basis": [{"path": "references/requirements-map.md", "sha256": sha256(SUITE / "references/requirements-map.md")}],
                "commands": ["fixture deterministic check"], "upstream": [], "risks": [], "conclusion": "通过",
                "checks": [{"id": cid, "status": "通过", "clause": "synthetic clause", "fact": "fixture bytes", "reason": "fixture only",
                            "action": None, "recheck": None,
                            "evidence": [{"path": "source.txt", "sha256": sha256(self.root / "source.txt"), "locator": "L1"}]}
                           for cid in route["checks"]]}

    def validate(self, report):
        return check_layer(report, self.output / ("layer-" + report["layer"] + ".json"), self.root, self.snapshot, self.rows)

    def test_complete_coverage_and_current_hash(self):
        issues, conclusion = self.validate(self.report())
        self.assertEqual(issues, [])
        self.assertEqual(conclusion, "通过")

    def test_missing_check_cannot_pass(self):
        report = self.report()
        report["checks"].pop()
        self.assertTrue(self.validate(report)[0])

    def test_stale_package_cannot_reuse(self):
        report = self.report()
        report["package_digest"] = "0" * 64
        self.assertTrue(self.validate(report)[0])

    def test_missing_skill_receipt_cannot_pass(self):
        report = self.report()
        report["skill"]["sha256"] = "0" * 64
        self.assertTrue(self.validate(report)[0])

    def test_actual_evidence_changed(self):
        report = self.report()
        (self.root / "source.txt").write_text("changed evidence\n")
        self.assertTrue(self.validate(report)[0])

    def test_upstream_missing_is_insufficient(self):
        report = self.report("B")
        self.assertTrue(self.validate(report)[0])
        report["conclusion"] = "证据不足"
        issues, conclusion = self.validate(report)
        self.assertEqual(issues, [])
        self.assertEqual(conclusion, "证据不足")

    def test_verified_upstream_is_required(self):
        source = self.report()
        path = self.output / "layer-A.json"
        path.write_text(json.dumps(source))
        report = self.report("B")
        report["upstream"] = [{"layer": "A", "path": "@review/layer-A.json", "sha256": sha256(path), "conclusion": "通过"}]
        self.assertEqual(self.validate(report)[0], [])
        report["upstream"][0]["conclusion"] = "证据不足"
        self.assertTrue(self.validate(report)[0])

    def test_waiting_only_allowed_in_postrun_layer(self):
        report = self.report("E")
        row = report["checks"][0]
        row["status"] = "合理待生成"
        row["reason"] = "attempt to mask formal evidence"
        self.assertTrue(self.validate(report)[0])

    def test_missing_repair_fields_rejected(self):
        report = self.report()
        report["checks"][0]["status"] = "证据不足"
        report["conclusion"] = "证据不足"
        self.assertTrue(self.validate(report)[0])

    def test_pass_without_evidence_rejected(self):
        report = self.report()
        report["checks"][0]["evidence"] = []
        self.assertTrue(self.validate(report)[0])

    def test_escape_evidence_rejected(self):
        report = self.report()
        report["checks"][0]["evidence"][0]["path"] = "../review/layer-A.json"
        self.assertTrue(self.validate(report)[0])

    def test_empty_source_commands_locator_rejected(self):
        for key, value in (("source_basis", [{}]), ("commands", [])):
            report = self.report()
            report[key] = value
            self.assertTrue(self.validate(report)[0])
        report = self.report()
        report["checks"][0]["evidence"][0]["locator"] = None
        self.assertTrue(self.validate(report)[0])

    def test_minimal_fake_upstream_rejected(self):
        path = self.output / "layer-A.json"
        path.write_text(json.dumps({"layer": "A", "conclusion": "通过", "package_digest": self.snapshot["sha256"]}))
        report = self.report("B")
        report["upstream"] = [{"layer": "A", "path": "@review/layer-A.json", "sha256": sha256(path), "conclusion": "通过"}]
        self.assertTrue(self.validate(report)[0])

    def test_pre_submit_cannot_reuse_pre_run_upstream(self):
        path = self.output / "layer-A.json"
        path.write_text(json.dumps(self.report()))
        report = self.report("B")
        report["mode"] = "pre_submit"
        report["upstream"] = [{"layer": "A", "path": "@review/layer-A.json", "sha256": sha256(path), "conclusion": "通过"}]
        self.assertTrue(self.validate(report)[0])

    def test_repeated_layer_cannot_fill_final_slots(self):
        import contextlib
        import io
        from review_contract import main
        report = self.report()
        for layer in "ABCDEFGH":
            (self.output / ("layer-" + layer + ".json")).write_text(json.dumps(report))
        stream = io.StringIO()
        with contextlib.redirect_stdout(stream):
            status = main(["final", "--root", str(self.root), "--reports", str(self.output)])
        result = json.loads(stream.getvalue())
        self.assertEqual(status, 1)
        self.assertFalse(result["layers_allow_admission"])
        self.assertTrue(any("槽位" in msg for r in result["reports"] for msg in r["issues"]))

    def test_deterministic_missing_cannot_be_hidden_by_reports(self):
        import contextlib
        import io
        from review_contract import main
        for layer in "ABCDEFGH":
            report = self.report(layer)
            if layer != "A":
                previous = chr(ord(layer) - 1)
                path = self.output / ("layer-" + previous + ".json")
                report["upstream"] = [{"layer": previous, "path": "@review/" + path.name, "sha256": sha256(path), "conclusion": "通过"}]
            (self.output / ("layer-" + layer + ".json")).write_text(json.dumps(report))
        stream = io.StringIO()
        with contextlib.redirect_stdout(stream):
            status = main(["final", "--root", str(self.root), "--reports", str(self.output)])
        result = json.loads(stream.getvalue())
        self.assertEqual(status, 0)  # Report contract is valid, package still fails deterministic gates.
        self.assertTrue(result["contract_valid"])
        self.assertFalse(result["layers_allow_admission"])
        self.assertTrue(result["deterministic_contradictions"])


if __name__ == "__main__":
    unittest.main(verbosity=2)
