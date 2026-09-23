# Copyright © 2026 Ligandal, Inc. All rights reserved.
"""Unit tests for MULTI-TARGET contrastive co-optimization. bd-dre-if562.

Co-optimization means N POSITIVE targets at once — human BRD4 *and* mouse Brd4
(one gene, two species), or two genes entirely — while still steering away from
off-target references.

These tests cover the capability and, more importantly, the six ways it can be
silently wrong:

1. an aggregation rule that is never stated (min vs mean is a different claim);
2. two orthologs collapsing onto ONE panel key and reporting one measurement
   as two;
3. a non-default organism with no structure of its own, which folds the human
   receptor and calls it mouse;
4. one target's hotspot residues reused on another's numbering frame;
5. a positive target sitting in another positive's CFG reference panel, which
   steers generation away from a receptor being co-optimized FOR;
6. a partial mean reported as a bound when a mean over a subset is not one.

No network: the transport, the fold submitter and the metric surface are all
stubbed, so the whole loop runs offline and burns nothing.
"""

from __future__ import annotations

import pytest

from ligandai.contrastive import (
    ContrastiveGuidance,
    ContrastiveReference,
    PocketFeatureMatrix,
    V6_9_POCKET_FEATURE_DIM,
)
from ligandai.errors import LigandAIValidationError
from ligandai.multitarget import (
    CoOptimization,
    TargetSpec,
    aggregate_across_targets,
    coerce_target,
)
from ligandai.resources.campaigns import (
    Campaigns,
    MultiTargetDelta,
    MultiTargetPartial,
    PanelLeg,
    PanelResult,
    SelectivityDelta,
)

A = "ACDEFGHIKLMNP"
B = "WYVTSRQNKHGFE"
C = "LLLLIIIVVVAAA"

MOUSE_PDB = "ATOM      1  N   MET A   1      11.104   6.134  -6.504  1.00 00.00           N\n"


def _human() -> TargetSpec:
    return TargetSpec(gene="BRD4", organism="human")


def _mouse() -> TargetSpec:
    return TargetSpec(
        gene="BRD4",
        organism="mouse",
        uniprot_id="Q9ESU6",
        receptor_pdb=MOUSE_PDB,
        receptor_name="BRD4_MOUSE",
    )


def _mouse_own_symbol() -> TargetSpec:
    """The ortholog under its OWN gene symbol — the supported generation route.

    A generation session cannot be persisted with two positives under one gene
    symbol (the parentage layer keys its variant maps by gene), so this is what
    a real cross-species co-optimization looks like end to end.
    """
    return TargetSpec(
        gene="BRD4_MOUSE",
        organism="mouse",
        uniprot_id="Q9ESU6",
        receptor_pdb=MOUSE_PDB,
        receptor_name="BRD4_MOUSE",
    )


def _rows(dim: int = V6_9_POCKET_FEATURE_DIM, n: int = 6, fill: float = 0.25):
    return [[fill + 0.001 * i + 0.0001 * j for j in range(dim)] for i in range(n)]


def _ref(gene: str) -> ContrastiveReference:
    return ContrastiveReference(gene=gene, features=PocketFeatureMatrix(rows=_rows()))


def _panel(values, *, targets, off_targets, aggregate, draws=1, specs=None):
    """A PanelResult built straight from per-leg ipSAE values."""
    legs: dict[tuple[str, str], PanelLeg] = {}
    for (seq, recep), draws_list in values.items():
        legs[(seq, recep)] = PanelLeg(
            sequence=seq,
            receptor=recep,
            peptide_ipsae_draws=list(draws_list),
            scaffold_mediated_draws=[False] * len(draws_list),
        )
    return PanelResult(
        targets=targets,
        off_targets=off_targets,
        legs=legs,
        requested_draws=draws,
        statistic="median",
        aggregate=aggregate,
        specs=specs or {},
    )


# ─────────────────────────────────────────────────────────────────────────────
# 1. The aggregation rule is explicit, selectable, and always reported
# ─────────────────────────────────────────────────────────────────────────────


def test_worst_case_is_the_min_and_names_the_limiting_target():
    v, limiting = aggregate_across_targets({"BRD4@human": 0.90, "BRD4@mouse": 0.62}, "worst_case")
    assert v == pytest.approx(0.62)
    assert limiting == "BRD4@mouse"


def test_mean_averages_and_still_names_the_leg_the_average_hid():
    v, limiting = aggregate_across_targets({"BRD4@human": 0.90, "BRD4@mouse": 0.62}, "mean")
    assert v == pytest.approx(0.76)
    # ⛔ Under 'mean' the limiting leg is the half the average hides — and it is
    # exactly the one a caller has to act on.
    assert limiting == "BRD4@mouse"


def test_an_empty_score_set_is_not_a_zero():
    with pytest.raises(LigandAIValidationError, match="not a score of zero"):
        aggregate_across_targets({}, "worst_case")


def test_unknown_aggregate_is_refused_by_name():
    with pytest.raises(LigandAIValidationError, match="worst_case"):
        aggregate_across_targets({"X": 0.5}, "median")  # type: ignore[arg-type]


def test_cooptimization_requires_the_rule_to_be_named():
    with pytest.raises(TypeError):
        CoOptimization([_human(), _mouse()])  # type: ignore[call-arg]


def test_multi_target_panel_without_a_rule_is_refused():
    with pytest.raises(LigandAIValidationError, match="no default"):
        PanelResult(
            targets=["BRD4@human", "BRD4@mouse"],
            off_targets=["BRD2"],
            legs={},
            requested_draws=1,
        )


def test_single_target_panel_needs_no_rule_and_reports_single():
    p = _panel(
        {(A, "BRD4"): [0.90], (A, "BRD2"): [0.30]},
        targets=["BRD4"], off_targets=["BRD2"], aggregate=None,
    )
    complete, _partial, summary = p.deltas(band=0.10, target_leg_min=0.66)
    assert summary["positive_aggregate"] == "single"
    assert isinstance(complete[0], SelectivityDelta)
    assert complete[0].delta == pytest.approx(0.60)


def test_the_rule_travels_with_the_number_on_every_row_and_in_the_summary():
    p = _panel(
        {
            (A, "BRD4@human"): [0.90], (A, "BRD4@mouse"): [0.70],
            (A, "BRD2"): [0.30],
        },
        targets=["BRD4@human", "BRD4@mouse"], off_targets=["BRD2"],
        aggregate="worst_case",
    )
    complete, _partial, summary = p.deltas(band=0.10, target_leg_min=0.66)
    row = complete[0]
    assert isinstance(row, MultiTargetDelta)
    assert row.aggregate_rule == "worst_case"
    assert row.aggregate_ipsae == pytest.approx(0.70)
    assert row.limiting_target == "BRD4@mouse"
    assert row.per_target_ipsae == {"BRD4@human": 0.90, "BRD4@mouse": 0.70}
    assert row.delta == pytest.approx(0.40)
    assert summary["positive_aggregate"] == "worst_case"
    assert summary["targets"] == ["BRD4@human", "BRD4@mouse"]
    assert summary["limiting_target_counts"] == {"BRD4@mouse": 1}


def test_worst_case_and_mean_give_different_verdicts_on_the_same_legs():
    legs = {
        (A, "BRD4@human"): [0.95], (A, "BRD4@mouse"): [0.50],
        (A, "BRD2"): [0.20],
    }
    worst = _panel(legs, targets=["BRD4@human", "BRD4@mouse"],
                   off_targets=["BRD2"], aggregate="worst_case")
    mean = _panel(legs, targets=["BRD4@human", "BRD4@mouse"],
                  off_targets=["BRD2"], aggregate="mean")

    w_rows, _p, _s = worst.deltas(band=0.10, target_leg_min=0.66)
    m_rows, _p, _s = mean.deltas(band=0.10, target_leg_min=0.66)

    # worst_case: the mouse leg at 0.50 fails the floor, so the row does.
    assert w_rows[0].aggregate_ipsae == pytest.approx(0.50)
    assert w_rows[0].target_leg_pass is False
    assert w_rows[0].verdict == "DELTA_ONLY"

    # mean: 0.725 clears the floor and the row reads PASS_BOTH — while one of
    # the two required targets was never bound. per_target_pass says so.
    assert m_rows[0].aggregate_ipsae == pytest.approx(0.725)
    assert m_rows[0].target_leg_pass is True
    assert m_rows[0].verdict == "PASS_BOTH"
    assert m_rows[0].per_target_pass == {"BRD4@human": True, "BRD4@mouse": False}
    assert m_rows[0].all_targets_pass is False


def test_survivors_default_refuses_an_aggregate_pass_that_misses_a_target():
    p = _panel(
        {
            (A, "BRD4@human"): [0.95], (A, "BRD4@mouse"): [0.50], (A, "BRD2"): [0.20],
            (B, "BRD4@human"): [0.90], (B, "BRD4@mouse"): [0.88], (B, "BRD2"): [0.20],
        },
        targets=["BRD4@human", "BRD4@mouse"], off_targets=["BRD2"], aggregate="mean",
    )
    # A passes on the mean while failing the mouse leg — excluded by default.
    assert p.survivors(band=0.10, target_leg_min=0.66) == [B]
    # Opting out is possible, and it changes the answer, which is the point.
    assert set(p.survivors(band=0.10, target_leg_min=0.66, require_all_targets=False)) == {A, B}


def test_summary_states_what_the_floor_was_applied_to():
    worst = _panel({(A, "T1"): [0.9], (A, "T2"): [0.8], (A, "O"): [0.1]},
                   targets=["T1", "T2"], off_targets=["O"], aggregate="worst_case")
    mean = _panel({(A, "T1"): [0.9], (A, "T2"): [0.8], (A, "O"): [0.1]},
                  targets=["T1", "T2"], off_targets=["O"], aggregate="mean")
    _c, _p, s_worst = worst.deltas(band=0.1, target_leg_min=0.66)
    _c, _p, s_mean = mean.deltas(band=0.1, target_leg_min=0.66)
    assert s_worst["target_leg_min_applied_to"] == "every positive target"
    assert s_mean["target_leg_min_applied_to"] == "the mean across positive targets"


# ─────────────────────────────────────────────────────────────────────────────
# 2. Orthologs get distinct keys — one measurement is never reported as two
# ─────────────────────────────────────────────────────────────────────────────


def test_orthologs_of_one_gene_get_distinct_panel_keys():
    assert _human().key == "BRD4@human"
    assert _mouse().key == "BRD4@mouse"
    co = CoOptimization([_human(), _mouse()], aggregate="worst_case")
    assert co.keys == ["BRD4@human", "BRD4@mouse"]
    assert co.is_cross_species is True


def test_two_targets_sharing_a_key_are_refused():
    with pytest.raises(LigandAIValidationError, match="share the panel key"):
        CoOptimization([TargetSpec(gene="BRD4"), TargetSpec(gene="BRD4")],
                       aggregate="worst_case")


def test_a_label_disambiguates_two_constructs_of_one_gene():
    a = TargetSpec(gene="BRD4", label="BRD4-BD1", pdb_code="3MXF")
    b = TargetSpec(gene="BRD4", label="BRD4-ET", pdb_code="2NCZ")
    co = CoOptimization([a, b], aggregate="worst_case")
    assert co.keys == ["BRD4-BD1", "BRD4-ET"]
    assert co.is_cross_species is False


def test_panel_result_refuses_duplicate_positive_keys():
    with pytest.raises(LigandAIValidationError, match="duplicate positive target key"):
        PanelResult(targets=["BRD4", "BRD4"], off_targets=["BRD2"],
                    legs={}, requested_draws=1, aggregate="worst_case")


# ─────────────────────────────────────────────────────────────────────────────
# 3. A non-default organism without its own structure is a fabrication
# ─────────────────────────────────────────────────────────────────────────────


def test_mouse_target_without_a_structure_is_refused():
    with pytest.raises(LigandAIValidationError) as exc:
        TargetSpec(gene="BRD4", organism="mouse")
    msg = str(exc.value)
    assert "no structure of its own" in msg
    # The error has to say WHY, not just no: target_gene resolves the canonical
    # receptor, so this leg would fold the human protein under a mouse label.
    assert "CANONICAL (human) receptor" in msg
    assert "list_species" in msg


def test_mouse_target_with_coordinates_is_accepted_and_folds_through_them():
    m = _mouse()
    assert m.has_explicit_structure is True
    assert m.structure_route == "receptor_pdb"
    # ⛔ fold_batch has no species kwarg. The organism is honoured by sending
    # the ortholog's own coordinates, and nothing else.
    kw = m.fold_kwargs()
    assert kw == {"receptor_pdb": MOUSE_PDB, "receptor_name": "BRD4_MOUSE"}
    assert "organism" not in kw and "species" not in kw and "target_gene" not in kw


def test_a_human_target_still_folds_by_gene_symbol():
    assert _human().fold_kwargs() == {"target_gene": "BRD4"}


def test_an_uploaded_construct_is_reachable_by_user_pdb_id():
    t = TargetSpec(gene="BRD4", label="BRD4-ET", user_pdb_id=4212, organism="human")
    assert t.structure_route == "user_pdb_id"
    assert t.generate_kwargs()["user_pdb_id"] == 4212


def test_every_target_states_its_numbering_frame():
    assert "mouse" in _mouse().frame and "Q9ESU6" in _mouse().frame
    assert "pdb numbering" in _mouse().frame
    assert "receptor_pdb" in _mouse().frame


# ─────────────────────────────────────────────────────────────────────────────
# 4. Residue frames never cross
# ─────────────────────────────────────────────────────────────────────────────


def test_hotspots_on_one_target_and_not_the_other_are_refused():
    h = TargetSpec(gene="BRD4", organism="human",
                   hotspot_residues=[146, 140], hotspot_frame_attested=True)
    with pytest.raises(LigandAIValidationError, match="resolve them against a different structure"):
        CoOptimization([h, _mouse()], aggregate="worst_case")


def test_multi_target_hotspots_require_a_per_target_frame_attestation():
    h = TargetSpec(gene="BRD4", organism="human", hotspot_residues=[146])
    m = TargetSpec(
        gene="BRD4", organism="mouse", receptor_pdb=MOUSE_PDB,
        receptor_name="BRD4_MOUSE", hotspot_residues=[143],
    )
    with pytest.raises(LigandAIValidationError, match="hotspot_frame_attested"):
        CoOptimization([h, m], aggregate="worst_case")


def test_attested_per_target_hotspots_are_accepted_and_stay_separate():
    h = TargetSpec(gene="BRD4", organism="human",
                   hotspot_residues=[146, 140], hotspot_frame_attested=True)
    m = TargetSpec(gene="BRD4", organism="mouse", receptor_pdb=MOUSE_PDB,
                   receptor_name="BRD4_MOUSE",
                   hotspot_residues=[143, 137], hotspot_frame_attested=True)
    co = CoOptimization([h, m], aggregate="worst_case")
    wire = co.to_wire()
    # ⛔ Residue lists are emitted PER TARGET, never hoisted to a shared level
    # where a second structure would resolve them.
    assert wire["targets"][0]["hotspotResidues"] == [146, 140]
    assert wire["targets"][1]["hotspotResidues"] == [143, 137]
    assert all(t["hotspotFrameAttested"] is True for t in wire["targets"])
    assert wire["coOptimization"]["frames"]["BRD4@mouse"].startswith("BRD4/mouse")


def test_a_single_target_needs_no_attestation():
    # Nothing to cross frames WITH, so the attestation would be ceremony.
    t = TargetSpec(gene="BRD4", hotspot_residues=[146])
    CoOptimization([t], aggregate="worst_case")


def test_zero_based_residue_indices_are_refused():
    with pytest.raises(LigandAIValidationError, match="not a PDB residue number"):
        TargetSpec(gene="BRD4", hotspot_residues=[0, 12])


# ─────────────────────────────────────────────────────────────────────────────
# 5. A positive target is never a contrastive reference
# ─────────────────────────────────────────────────────────────────────────────


def test_a_positive_target_in_the_off_target_panel_is_refused():
    co = CoOptimization([_human(), _mouse()], aggregate="worst_case")
    with pytest.raises(LigandAIValidationError, match="both sides of the panel"):
        co.check_reference_roles(["BRD2", "BRD4"])


def test_the_refusal_explains_the_sign_of_the_guidance():
    co = CoOptimization([TargetSpec(gene="ENPP3")], aggregate="worst_case")
    with pytest.raises(LigandAIValidationError) as exc:
        co.check_reference_roles(["ENPP1", "ENPP3"])
    msg = str(exc.value)
    assert "steer" in msg.lower()
    assert "co-optimizing FOR" in msg or "co-optimizing for" in msg.lower()


def test_species_discriminating_design_is_still_expressible():
    # Bind human, AVOID mouse: mouse is an off-target and must not also be a
    # positive. That combination is legal and is the opposite request.
    co = CoOptimization([_human()], aggregate="worst_case")
    co.check_reference_roles(["BRD2"])  # fine
    with pytest.raises(LigandAIValidationError):
        CoOptimization([_human(), _mouse()], aggregate="worst_case").check_reference_roles(
            [TargetSpec(gene="BRD4", organism="mouse", receptor_pdb=MOUSE_PDB)]
        )


# ─────────────────────────────────────────────────────────────────────────────
# 6. Partial rows: a bound under worst_case, NOT a bound under mean
# ─────────────────────────────────────────────────────────────────────────────


def test_a_missing_positive_leg_is_an_upper_bound_under_worst_case():
    p = _panel(
        {(A, "T1"): [0.90], (A, "O1"): [0.20]},  # T2 never measured
        targets=["T1", "T2"], off_targets=["O1"], aggregate="worst_case",
    )
    complete, partial, summary = p.deltas(band=0.10, target_leg_min=0.66)
    assert complete == []
    assert len(partial) == 1
    row = partial[0]
    assert isinstance(row, MultiTargetPartial)
    assert row.targets_missing == ("T2",)
    assert row.bound_kind == "upper_bound"
    # min over {T1} is >= min over {T1,T2}, so Δ can only fall.
    assert row.delta_upper_bound == pytest.approx(0.70)
    assert summary["n_partial_indeterminate"] == 0


def test_a_missing_positive_leg_is_NOT_a_bound_under_mean():
    p = _panel(
        {(A, "T1"): [0.90], (A, "O1"): [0.20]},
        targets=["T1", "T2"], off_targets=["O1"], aggregate="mean",
    )
    _complete, partial, summary = p.deltas(band=0.10, target_leg_min=0.66)
    row = partial[0]
    # ⛔ A mean over a subset moves either way when the missing leg arrives.
    # Emitting a number here would be the partial-panel defect under a new rule.
    assert row.bound_kind == "indeterminate"
    assert row.delta_upper_bound is None
    assert summary["n_partial_indeterminate"] == 1


def test_a_missing_off_target_is_still_an_upper_bound_under_worst_case():
    p = _panel(
        {(A, "T1"): [0.90], (A, "T2"): [0.80], (A, "O1"): [0.20]},  # O2 missing
        targets=["T1", "T2"], off_targets=["O1", "O2"], aggregate="worst_case",
    )
    _c, partial, _s = p.deltas(band=0.10, target_leg_min=0.66)
    assert partial[0].comparators_missing == ("O2",)
    assert partial[0].bound_kind == "upper_bound"
    assert partial[0].delta_upper_bound == pytest.approx(0.60)


def test_no_positive_leg_at_all_produces_no_row_of_any_kind():
    p = _panel(
        {(A, "O1"): [0.20]},
        targets=["T1", "T2"], off_targets=["O1"], aggregate="worst_case",
    )
    complete, partial, summary = p.deltas(band=0.10, target_leg_min=0.66)
    assert complete == [] and partial == []
    assert summary["n_no_target_leg"] == 1


# ─────────────────────────────────────────────────────────────────────────────
# 7. fold_panel folds every design against every positive AND every off-target
# ─────────────────────────────────────────────────────────────────────────────


class _FakeBatch:
    def __init__(self, receptor, seqs, draw):
        self.receptor = receptor
        self.jobs = [
            {"job_id": f"{receptor}|{draw}|{i}", "peptide_index": i, "sequence": s}
            for i, s in enumerate(seqs)
        ]

    def wait(self, **_kw):
        return []


class _FakePeptides:
    # ⛔ Keyed by the WIRE identity fold_batch actually receives, which is NOT
    # the panel key: the human leg goes out as target_gene="BRD4" and the mouse
    # leg as receptor_name="BRD4_MOUSE", while the panel keys them "BRD4@human"
    # and "BRD4@mouse". The panel has to bridge those two namespaces correctly
    # or a two-species result lands on one leg — so the stub deliberately does
    # not share the panel's vocabulary.
    VALUES = {
        "BRD4": 0.90, "BRD4_MOUSE": 0.72, "BRD2": 0.25, "BRD3": 0.20,
    }

    def __init__(self):
        self.calls: list[dict] = []
        self.generate_calls: list[dict] = []

    def fold_batch(self, *, peptides, **kw):
        key = (
            kw.get("target_gene")
            or kw.get("receptor_name")
            or "unnamed"
        )
        self.calls.append({"receptor": key, "n": len(peptides), **kw})
        draw = sum(1 for c in self.calls if c["receptor"] == key) - 1
        return _FakeBatch(key, peptides, draw)

    def generate(self, **kw):
        self.generate_calls.append(kw)
        n = int(kw.get("num_peptides") or 1)
        pool = [A, B, C]
        seqs = [pool[i % len(pool)] for i in range(n)]

        class _Job:
            def wait(_self):
                class _R:
                    peptides = [{"sequence": s} for s in seqs]
                return _R()
        return _Job()


class _FakeEvaluator:
    def filter_folds(self, ids, criteria=None):
        rows = []
        for jid in ids:
            recep = jid.split("|")[0]
            rows.append({
                "fold_job_id": jid,
                "quality_tier": {"verdict": "great_plus"},
                "metrics": {
                    "peptide_ipsae": _FakePeptides.VALUES[recep],
                    "iptm": 0.8,
                    "plddt_mean": 88.0,
                    "scaffold_mediated": False,
                },
            })
        return {"results": rows}


class _FakeClient:
    def __init__(self):
        self.peptides = _FakePeptides()
        self.evaluator = _FakeEvaluator()


def test_fold_panel_submits_every_positive_and_every_off_target():
    client = _FakeClient()
    camp = Campaigns(transport=None, client=client)  # type: ignore[arg-type]
    panel = camp.fold_panel(
        [A, B],
        targets=[_human(), _mouse()],
        aggregate="worst_case",
        off_targets=["BRD2", "BRD3"],
        replicates=2,
    )
    # (2 positives + 2 off-targets) x 2 replicates
    assert len(client.peptides.calls) == 8
    receptors = {c["receptor"] for c in client.peptides.calls}
    assert receptors == {"BRD4", "BRD4_MOUSE", "BRD2", "BRD3"}
    # ...and the panel keys them apart from the wire identities.
    assert {r for (_s, r) in panel.legs} == {
        "BRD4@human", "BRD4@mouse", "BRD2", "BRD3"
    }
    # 2 designs x 4 receptors
    assert len(panel.legs) == 8
    assert all(leg.n_draws == 2 for leg in panel.legs.values())
    assert panel.targets == ("BRD4@human", "BRD4@mouse")
    assert panel.aggregate == "worst_case"


def test_fold_panel_reaches_the_mouse_leg_through_its_own_coordinates():
    client = _FakeClient()
    camp = Campaigns(transport=None, client=client)  # type: ignore[arg-type]
    camp.fold_panel([A], targets=[_human(), _mouse()], aggregate="worst_case",
                    off_targets=["BRD2"])
    by_receptor = {c["receptor"]: c for c in client.peptides.calls}
    # ⛔ The mouse leg must NOT go out as target_gene="BRD4" — that resolves the
    # human structure and the leg would be a human measurement under a mouse
    # label, with nothing in the response contradicting it.
    assert "target_gene" not in by_receptor["BRD4_MOUSE"]
    assert by_receptor["BRD4_MOUSE"]["receptor_pdb"] == MOUSE_PDB
    assert by_receptor["BRD4"]["target_gene"] == "BRD4"


def test_fold_panel_produces_per_target_legs_and_a_co_optimized_delta():
    client = _FakeClient()
    camp = Campaigns(transport=None, client=client)  # type: ignore[arg-type]
    panel = camp.fold_panel([A], targets=[_human(), _mouse()], aggregate="worst_case",
                            off_targets=["BRD2", "BRD3"])
    complete, _partial, summary = panel.deltas(band=0.10, target_leg_min=0.66)
    row = complete[0]
    assert row.per_target_ipsae == {"BRD4@human": 0.90, "BRD4@mouse": 0.72}
    assert row.aggregate_ipsae == pytest.approx(0.72)   # worst case = mouse
    assert row.limiting_target == "BRD4@mouse"
    assert row.worst_off_target == "BRD2"               # 0.25 > 0.20
    assert row.delta == pytest.approx(0.47)
    assert row.verdict == "PASS_BOTH"
    assert row.quality_tier == "great_plus"
    # The frames are on the row, not inferred by the reader.
    assert row.frames["BRD4@mouse"].startswith("BRD4/mouse/Q9ESU6")
    assert summary["n_all_targets_pass"] == 1


def test_fold_panel_refuses_both_target_and_targets():
    camp = Campaigns(transport=None, client=_FakeClient())  # type: ignore[arg-type]
    with pytest.raises(LigandAIValidationError, match="exactly one of target="):
        camp.fold_panel([A], target="BRD4", targets=[_human()], off_targets=["BRD2"])


def test_fold_panel_refuses_several_targets_with_no_rule():
    camp = Campaigns(transport=None, client=_FakeClient())  # type: ignore[arg-type]
    with pytest.raises(LigandAIValidationError, match="aggregate= is\n?\\s*required"):
        camp.fold_panel([A], targets=[_human(), _mouse()], off_targets=["BRD2"])


def test_fold_panel_refuses_a_positive_target_that_is_also_an_off_target():
    camp = Campaigns(transport=None, client=_FakeClient())  # type: ignore[arg-type]
    with pytest.raises(LigandAIValidationError, match="both sides of the panel"):
        camp.fold_panel([A], targets=[_human(), _mouse()], aggregate="worst_case",
                        off_targets=["BRD4"])


def test_the_single_target_call_shape_is_unchanged():
    client = _FakeClient()
    camp = Campaigns(transport=None, client=client)  # type: ignore[arg-type]
    panel = camp.fold_panel([A], target="BRD4", off_targets=["BRD2"])
    assert panel.targets == ("BRD4",)
    assert panel.target == "BRD4"
    assert panel.aggregate is None
    complete, _p, summary = panel.deltas(band=0.10, target_leg_min=0.66)
    assert isinstance(complete[0], SelectivityDelta)
    assert complete[0].target_ipsae == pytest.approx(0.90)
    assert summary["positive_aggregate"] == "single"
    assert summary["target"] == "BRD4"


def test_to_rows_says_which_positive_target_each_leg_belongs_to():
    client = _FakeClient()
    camp = Campaigns(transport=None, client=client)  # type: ignore[arg-type]
    panel = camp.fold_panel([A], targets=[_human(), _mouse()], aggregate="worst_case",
                            off_targets=["BRD2"])
    rows = {r["receptor"]: r for r in panel.to_rows()}
    assert rows["BRD4@mouse"]["is_target"] is True
    assert rows["BRD4@mouse"]["positive_target"] == "BRD4@mouse"
    assert rows["BRD2"]["positive_target"] is None
    assert "mouse" in rows["BRD4@mouse"]["frame"]


# ─────────────────────────────────────────────────────────────────────────────
# 8. The recursive loop generates one leg per positive target
# ─────────────────────────────────────────────────────────────────────────────


def test_recursive_generates_one_leg_per_positive_target_and_pools_them():
    client = _FakeClient()
    camp = Campaigns(transport=None, client=client)  # type: ignore[arg-type]
    run = camp.recursive(
        targets=[_human(), _mouse_own_symbol()],
        aggregate="worst_case",
        off_targets=["BRD2"],
        band=0.10,
        target_leg_min=0.66,
        num_peptides=6,
    )
    rnd = run.run_round(confirm=False)
    # One generate() per positive target, the budget split between them.
    assert len(client.peptides.generate_calls) == 2
    assert [c["num_peptides"] for c in client.peptides.generate_calls] == [3, 3]
    # Each leg carries its OWN target identity — its own gene, not the first
    # target's. A leg generated against the wrong symbol is a leg conditioned on
    # the wrong pocket.
    assert {c["gene"] for c in client.peptides.generate_calls} == {"BRD4", "BRD4_MOUSE"}
    assert rnd.survivors  # folded jointly and selected on the worst case
    report = run.report()
    assert report["positive_aggregate"] == "worst_case"
    assert report["targets"] == ["BRD4@human", "BRD4_MOUSE@mouse"]
    assert report["cross_species"] is True
    assert "BRD4_MOUSE@mouse" in report["target_frames"]


def test_a_per_target_contrastive_panel_reaches_only_its_own_leg():
    client = _FakeClient()
    camp = Campaigns(transport=None, client=client)  # type: ignore[arg-type]
    panel_h = ContrastiveGuidance([_ref("BRD2")])
    run = camp.recursive(
        targets=[_human(), _mouse_own_symbol()],
        aggregate="worst_case",
        off_targets=["BRD2"],
        band=0.10,
        target_leg_min=0.66,
        num_peptides=2,
        contrastive_by_target={"BRD4@human": panel_h},
    )
    run.run_round(confirm=False)
    by_gene = client.peptides.generate_calls
    assert by_gene[0].get("contrastive") is panel_h
    assert "contrastive" not in by_gene[1]


def test_a_contrastive_panel_keyed_to_no_target_is_refused():
    camp = Campaigns(transport=None, client=_FakeClient())  # type: ignore[arg-type]
    with pytest.raises(LigandAIValidationError, match="not\n?\\s*positive targets|not "):
        camp.recursive(
            targets=[_human(), _mouse_own_symbol()],
            aggregate="worst_case",
            off_targets=["BRD2"],
            band=0.10,
            target_leg_min=0.66,
            contrastive_by_target={"BRD4@rat": ContrastiveGuidance([_ref("BRD2")])},
        )


def test_contrastive_references_refuses_a_paralog_that_is_a_positive_target():
    camp = Campaigns(transport=None, client=_FakeClient())  # type: ignore[arg-type]
    with pytest.raises(LigandAIValidationError, match="both sides of the panel"):
        camp.contrastive_references(
            targets=[_human(), _mouse()], paralogs=["BRD2", "BRD4"],
        )


# ─────────────────────────────────────────────────────────────────────────────
# 8b. The same-gene-symbol limit applies to GENERATION only, never to folding
# ─────────────────────────────────────────────────────────────────────────────


def test_recursive_refuses_two_positives_sharing_a_gene_symbol():
    camp = Campaigns(transport=None, client=_FakeClient())  # type: ignore[arg-type]
    with pytest.raises(LigandAIValidationError) as exc:
        camp.recursive(
            targets=[_human(), _mouse()],
            aggregate="worst_case",
            off_targets=["BRD2"],
            band=0.10,
            target_leg_min=0.66,
        )
    msg = str(exc.value)
    # ⛔ Not "please retry" — the server says that and it can never work.
    assert "structural limit" in msg
    assert "retrying will not change it" in msg
    # It must name the route that works, and say folding is unaffected.
    assert "upload_pdb" in msg
    assert "BRD4_MOUSE" in msg
    assert "fold_panel() accepts these same two targets" in msg


def test_fold_panel_still_measures_two_positives_sharing_a_symbol():
    # ⛔ THE LIMIT IS ON GENERATION, NOT MEASUREMENT. Each leg reaches its own
    # receptor through its own structure route and nothing keys a fold by gene,
    # so refusing the panel too would delete a capability that works.
    client = _FakeClient()
    camp = Campaigns(transport=None, client=client)  # type: ignore[arg-type]
    panel = camp.fold_panel([A], targets=[_human(), _mouse()],
                            aggregate="worst_case", off_targets=["BRD2"])
    assert panel.targets == ("BRD4@human", "BRD4@mouse")
    complete, _p, _s = panel.deltas(band=0.10, target_leg_min=0.66)
    assert complete[0].per_target_ipsae == {"BRD4@human": 0.90, "BRD4@mouse": 0.72}


def test_recursive_accepts_the_ortholog_under_its_own_symbol():
    client = _FakeClient()
    client.peptides.VALUES = {**_FakePeptides.VALUES, "BRD4_MOUSE": 0.72}
    camp = Campaigns(transport=None, client=client)  # type: ignore[arg-type]
    mouse_own = TargetSpec(
        gene="BRD4_MOUSE", organism="mouse", uniprot_id="Q9ESU6",
        receptor_pdb=MOUSE_PDB, receptor_name="BRD4_MOUSE",
    )
    run = camp.recursive(
        targets=[_human(), mouse_own],
        aggregate="worst_case",
        off_targets=["BRD2"],
        band=0.10,
        target_leg_min=0.66,
        num_peptides=2,
    )
    assert run.targets == ["BRD4@human", "BRD4_MOUSE@mouse"]
    assert run.co_optimization.is_cross_species is True


def test_two_different_genes_need_no_workaround():
    camp = Campaigns(transport=None, client=_FakeClient())  # type: ignore[arg-type]
    run = camp.recursive(
        targets=["EGFR", "ERBB2"],
        aggregate="worst_case",
        off_targets=["ERBB3"],
        band=0.10,
        target_leg_min=0.66,
    )
    assert run.targets == ["EGFR", "ERBB2"]


# ─────────────────────────────────────────────────────────────────────────────
# 9. Coercion keeps the old call shapes working
# ─────────────────────────────────────────────────────────────────────────────


def test_a_bare_gene_symbol_is_the_canonical_receptor():
    t = coerce_target("BRD4")
    assert t.key == "BRD4"
    assert t.organism_normalized == "human"
    assert t.fold_kwargs() == {"target_gene": "BRD4"}


def test_a_dict_is_accepted_as_a_target_spec():
    t = coerce_target({"gene": "BRD4", "label": "BRD4-BD1", "pdb_code": "3MXF"})
    assert t.key == "BRD4-BD1"
    assert t.pdb_code == "3MXF"


def test_a_nonsense_target_is_refused_by_type():
    with pytest.raises(LigandAIValidationError, match="must be a TargetSpec"):
        coerce_target(42)  # type: ignore[arg-type]
