"""State machine, locks, k counting, revision caps, failure routing, ledger integrity (design §4, §7)."""
import json
import os
from pathlib import Path

import pytest
from conftest import cli, copy_method, drive_until

from tools.g03_conductor import Conductor
from tools.g03_conductor.ledger import Ledger, LedgerCorrupt
from tools.g03_conductor.state import TRANSITIONS, IllegalTransition, allowed


def test_transition_table_and_illegal_transition(task):
    c = Conductor(task["root"])
    assert c.st.stage == "S0"
    with pytest.raises(IllegalTransition):
        c.transition("S3", "skip ahead")
    for frm, tos in TRANSITIONS.items():
        assert frm not in tos
    assert TRANSITIONS["DONE"] == set() and TRANSITIONS["STOP_UNMET"] == set()


def test_stage_gating_table():
    assert allowed("S0", "g03_run", "diag")[0] is False
    assert allowed("S1", "g03_run", "diag")[0] is True
    assert allowed("S1", "g03_run", "formal_b")[0] is False
    assert allowed("S2", "g03_propose", "protocol")[0] is False
    assert allowed("S4", "g03_run", "screen")[0] is True
    assert allowed("S5", "g03_propose", "candidate")[0] is False
    assert allowed("DONE", "g03_note")[0] is False


def test_llm_cannot_run_formal_or_auto_out_of_stage(task):
    drive_until(task["root"], task["proto"], "S1")
    assert cli("run", "--root", task["root"], "--kind", "formal_b")["code"] == "STAGE_FORBIDDEN"
    assert cli("run", "--root", task["root"], "--auto", "--kind", "formal_b")["code"] == "STAGE_FORBIDDEN"


def test_full_done_path_and_hidden_formal_r(task):
    res = drive_until(task["root"], task["proto"], "S5")
    assert res["action"] == "auto_run" and res["auto_run"]["kind"] == "formal_r"
    ar = res["auto_run"]
    out = cli("run", "--root", task["root"], "--auto", "--kind", "formal_r", "--method", ar["method"],
              "--seed", ar["seeds"][0])
    assert out["results"][0]["metric"] == "hidden-until-S6"
    detail = cli("status", "--root", task["root"], "--detail")["detail"]
    assert any("正式确认跑完前不显示中间统计" in line for line in detail)
    res = drive_until(task["root"], task["proto"], "never")
    assert res["action"] == "terminal" and res["terminal"]["status"] == "DONE"
    final = json.loads((task["root"] / "g03" / "final" / "comparison_summary.json").read_text())
    assert final["schema_version"] == "autoresearch-comparison-summary.v2"
    assert final["significance_rule"]["passed"] is True
    assert all(final["validity"].values())
    assert len(final["paired_results"]) == 5
    assert "g03_conductor" in final and final["g03_conductor"]["confirmation_attempts"]
    assert cli("verify", "--root", task["root"])["ok"]
    # terminal is sticky
    assert cli("run", "--root", task["root"], "--kind", "screen")["code"] == "TERMINAL"


def test_k_counting_and_stop_after_K(task):
    res = drive_until(task["root"], task["proto"], "never", method="overfit")
    t = res["terminal"]
    assert t["status"] == "STOP_UNMET" and t["reason"] == "K_exhausted" and t["attempts_a"] == 2
    c = Conductor(task["root"])
    groups = [e["seeds"] for e in c.st.events if e["type"] == "group_start"]
    assert groups == [[31, 32, 33, 34, 35], [41, 42, 43, 44, 45]]   # k-th confirmation uses k-th group
    fb = [r for r in c.st.runs.values() if r["kind"] == "formal_b"]
    assert len(fb) == 10   # Baseline reran on the new formal seeds
    assert len(c.st.rejected) >= 2


def test_locked_scorer_change_is_detected_and_paused_on_fail(task):
    drive_until(task["root"], task["proto"], "S4")
    scorer = task["root"] / "scorer" / "score.py"
    scorer.write_text(scorer.read_text() + "\n# tampered\n")
    out = cli("run", "--root", task["root"], "--kind", "screen")
    assert out["code"] == "LOCK_VIOLATION"
    env = dict(os.environ, G03_FAKE_VERDICT="FAIL", G03_FAKE_DEFECT="none")
    res = cli("next", "--root", task["root"])
    assert res["action"] == "review" and res["gate"] == "lock_violation"
    cli("review-run", "--root", task["root"], "--gate", res["gate"], "--subject", res["subject"], env=env)
    res = cli("next", "--root", task["root"])
    assert res["action"] == "paused"
    assert cli("run", "--root", task["root"], "--kind", "screen")["code"] == "PAUSED"


def test_baseline_change_after_S1_is_rejected(task):
    drive_until(task["root"], task["proto"], "S1_5")
    (task["root"] / "baseline" / "method.json").write_text('{"gain": -0.01}')
    v = cli("propose", "--root", task["root"], "--kind", "candidate", "--description", "x")
    copy_method(Path(v["path"]), "good")
    out = cli("run", "--root", task["root"], "--kind", "dev", "--method", v["version"])
    assert out["code"] == "LOCK_VIOLATION"


def test_type_b_defect_claim_invalidates_and_returns_to_S1(task):
    drive_until(task["root"], task["proto"], "S4")
    before = Conductor(task["root"]).st
    assert before.major == 1 and before.runs
    claim = cli("propose", "--root", task["root"], "--kind", "defect", "--description", "scorer off-by-one")
    assert claim["ok"]
    res = cli("next", "--root", task["root"])
    assert res["gate"] == "defect_claim"
    cli("review-run", "--root", task["root"], "--gate", res["gate"], "--subject", res["subject"])
    res = cli("next", "--root", task["root"])
    c = Conductor(task["root"])
    assert c.st.stage == "S1" and c.st.major == 2 and c.st.rev_b == 1 and c.st.rev_c == 0
    assert set(before.runs) <= c.st.invalidated       # invalidated, never deleted
    assert all((c.dir / r["run_dir"]).exists() for r in before.runs.values())
    assert c.st.baseline_fixed is None and c.st.lock is None


def test_type_c_revision_cap(task):
    drive_until(task["root"], task["proto"], "S1_5")
    for expect_stage in ("S0", None):
        cli("propose", "--root", task["root"], "--kind", "infeasible", "--description", "budget cannot separate")
        res = cli("next", "--root", task["root"])
        assert res["gate"] == "infeasible_claim"
        cli("review-run", "--root", task["root"], "--gate", res["gate"], "--subject", res["subject"])
        res = cli("next", "--root", task["root"])
        if expect_stage:
            c = Conductor(task["root"])
            assert c.st.stage == "S0" and c.st.rev_c == 1
            # deterministic rule: reusing old formal seeds is rejected
            bad = cli("propose", "--root", task["root"], "--kind", "protocol", "--file", "-", stdin=task["proto"])
            assert bad["code"] == "RULE_VIOLATION"
            new = task["proto"].replace("[31, 32, 33, 34, 35]", "[51, 52, 53, 54, 55]") \
                               .replace("[41, 42, 43, 44, 45]", "[61, 62, 63, 64, 65]")
            assert cli("propose", "--root", task["root"], "--kind", "protocol", "--file", "-", stdin=new)["ok"]
            res = drive_until(task["root"], new, "S1_5")
            assert res["stage"] == "S1_5"
        else:
            assert res["action"] == "terminal"
            assert res["terminal"]["reason"] == "protocol_revision_limit"


def test_sigma_zero_waits_for_expert_signoff(task):
    proto = task["proto"].replace("noise = 0.005", "noise = 0.0")
    score = task["root"] / "scorer" / "score.py"
    score.write_text(score.read_text().replace("noise = 0.005 *", "noise = 0.0 *"))
    res = drive_until(task["root"], proto, "S4")
    assert res["action"] == "wait_expert"
    out = cli("signoff", "--root", task["root"], "--kind", "deterministic", "--expert", "专家", "--note", "合成确定性任务")
    assert out["ok"]
    res = drive_until(task["root"], proto, "never")
    assert res["terminal"]["status"] == "DONE" and res["terminal"]["label"] == "DETERMINISTIC"


def test_control_stop_and_pause(task):
    drive_until(task["root"], task["proto"], "S1")
    cli("control", "--root", task["root"], "--action", "pause", "--reason", "lunch")
    assert cli("next", "--root", task["root"])["action"] == "paused"
    cli("control", "--root", task["root"], "--action", "resume")
    assert cli("next", "--root", task["root"])["action"] != "paused"
    cli("control", "--root", task["root"], "--action", "stop")
    res = cli("next", "--root", task["root"])
    assert res["action"] == "terminal" and res["terminal"]["reason"] == "user_stop"


def test_budget_exhaustion_stops(task):
    c = Conductor(task["root"])
    c.log("run", request_id="synthetic", kind="diag", method="base", seed=0, mult=1, method_hash="x", exec_hash="x",
          budget_hash="x", major=1, status="INVALID", reason="synthetic", metric=None, elapsed=3601.0)
    res = cli("next", "--root", task["root"])
    assert res["action"] == "terminal" and res["terminal"]["reason"] == "gpu_budget_exhausted"


def test_reviewer_undecidable_twice_pauses(task):
    env = dict(os.environ, G03_FAKE_VERDICT="无法判断")
    proto = task["proto"]
    for _ in range(40):
        res = cli("next", "--root", task["root"])
        if res["action"] == "review":
            cli("review-run", "--root", task["root"], "--gate", res["gate"], "--subject", res["subject"], env=env)
        elif res["action"] == "paused":
            break
        else:
            from conftest import dry_run
            dry_run.policy(task["root"], res["stage"], proto, "good")
    assert res["action"] == "paused" and "review_undecidable" in res["message"]


def test_ledger_tamper_detected(task):
    path = task["root"] / "g03" / "ledger.jsonl"
    lines = path.read_text().splitlines()
    rec = json.loads(lines[0])
    rec["config"]["budget_total"]["gpu_hours"] = 999
    lines[0] = json.dumps(rec)
    path.write_text("\n".join(lines) + "\n")
    with pytest.raises(LedgerCorrupt):
        Ledger(path).read()
    assert cli("status", "--root", task["root"])["code"] == "LedgerCorrupt"


def test_request_id_is_deterministic_and_reused(task):
    drive_until(task["root"], task["proto"], "S1")
    a = cli("run", "--root", task["root"], "--kind", "diag")
    b = cli("run", "--root", task["root"], "--kind", "diag")
    assert [r["request_id"] for r in a["results"]] == [r["request_id"] for r in b["results"]]
    assert all(r["reused"] for r in b["results"])
    rid = a["results"][0]["request_id"]
    assert rid.startswith("fake_g03-p1-diag-base.") and rid.endswith("-11")
