"""Protocol schema and deterministic revision rules (design §5, §7)."""
import copy

import pytest
from conftest import cli

from tools.g03_conductor import protocol as P
from tools.g03_conductor import yamlish


def _proto(task):
    return P.validate(P.parse_text(task["proto"]), task["root"], task_id="fake_g03",
                      budget_total={"gpu_hours": 1, "deadline_at": "x"}, executor="local_cpu")


def _errors(task, mutate):
    raw = P.parse_text(task["proto"])
    mutate(raw)
    with pytest.raises(P.ProtocolError) as e:
        P.validate(raw, task["root"], task_id="fake_g03", budget_total={"gpu_hours": 1, "deadline_at": "x"},
                   executor="local_cpu")
    return " | ".join(e.value.errors)


def test_valid_protocol_has_defaults(task):
    p = _proto(task)
    assert p["K"] == 2 and p["margin_sigma"] == 1 and p["max_protocol_revisions"] == 1


def test_seed_sets_must_not_overlap(task):
    msg = _errors(task, lambda r: r["seeds"].__setitem__("screen", [11, 22, 23, 24]))
    assert "overlap" in msg


def test_formal_groups_need_n_ge_3_and_K_groups(task):
    assert ">= 3" in _errors(task, lambda r: r["seeds"].__setitem__("formal_pool", [[31, 32], [41, 42]]))
    assert "at least K" in _errors(task, lambda r: r["seeds"].__setitem__("formal_pool", [[31, 32, 33]]))


def test_u_basis_required_and_revisions_fixed(task):
    assert "basis" in _errors(task, lambda r: r["U"].pop("basis"))
    assert "max_protocol_revisions" in _errors(task, lambda r: r.__setitem__("max_protocol_revisions", 2))


def test_data_hash_and_unknown_keys_rejected(task):
    assert "sha256" in _errors(task, lambda r: r["data"]["eval"].__setitem__("sha256", "0" * 64))
    assert "unknown" in _errors(task, lambda r: r.__setitem__("surprise", 1))


def test_private_root_must_not_be_inside_starter(task):
    msg = _errors(task, lambda r: r["paths"].__setitem__("starter", ".."))
    assert "must not contain each other" in msg


def test_propose_rejects_invalid_and_stage_forbidden(task):
    bad = cli("propose", "--root", task["root"], "--kind", "protocol", "--file", "-", stdin="task_id: [")
    assert not bad["ok"] and bad["code"] == "PROTOCOL_INVALID"
    forbidden = cli("run", "--root", task["root"], "--kind", "formal_b")
    assert forbidden["code"] == "STAGE_FORBIDDEN"


def test_revision_c_rules(task):
    old = _proto(task)
    good = copy.deepcopy(old)
    good["seeds"]["formal_pool"] = [[51, 52, 53, 54, 55], [61, 62, 63, 64, 65]]
    used = P.all_formal_seeds(old)
    assert P.check_revision_c(old, good, observed_R=[0.55], used_formal=used, baseline_hash_old="h",
                              baseline_hash_new="h") == []
    same_seeds = copy.deepcopy(good)
    same_seeds["seeds"]["formal_pool"][0][0] = 31
    assert P.check_revision_c(old, same_seeds, observed_R=[], used_formal=used, baseline_hash_old="h",
                              baseline_hash_new="h")
    weaker = copy.deepcopy(good)
    weaker["budget_per_run"]["steps"] = 5
    assert any("weaken" in e for e in P.check_revision_c(old, weaker, observed_R=[], used_formal=used,
                                                         baseline_hash_old="h", baseline_hash_new="h"))
    toward_r = copy.deepcopy(good)
    toward_r["U"] = {"value": 0.56, "basis": "new basis"}
    assert any("toward" in e for e in P.check_revision_c(old, toward_r, observed_R=[0.55], used_formal=used,
                                                         baseline_hash_old="h", baseline_hash_new="h"))
    changed_base = P.check_revision_c(old, good, observed_R=[], used_formal=used, baseline_hash_old="a",
                                      baseline_hash_new="b")
    assert changed_base


def test_draft_changes_in_s1_5_limited_to_budget_increase(task):
    old = _proto(task)
    up = copy.deepcopy(old)
    up["budget_per_run"]["steps"] = 20
    assert P.check_draft_change(old, up, "S1_5") == []
    down = copy.deepcopy(old)
    down["budget_per_run"]["steps"] = 5
    assert P.check_draft_change(old, down, "S1_5")
    u = copy.deepcopy(old)
    u["U"]["value"] = 0.7
    assert P.check_draft_change(old, u, "S1_5")


def test_yamlish_subset():
    d = yamlish.loads("a: [1,\n  2]\nb: {x: 'y # z'}  # c\nc:\n  - 1.5e-3\n  - true\n")
    assert d == {"a": [1, 2], "b": {"x": "y # z"}, "c": [0.0015, True]}
    with pytest.raises(yamlish.YamlError):
        yamlish.loads("a: 1\na: 2\n")
