"""Deterministic G03 statistics (design §6). Pure functions, no I/O."""
from __future__ import annotations

import math

EPS = 1e-12
SCORE_LO, SCORE_HI = 0.15, 0.8


def mean(values: list[float]) -> float:
    if not values:
        raise ValueError("mean of empty list")
    return math.fsum(values) / len(values)


def sample_std(values: list[float]) -> float:
    """σ with n-1 denominator; requires n >= 2."""
    if len(values) < 2:
        raise ValueError("sample std needs n >= 2")
    m = mean(values)
    return math.sqrt(math.fsum((v - m) ** 2 for v in values) / (len(values) - 1))


def signed_delta(direction: str, baseline: float, value: float) -> float:
    """Improvement of value over baseline, positive = better."""
    if direction == "maximize":
        return value - baseline
    if direction == "minimize":
        return baseline - value
    raise ValueError(f"unknown direction {direction!r}")


def _ge(a: float, b: float) -> bool:
    return a >= b - EPS * max(1.0, abs(a), abs(b))


def _le(a: float, b: float) -> bool:
    return a <= b + EPS * max(1.0, abs(a), abs(b))


def window(direction: str, B: float, sigma: float, U: float) -> dict:
    """Feasible improvement window [d_min, d_max] in signed-improvement units."""
    gap = signed_delta(direction, B, U)
    out = {"B": B, "sigma_B": sigma, "U": U, "gap": gap, "deterministic": sigma == 0}
    if gap <= 0:
        out.update(d_min=None, d_max=None, width=None, valid=False,
                   reason="U is not on the improvement side of B")
        return out
    if sigma == 0:
        d_min, d_max = SCORE_LO * gap, SCORE_HI * gap
        out.update(d_min=d_min, d_max=d_max, width=d_max - d_min, valid=True, reason="deterministic (sigma_B=0)")
        return out
    d_min = max(3 * sigma, SCORE_LO * gap)
    d_max = min(gap - 3 * sigma, SCORE_HI * gap)
    width = d_max - d_min
    valid = _ge(width, sigma)
    out.update(d_min=d_min, d_max=d_max, width=width, valid=valid,
               reason="ok" if valid else "window width < 1 sigma_B")
    return out


def normalized(direction: str, B: float, U: float, value: float) -> float:
    gap = signed_delta(direction, B, U)
    return signed_delta(direction, B, value) / gap


def screen_pass(delta: float, sigma: float, margin: float, win: dict) -> bool:
    """Screening / feasibility rule: Δ ≥ (3+m)σ_B and Δ ≤ Δ_max (deterministic: Δ ∈ [Δ_min, Δ_max])."""
    if not win.get("valid"):
        return False
    if sigma == 0:
        return delta > 0 and _ge(delta, win["d_min"]) and _le(delta, win["d_max"])
    return _ge(delta, (3 + margin) * sigma) and _le(delta, win["d_max"])


def final_verdict(direction: str, B: float, sigma: float, R: float, U: float, validity: dict[str, bool]) -> dict:
    delta = signed_delta(direction, B, R)
    gap = signed_delta(direction, B, U)
    score = delta / gap if gap > 0 else None
    u_minus_r = abs(U - R)
    checks: dict[str, bool] = {}
    if sigma == 0:
        checks["real_improvement"] = delta > 0
        checks["u_distance"] = u_minus_r > 0
        multiple = None
    else:
        multiple = delta / sigma
        checks["delta_ge_3sigma"] = _ge(delta, 3 * sigma)
        checks["u_distance_ge_3sigma"] = _ge(u_minus_r, 3 * sigma)
    checks["score_in_range"] = score is not None and _ge(score, SCORE_LO) and _le(score, SCORE_HI)
    checks["validity"] = all(validity.values()) if validity else False
    passed = all(checks.values())
    if sigma == 0:
        label = "DETERMINISTIC"
    elif not passed:
        label = "FAIL"
    elif multiple is not None and _ge(multiple, 5):
        label = "强证据"
    else:
        label = "需平台复核"
    return {"passed": passed, "delta": delta, "sigma_multiple": multiple, "score": score,
            "u_minus_r": u_minus_r, "checks": checks, "label": label, "validity": dict(validity)}
