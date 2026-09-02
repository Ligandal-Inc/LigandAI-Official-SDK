# Copyright © 2026 Ligandal, Inc. All rights reserved.
"""The eight defect guards, each pinned to the real failure it prevents.

Every number in this file comes from the Avammune/ENPP3 campaign of 2026-09-02.
Nothing here is synthetic in the sense that matters: the SHAPES are the shapes
that actually occurred, and the expected outputs are the values that were
actually shipped to the customer.
"""

from __future__ import annotations

import math

import pytest

from ligandai.campaign import (
    ConstantTermError, Correlation, Direction, DirectionEvidence,
    DirectionInvertedError, GateSpec, GateStatus, Measured, OptimisticSplitError,
    Outcome, Population, Scorer, SplitEvidence, SplitType, Status,
    UnvalidatedScorerError, VacuousGateError, WrongPopulationError, all_of,
    apply_gate, apply_gates, assert_population, composite_score, count_at_least,
    rank, runs_needed_for_upper_bound, wilson,
)
from ligandai.campaign.gates import Population as Pop


# ── D1: a gate that removes nothing (or almost nothing) is not a gate ────────

def test_d1_gate_removing_one_of_2997_is_refused():
    """complex pLDDT >= 88 excluded 1 design of 2,997 and read as a quality bar."""
    rows = {f"d{i}": {"complex_plddt": 87.5 if i == 0 else 88.0 + (i % 900) * 0.005}
            for i in range(2997)}
    spec = GateSpec("complex_plddt", ">=", 88.0, label="complex_plddt>=88")
    with pytest.raises(VacuousGateError) as exc:
        apply_gate(rows, spec)
    assert "DECORATIVE" in str(exc.value)
    assert "1 of 2997" in str(exc.value)
    # and the observed range is reported, always
    keep, report = apply_gate(rows, spec, strict=False)
    assert report.status is GateStatus.DECORATIVE
    assert report.observed_min == pytest.approx(87.5)
    assert report.n_removed == 1


def test_d1_truly_vacuous_gate_is_refused_and_reports_range():
    rows = {f"d{i}": {"iptm": 0.9} for i in range(500)}
    keep, report = apply_gate(rows, GateSpec("iptm", ">=", 0.5), strict=False)
    assert report.status is GateStatus.VACUOUS
    assert report.observed_min == report.observed_max == 0.9
    assert len(keep) == 500


def test_d1_vacuous_allowed_only_with_a_written_reason():
    with pytest.raises(ValueError, match="allow_vacuous_reason"):
        GateSpec("iptm", ">=", 0.5, allow_vacuous=True)
    ok = GateSpec("iptm", ">=", 0.5, allow_vacuous=True,
                  allow_vacuous_reason="reported descriptively, not a bar")
    rows = {f"d{i}": {"iptm": 0.9} for i in range(10)}
    keep, report = apply_gate(rows, ok)          # strict, but permitted
    assert report.status is GateStatus.VACUOUS
    assert len(keep) == 10


def test_d1_unmeasurable_gate_is_refused():
    rows = {f"d{i}": {"other": 1.0} for i in range(10)}
    with pytest.raises(VacuousGateError, match="no row carried metric"):
        apply_gate(rows, GateSpec("peptide_ipsae_median", ">=", 0.67))


# ── D2: an ordering must be computed on the population it exists to order ────

def test_d2_ranking_rejected_designs_raises():
    """DeltaForge ran on 679 designs the interface gate had REJECTED."""
    rows = {f"d{i}": {"ipsae": i / 3000.0} for i in range(3000)}
    panel = apply_gates(rows, [GateSpec("ipsae", ">=", 0.5, label="interface")])
    passing = panel.population
    rejected = [d for d in rows if d not in passing][:679]
    with pytest.raises(WrongPopulationError) as exc:
        assert_population(rejected, passing, operation="DeltaForge ranking")
    assert "NOT in population" in str(exc.value)


def test_d2_skipping_passing_designs_raises():
    """...while skipping 415 designs that passed."""
    pop = Pop("gate_passing", frozenset(f"d{i}" for i in range(1000)))
    partial = [f"d{i}" for i in range(585)]
    with pytest.raises(WrongPopulationError, match="were never\n *scored|never"):
        assert_population(partial, pop, operation="DeltaForge ranking")
    cov = assert_population(partial, pop, operation="x", require_full_coverage=False)
    assert cov == pytest.approx(0.585)


def test_d2_population_carries_its_provenance():
    rows = {f"d{i}": {"a": i, "b": 1000 - i} for i in range(100)}
    panel = apply_gates(rows, [GateSpec("a", ">=", 10), GateSpec("b", ">=", 950)])
    assert panel.population.parent == "cohort"
    assert len(panel.population.provenance) == 2
    assert "active" in panel.population.provenance[0]


# ── D3: a term constant across the cohort cannot enter a composite ───────────

def _validated(name, direction=Direction.HIGHER_IS_BETTER, **kw):
    return Scorer(name, direction, field_name=name,
                  direction_evidence=DirectionEvidence(
                      "measured_reference", Correlation(0.4, 60, 0.15, 0.60)), **kw)


def test_d3_n_end_rule_constant_across_851_macrocycles_is_refused():
    rows = {f"d{i}": {"n_end_rule": 1.0, "charge": i * 0.01} for i in range(851)}
    with pytest.raises(ConstantTermError) as exc:
        composite_score(rows, [_validated("n_end_rule"), _validated("charge")])
    assert "CONSTANT across the cohort" in str(exc.value)
    assert "851 measured rows" in str(exc.value)


def test_d3_inapplicable_term_contributes_not_applicable_not_a_constant():
    sc = Scorer("n_end_rule", Direction.HIGHER_IS_BETTER, field_name="n_end_rule",
                applies_to=frozenset({"linear"}),
                direction_evidence=DirectionEvidence(
                    "measured_reference", Correlation(0.4, 60, 0.15, 0.60)))
    m = sc.value_for({"n_end_rule": 1.0}, molecule_class="cyclic")
    assert m.status is Status.NOT_APPLICABLE
    assert m.value is None                        # never a filled-in constant
    assert float(Measured(0.0, Status.MEASURED)) == 0.0   # zero is still zero


def test_d3_composite_reports_coverage_when_a_term_is_absent():
    rows = {"a": {"x": 1.0, "y": 2.0}, "b": {"x": 2.0}, "c": {"x": 3.0, "y": 9.0}}
    res = composite_score(rows, [_validated("x"), _validated("y")],
                          min_term_coverage=0.5)
    assert res.coverage["b"] == 0.5              # one of two admitted terms
    assert res.scores["b"].denominator == 2
    assert res.scores["b"].n == 1


# ── D4: a metric that orders backwards ──────────────────────────────────────

def test_d4_halflife_scorer_with_negative_spearman_is_rejected_at_construction():
    with pytest.raises(DirectionInvertedError) as exc:
        Scorer("halflife_score", Direction.HIGHER_IS_BETTER, field_name="hl",
               direction_evidence=DirectionEvidence(
                   "measured_serum_halflife", Correlation(-0.41, 120, -0.55, -0.24),
                   strata=("linear", "cyclic")))
    assert "orders BACKWARDS" in str(exc.value)


def test_d4_unvalidated_scorer_cannot_silently_enter_a_composite():
    rows = {f"d{i}": {"z": i * 1.0, "w": (i % 7) * 1.0} for i in range(50)}
    with pytest.raises(UnvalidatedScorerError, match="UNVALIDATED"):
        composite_score(rows, [Scorer("z", Direction.HIGHER_IS_BETTER, field_name="z"),
                               _validated("w")])
    # allowed, but the audit says so
    res = composite_score(rows, [Scorer("z", Direction.HIGHER_IS_BETTER, field_name="z")],
                          require_validated=False)
    assert res.admitted_terms == ("z",)


# ── D5: a learned scorer reported on a random split ─────────────────────────

def test_d5_random_split_is_not_performance():
    rows = {f"d{i}": {"m": i * 1.0} for i in range(60)}
    learned = Scorer("m", Direction.HIGHER_IS_BETTER, field_name="m", is_learned=True,
                     direction_evidence=DirectionEvidence(
                         "measured", Correlation(0.5, 60, 0.3, 0.7)),
                     split_evidence=SplitEvidence(SplitType.RANDOM, "r2", 0.92, 300))
    with pytest.raises(OptimisticSplitError) as exc:
        composite_score(rows, [learned])
    assert "R^2 0.92 random and -1.95" in str(exc.value)
    learned.split_evidence = SplitEvidence(SplitType.CLUSTERED, "r2", -1.95, 300,
                                           cluster_identity_threshold=0.7)
    assert composite_score(rows, [learned]).admitted_terms == ("m",)


# ── D6: every ranking ships a length baseline ───────────────────────────────

def test_d6_ranking_always_carries_its_length_baseline():
    n = 200
    pop = Pop("gate_passing", frozenset(f"d{i}" for i in range(n)))
    # score anti-correlated with length, as measured: rho about -0.37
    lengths = {f"d{i}": 12 + (i % 13) for i in range(n)}
    scores = {f"d{i}": Measured(1.0 - lengths[f"d{i}"] * 0.03 + (i % 5) * 0.004,
                                Status.MEASURED) for i in range(n)}
    res = rank("iface", pop, scores, lengths)
    assert res.length_baseline is not None
    assert res.length_baseline.score_vs_length.rho < -0.3
    assert res.length_confounded is True
    assert res.stratified_ranks, "a confounded ranking must ship length strata"
    assert any("ordering of lengths" in n_ for n_ in res.notes)


def test_d6_uncorrelated_ranking_is_not_flagged():
    pop = Pop("p", frozenset(f"d{i}" for i in range(60)))
    lengths = {f"d{i}": 12 + (i % 4) for i in range(60)}
    scores = {f"d{i}": Measured(((i * 37) % 61) / 61.0, Status.MEASURED)
              for i in range(60)}
    res = rank("x", pop, scores, lengths)
    assert res.length_confounded is False


# ── D7: identity is a hash of the structure, never a name ───────────────────

def test_d7_cross_receptor_pooling_is_refused(tmp_path):
    from ligandai.campaign import assert_single_receptor, identify_pose

    def write(path, rec_seq, pep_seq, rec_chain="A", pep_chain="B"):
        aa = {"A": "ALA", "G": "GLY", "S": "SER", "V": "VAL", "L": "LEU"}
        lines, n = [], 0
        for ch, seq in ((rec_chain, rec_seq), (pep_chain, pep_seq)):
            for i, c in enumerate(seq, start=1):
                n += 1
                lines.append(
                    f"ATOM  {n:5d}  CA  {aa[c]} {ch}{i:4d}    "
                    f"{i:8.3f}{0.0:8.3f}{0.0:8.3f}  1.00 50.00           C")
        path.write_text("\n".join(lines) + "\nEND\n")
        return str(path)

    a = write(tmp_path / "arm_ENPP2_0001.pdb", "AGSVLAGSVL" * 4, "AGSVLAGS")
    b = write(tmp_path / "arm_ENPP2_0002.pdb", "LVSGALVSGA" * 4, "AGSVLAGS")
    ids = [identify_pose(a), identify_pose(b)]
    # the two files sit in the same arm and are named alike; their receptors differ
    assert ids[0].receptor_sha256 != ids[1].receptor_sha256
    with pytest.raises(Exception) as exc:
        assert_single_receptor(ids, context="arm_ENPP2")
    assert "2 distinct receptors" in str(exc.value)
    assert "not identity" in str(exc.value)


def test_d7_declared_receptor_match_is_trusted_and_fallback_is_not(tmp_path):
    from ligandai.campaign import identify_pose

    rec = "AGSVLAGSVL" * 4
    aa = {"A": "ALA", "G": "GLY", "S": "SER", "V": "VAL", "L": "LEU"}
    lines, n = [], 0
    for ch, seq in (("A", rec), ("B", "AGSVLAGS")):
        for i, c in enumerate(seq, start=1):
            n += 1
            lines.append(f"ATOM  {n:5d}  CA  {aa[c]} {ch}{i:4d}    "
                         f"{i:8.3f}{0.0:8.3f}{0.0:8.3f}  1.00 50.00           C")
    p = tmp_path / "x.pdb"
    p.write_text("\n".join(lines) + "\nEND\n")
    matched = identify_pose(str(p), declared_receptors={"TARGET": rec})
    assert matched.trusted_assignment is True
    assert matched.peptide_sequence == "AGSVLAGS"
    guessed = identify_pose(str(p))
    assert guessed.trusted_assignment is False
    assert guessed.assignment_method == "length_fallback"
    wrong = identify_pose(str(p), declared_receptors={"OTHER": "AAAAAAAAAA"})
    assert any("NO parsed chain matches" in w for w in wrong.warnings)


# ── D8: an aggregate over zero rows is INDETERMINATE, never PASS ────────────

def test_d8_all_over_zero_rows_does_not_pass():
    assert all([]) is True                                    # what Python does
    v = all_of([], lambda r: True)
    assert v.outcome is Outcome.INDETERMINATE
    assert bool(v) is False
    assert v.n_rows == 0
    assert "would have returned True" in v.detail


def test_d8_every_verdict_carries_its_row_count():
    rows = [{"ok": True}, {"ok": False}, {"ok": None}]
    v = all_of(rows, lambda r: r["ok"])
    assert v.n_rows == 2 and v.n_missing == 1 and v.n_eligible == 3
    c = count_at_least(rows, lambda r: r["ok"], 1)
    assert c.outcome is Outcome.PASS and c.n_true == 1


# ── The certification arithmetic, against the numbers actually shipped ──────

@pytest.mark.parametrize("n,upper", [
    (8, 0.32), (16, 0.19), (20, 0.16), (24, 0.14), (32, 0.11), (40, 0.09),
])
def test_wilson_reproduces_the_published_zero_failure_table(n, upper):
    """The clean-pose table in RELIABILITY_IS_A_RATE_NOT_A_LABEL.md."""
    iv = wilson(0, n)
    assert iv is not None
    assert round(iv.upper, 2) == upper


@pytest.mark.parametrize("k,n,lo,hi", [
    (5, 16, 0.14, 0.56),     # CLSQHPNMSRLPRFELEQKLNC
    (4, 16, 0.10, 0.49),     # CTPSIEKKDFMKNDTIIKKC
    (5, 20, 0.11, 0.47),     # CKEQPALERAPTKC
    (3, 16, 0.07, 0.43),     # CQREDALTIPVHLVANKC
    (3, 20, 0.05, 0.36),     # CETLHTWTELEAADKC
    (2, 16, 0.03, 0.36),     # CLKKQTRIKNELPPLQETPC at n=16
    (1, 16, 0.01, 0.28),     # CTVRPELKSKIC
])
def test_wilson_reproduces_the_published_per_peptide_intervals(k, n, lo, hi):
    """Every interval in the campaign's own per-peptide table, to 2 dp."""
    iv = wilson(k, n)
    assert iv is not None
    assert (round(iv.lower, 2), round(iv.upper, 2)) == (lo, hi)


def test_wilson_reproduces_the_certified_headline():
    """CLKKQTRIKNELPPLQETPC certified at 19/192: rate 9.9%, bound 0.149."""
    iv = wilson(19, 192)
    assert iv.point == pytest.approx(0.0990, abs=5e-4)
    assert iv.upper == pytest.approx(0.1494, abs=5e-4)
    assert iv.upper <= 0.15          # it certified, by 0.0006


def test_zero_runs_has_no_interval_rather_than_a_wide_one():
    assert wilson(0, 0) is None


def test_runs_needed_is_computed_not_asserted():
    """'at 10.3% the bound converges toward 0.16 and never crosses 0.15' was
    stated three times and was false. Compute it."""
    need = runs_needed_for_upper_bound(0.15, 19 / 192)
    assert need is not None and need > 192
    # the doc's own claims: 22 clean poses to certify below 15%, 35 below 10%
    assert runs_needed_for_upper_bound(0.15, 0.0) == 22
    assert runs_needed_for_upper_bound(0.10, 0.0) == 35
    assert runs_needed_for_upper_bound(0.15, 0.30) is None    # genuinely unreachable
