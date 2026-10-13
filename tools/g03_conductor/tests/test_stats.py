"""Window boundaries, both directions, σ_B=0, verdict labels, golden summary numbers (design §6)."""
import json
import math

import pytest
from conftest import REPO

from tools.g03_conductor import stats as S

GOLDEN = REPO / "materials" / "example" / "optimization_evidence" / "comparison_summary.json"


def test_sample_std_uses_n_minus_1():
    assert S.sample_std([1.0, 2.0, 3.0]) == pytest.approx(1.0)
    with pytest.raises(ValueError):
        S.sample_std([1.0])


@pytest.mark.parametrize("direction,B,U,R", [("maximize", 0.5, 0.6, 0.55), ("minimize", 0.16, 0.10, 0.145)])
def test_signed_delta_is_positive_for_improvement(direction, B, U, R):
    assert S.signed_delta(direction, B, R) > 0
    assert S.signed_delta(direction, B, U) > 0
    assert 0 < S.normalized(direction, B, U, R) < 1


def test_window_formula_and_validity():
    w = S.window("maximize", 0.0, 0.01, 1.0)
    assert w["d_min"] == pytest.approx(max(0.03, 0.15))
    assert w["d_max"] == pytest.approx(min(1.0 - 0.03, 0.8))
    assert w["valid"]


def test_window_width_exactly_one_sigma_is_valid_and_below_is_invalid():
    # gap G with 3σ ≥ 0.15G and G-3σ ≤ 0.8G  →  width = G - 6σ; width = σ ⇔ G = 7σ
    sigma = 0.01
    exact = S.window("maximize", 0.0, sigma, 7 * sigma)
    assert exact["width"] == pytest.approx(sigma)
    assert exact["valid"]
    below = S.window("maximize", 0.0, sigma, 7 * sigma - 1e-6)
    assert not below["valid"]
    assert below["reason"] == "window width < 1 sigma_B"


def test_window_u_on_wrong_side_is_invalid_both_directions():
    assert not S.window("maximize", 0.5, 0.01, 0.4)["valid"]
    assert not S.window("minimize", 0.16, 0.01, 0.2)["valid"]


def test_minimize_and_maximize_windows_mirror():
    a = S.window("maximize", 0.5, 0.01, 0.7)
    b = S.window("minimize", -0.5, 0.01, -0.7)
    for k in ("d_min", "d_max", "width", "valid"):
        assert a[k] == pytest.approx(b[k])


def test_screen_rule_margin_and_upper_bound():
    w = S.window("maximize", 0.0, 0.01, 1.0)
    assert not S.screen_pass(0.039, 0.01, 1, w)   # < (3+1)σ
    assert S.screen_pass(0.04, 0.01, 1, w)
    assert not S.screen_pass(0.81, 0.01, 1, w)    # > Δ_max


def test_sigma_zero_is_deterministic():
    w = S.window("maximize", 0.5, 0.0, 0.6)
    assert w["deterministic"] and w["valid"]
    v = S.final_verdict("maximize", 0.5, 0.0, 0.53, 0.6, {"ok": True})
    assert v["label"] == "DETERMINISTIC" and v["sigma_multiple"] is None
    assert v["passed"]
    v = S.final_verdict("maximize", 0.5, 0.0, 0.5, 0.6, {"ok": True})
    assert not v["passed"]


def test_verdict_labels_and_checks():
    ok = {"v": True}
    assert S.final_verdict("maximize", 0.0, 0.01, 0.06, 1.0 / 0.25 * 0.06, ok)["label"] == "强证据"
    mid = S.final_verdict("maximize", 0.0, 0.01, 0.04, 0.2, ok)
    assert mid["passed"] and mid["label"] == "需平台复核"
    assert S.final_verdict("maximize", 0.0, 0.01, 0.029, 0.1, ok)["label"] == "FAIL"
    bad_validity = S.final_verdict("maximize", 0.0, 0.01, 0.06, 0.24, {"v": False})
    assert not bad_validity["passed"] and not bad_validity["checks"]["validity"]
    near_u = S.final_verdict("maximize", 0.0, 0.01, 0.08, 0.1, ok)   # |U-R| = 2σ
    assert not near_u["checks"]["u_distance_ge_3sigma"]


def test_golden_summary_numbers():
    g = json.loads(GOLDEN.read_text())
    seeds = [str(s) for s in g["seeds"]]
    b = [g["paired_results"][s]["baseline"] for s in seeds]
    r = [g["paired_results"][s]["reference"] for s in seeds]
    st = g["statistics"]
    direction, U = g["metric"]["direction"], g["metric"]["attainable_anchor"]
    B, sig, R = S.mean(b), S.sample_std(b), S.mean(r)
    assert B == pytest.approx(st["baseline_mean"], rel=1e-12)
    assert sig == pytest.approx(st["baseline_sample_std"], rel=1e-12)
    assert R == pytest.approx(st["reference_mean"], rel=1e-12)
    assert S.sample_std(r) == pytest.approx(st["reference_sample_std"], rel=1e-12)
    imp = [S.signed_delta(direction, x, y) for x, y in zip(b, r)]
    assert S.mean(imp) == pytest.approx(st["mean_paired_improvement"], rel=1e-12)
    assert S.sample_std(imp) == pytest.approx(st["paired_improvement_sample_std"], rel=1e-12)
    # hidden_assets_packaged=false is a desired negative property, not a failed check
    validity = {k: v for k, v in g["validity"].items() if k != "hidden_assets_packaged"}
    v = S.final_verdict(direction, B, sig, R, U, validity)
    assert v["sigma_multiple"] == pytest.approx(st["baseline_sigma_multiple"], rel=1e-12)
    assert v["score"] == pytest.approx(g["normalized_score"]["reference"], rel=1e-12)
    assert v["passed"] == g["significance_rule"]["passed"]
    assert v["label"] == "强证据"
    assert math.isclose(S.signed_delta(direction, B, R) / B, st["relative_improvement"], rel_tol=1e-12)
