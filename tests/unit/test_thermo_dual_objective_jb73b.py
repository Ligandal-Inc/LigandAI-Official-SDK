# Copyright © 2026 Ligandal, Inc. All rights reserved.
"""The dual objective: ipSAE GATES, DeltaForge Kd SCORES. bd-LIGANDAI_ALPHA_V2-jb73b.

Every selection surface here was ipSAE-only. ipSAE says an interface was
modelled; the deliverable is stated in molar units. These tests pin the five
rules that let Kd travel through the selectivity panel, the co-optimization
aggregate and the refinement verdict without acquiring a meaning it has not
earned:

1. gate on peptide ipSAE BEFORE scoring — DeltaForge scores an interface
2. geometric mean over draws, never the median — log10(Kd) is symmetric
3. no ratio on an unconfirmed denominator — screen depth runs ~22x optimistic
4. direction travels with the metric — Kd's worst case is the MAX
5. an absent Kd is not 0 nM — 0 nM is infinitely tight and sorts to the top

SCOPE. This file owns the shared primitives (:mod:`ligandai.thermo`) and the
CO-OPTIMIZATION objective (:mod:`ligandai.multitarget`). The panel-side carriage
of Kd — ``PanelLeg`` fields, ``_hydrate_metrics``, ``deltas()`` rows — belongs to
bd-LIGANDAI_ALPHA_V2-jb73b.2 and is covered by
``tests/unit/test_panel_thermo_jb73b.py``. Two implementations of one ordering is
the defect class both files exist to prevent, so neither reaches into the other.

No network: nothing here touches the transport, so the whole file runs offline
and burns nothing.
"""

from __future__ import annotations

import math

import pytest

from ligandai.errors import LigandAIValidationError
from ligandai.multitarget import (
    CoOptimization,
    TargetSpec,
    aggregate_across_targets,
    dual_objective_pass,
)
from ligandai.thermo import (
    CONFIRM_MIN_DRAWS,
    VALID_IPSAE,
    clears_floor,
    expected_false_positives,
    geometric_mean_kd,
    kd_bootstrap_ci,
    kd_provenance_ok,
    multiplicity_note,
    selectivity_ratio,
    worst_case,
)

A = "ACDEFGHIKLMNP"
B = "WYVTSRQNKHGFE"


# ─────────────────────────────────────────────────────────────────────────────
# 1. Estimator — geometric mean, not the median
# ─────────────────────────────────────────────────────────────────────────────


def test_geometric_mean_is_not_the_median_on_a_log_symmetric_cell():
    # Median would return 10.0 and hide both tails. The geometric mean is the
    # efficient estimator for a log-symmetric quantity.
    draws = [1.0, 10.0, 100.0]
    assert geometric_mean_kd(draws) == pytest.approx(10.0)
    # Now break the symmetry: the median is unmoved, the geometric mean is not.
    draws2 = [1.0, 10.0, 10000.0]
    import statistics
    assert statistics.median(draws2) == pytest.approx(10.0)      # order statistic: inert
    # 10^(5/3): the estimator tracked the loose pose the median discarded.
    assert geometric_mean_kd(draws2) == pytest.approx(10 ** (5 / 3))
    assert geometric_mean_kd(draws2) > 4 * statistics.median(draws2)


def test_an_absent_kd_is_not_a_kd_of_zero():
    with pytest.raises(LigandAIValidationError, match="not a Kd of zero"):
        geometric_mean_kd([])


def test_a_nonpositive_kd_is_a_scorer_failure_not_a_measurement():
    with pytest.raises(LigandAIValidationError, match="finite and > 0"):
        geometric_mean_kd([5.0, 0.0])
    with pytest.raises(LigandAIValidationError, match="finite and > 0"):
        geometric_mean_kd([5.0, -1.0])


def test_a_single_draw_yields_no_confidence_interval():
    # An interval derived from one point is the point. Reporting it as a CI
    # states a precision that was never measured.
    assert kd_bootstrap_ci([4.2]) is None
    lo, hi = kd_bootstrap_ci([1.0, 10.0, 100.0])
    assert lo < 10.0 < hi


# ─────────────────────────────────────────────────────────────────────────────
# 2. Direction travels with the metric
# ─────────────────────────────────────────────────────────────────────────────


def test_worst_case_for_kd_is_the_max_not_the_min():
    kds = {"BRD4@human": 5.0, "BRD4@mouse": 900.0}
    v, limiting = worst_case(kds, "lower_is_better")
    assert v == pytest.approx(900.0)
    assert limiting == "BRD4@mouse"      # the leg holding the design back
    # Under ipSAE's direction the SAME mapping names the opposite leg — this is
    # the silent inversion the direction parameter exists to prevent.
    v2, limiting2 = worst_case(kds, "higher_is_better")
    assert limiting2 == "BRD4@human"
    assert v2 == pytest.approx(5.0)


def test_clears_floor_flips_with_direction():
    assert clears_floor(0.90, 0.67, "higher_is_better") is True
    assert clears_floor(0.50, 0.67, "higher_is_better") is False
    assert clears_floor(12.0, 100.0, "lower_is_better") is True      # 12 nM beats a 100 nM bar
    assert clears_floor(900.0, 100.0, "lower_is_better") is False


def test_unknown_direction_is_refused_by_name():
    with pytest.raises(LigandAIValidationError, match="lower_is_better"):
        worst_case({"X": 1.0}, "tighter")  # type: ignore[arg-type]


def test_aggregate_across_targets_takes_a_direction_and_inverts_for_kd():
    kds = {"BRD4@human": 5.0, "BRD4@mouse": 900.0}
    v, limiting = aggregate_across_targets(kds, "worst_case", direction="lower_is_better")
    assert v == pytest.approx(900.0)
    assert limiting == "BRD4@mouse"
    # 'mean' over Kd is a GEOMETRIC mean — averaging raw Kd is a units error
    # that lets one weak leg dominate a log-scale quantity.
    m, _ = aggregate_across_targets(kds, "mean", direction="lower_is_better")
    assert m == pytest.approx(math.sqrt(5.0 * 900.0))


def test_direction_defaults_to_ipsae_so_existing_callers_are_unchanged():
    v, limiting = aggregate_across_targets({"a": 0.90, "b": 0.62}, "worst_case")
    assert v == pytest.approx(0.62) and limiting == "b"


# ─────────────────────────────────────────────────────────────────────────────
# 3. The dual objective — ipSAE gates, Kd scores
# ─────────────────────────────────────────────────────────────────────────────


def _co() -> CoOptimization:
    return CoOptimization(
        [TargetSpec(gene="BRD4", organism="human"),
         TargetSpec(gene="BRD2", organism="human")],
        aggregate="worst_case",
    )


def test_a_leg_below_the_ipsae_gate_fails_even_with_a_stellar_kd():
    # DeltaForge scored a non-interface. The Kd is about noise, and a tight
    # number there is the most dangerous possible output.
    ok, detail = dual_objective_pass(
        per_target_ipsae={"BRD4@human": 0.30, "BRD2@human": 0.90},
        per_target_kd_nm={"BRD4@human": 0.001, "BRD2@human": 5.0},
        ipsae_gate=VALID_IPSAE, kd_ceiling_nm=100.0, aggregate="worst_case",
    )
    assert ok is False
    assert detail["gate_pass"] is False
    assert detail["per_target_gate"]["BRD4@human"] is False
    # ⛔ and the Kd of the ungated leg must NOT be reported as an affinity
    assert detail["per_target_kd_nm"].get("BRD4@human") is None
    assert "BRD4@human" in detail["ungated_targets"]


def test_a_missing_kd_makes_the_row_incomplete_not_a_zero():
    ok, detail = dual_objective_pass(
        per_target_ipsae={"BRD4@human": 0.90, "BRD2@human": 0.88},
        per_target_kd_nm={"BRD4@human": 5.0},        # BRD2 never scored
        ipsae_gate=VALID_IPSAE, kd_ceiling_nm=100.0, aggregate="worst_case",
    )
    assert ok is False
    assert detail["complete"] is False
    assert detail["kd_missing"] == ("BRD2@human",)
    # 0 nM would be infinitely tight binding and would sort this row to the top.
    assert detail["aggregate_kd_nm"] is None


def test_both_gates_clear_and_the_worst_kd_leg_is_named():
    ok, detail = dual_objective_pass(
        per_target_ipsae={"BRD4@human": 0.90, "BRD2@human": 0.88},
        per_target_kd_nm={"BRD4@human": 5.0, "BRD2@human": 40.0},
        ipsae_gate=VALID_IPSAE, kd_ceiling_nm=100.0, aggregate="worst_case",
    )
    assert ok is True and detail["complete"] is True
    assert detail["aggregate_kd_nm"] == pytest.approx(40.0)   # worst case = max
    assert detail["limiting_kd_target"] == "BRD2@human"


def test_worst_case_kd_fails_when_one_leg_misses_the_ceiling():
    ok, detail = dual_objective_pass(
        per_target_ipsae={"BRD4@human": 0.90, "BRD2@human": 0.88},
        per_target_kd_nm={"BRD4@human": 5.0, "BRD2@human": 900.0},
        ipsae_gate=VALID_IPSAE, kd_ceiling_nm=100.0, aggregate="worst_case",
    )
    assert ok is False
    assert detail["per_target_kd_pass"]["BRD2@human"] is False


def test_cooptimization_exposes_the_dual_objective_under_its_own_rule():
    co = _co()
    ok, detail = co.dual_objective(
        per_target_ipsae={"BRD4@human": 0.90, "BRD2@human": 0.88},
        per_target_kd_nm={"BRD4@human": 5.0, "BRD2@human": 40.0},
        ipsae_gate=VALID_IPSAE, kd_ceiling_nm=100.0,
    )
    assert ok is True
    assert detail["aggregate_rule"] == "worst_case"


# ─────────────────────────────────────────────────────────────────────────────
# 4. Selectivity is a Kd RATIO, and it is withheld on a soft denominator
# ─────────────────────────────────────────────────────────────────────────────


def test_ratio_is_withheld_when_the_on_target_leg_is_screen_depth():
    r = selectivity_ratio(
        on_target_kd_nm=1.03, off_target_kd_nm={"ENPP6": 500.0},
        on_target_n_draws=1,
    )
    assert r.ratio is None
    assert r.quotable is False
    assert r.reason == "on_target_unconfirmed"
    assert "22x" in r.note


def test_ratio_is_computed_on_a_confirmed_denominator_and_uses_the_tightest_off_target():
    r = selectivity_ratio(
        on_target_kd_nm=10.0,
        off_target_kd_nm={"ENPP6": 5000.0, "ENPP1": 500.0},
        on_target_n_draws=CONFIRM_MIN_DRAWS,
    )
    assert r.quotable is True
    assert r.tightest_off_target == "ENPP1"        # min off-target Kd, not max
    assert r.ratio == pytest.approx(50.0)


def test_ratio_is_withheld_when_the_on_target_leg_never_cleared_the_ipsae_gate():
    r = selectivity_ratio(
        on_target_kd_nm=0.5, off_target_kd_nm={"ENPP6": 500.0},
        on_target_n_draws=4, on_target_gated=False,
    )
    assert r.ratio is None and r.reason == "on_target_not_gated"


def test_a_missing_on_target_kd_is_not_a_ratio_of_one():
    r = selectivity_ratio(
        on_target_kd_nm=None, off_target_kd_nm={"ENPP6": 500.0}, on_target_n_draws=4
    )
    assert r.ratio is None and r.reason == "on_target_missing"


def test_no_off_target_kd_is_not_selectivity():
    r = selectivity_ratio(
        on_target_kd_nm=10.0, off_target_kd_nm={}, on_target_n_draws=4
    )
    assert r.ratio is None and r.reason == "off_targets_missing"


# ─────────────────────────────────────────────────────────────────────────────
# 5. Multiplicity — ranking N designs is N tests
# ─────────────────────────────────────────────────────────────────────────────


def test_nominal_hits_are_compared_against_chance():
    assert expected_false_positives(200, 0.05) == pytest.approx(10.0)
    note = multiplicity_note(n_hits=3, n_tests=200, alpha=0.05)
    assert note["exceeds_chance"] is False
    assert "does NOT exceed chance" in note["note"]
    note2 = multiplicity_note(n_hits=40, n_tests=200, alpha=0.05)
    assert note2["exceeds_chance"] is True


# ─────────────────────────────────────────────────────────────────────────────
# 6. Kd provenance — the v10.2 scorer, NOT calibration_head
# ─────────────────────────────────────────────────────────────────────────────


def test_a_v102_row_is_admissible_on_its_scorer_version():
    ok, why = kd_provenance_ok({
        "kd_nm": 86.197, "scorer_version": "v10_2_unified_parallel_2026_05_08",
        "affinity_quotable": True,
    })
    assert ok is True and "v10_2_unified_parallel" in why


def test_calibration_head_is_not_required_because_production_never_sets_it():
    # ⛔ VERIFIED on production 2026-09-23: absent from every batch_score_fold
    # response and NULL on all 43,668 persisted rows. Requiring it would refuse
    # every real measurement while looking rigorous.
    row = {"kd_nm": 86.197, "scorer_version": "v10_2_unified_parallel_2026_05_08"}
    assert "calibration_head" not in row
    assert kd_provenance_ok(row)[0] is True


def test_the_v9_path_is_refused_by_name():
    ok, why = kd_provenance_ok({"kd_nm": 0.4, "scorer": "compute_deltaforge_energy"})
    assert ok is False
    assert "v10.2" in why


def test_a_row_with_no_scorer_at_all_is_refused():
    assert kd_provenance_ok({"kd_nm": 12.0})[0] is False


def test_a_platform_withheld_quote_is_refused_with_the_servers_own_code():
    ok, why = kd_provenance_ok({
        "kd_nm": 5.0, "scorer_version": "v10_2_unified_parallel_2026_05_08",
        "affinity_quotable": False, "affinity_reason_code": "interface_type_tcr_pmhc",
    })
    assert ok is False
    assert "interface_type_tcr_pmhc" in why
