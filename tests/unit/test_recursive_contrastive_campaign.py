# Copyright © 2026 Ligandal, Inc. All rights reserved.
"""Unit tests for the recursive contrastive design loop. bd-dre-if562.

Covers the six steps a campaign runs and, more importantly, the five defects the
module exists to prevent — a partial panel read as a verdict, a Δ inside the
noise read as a preference, a near-tie read as an exact maximum, a
scaffold-carried interface read as a hit, and a silently-dropped guidance knob.

No network: the transport, the fold submitter and the metric surface are all
stubbed, so the whole loop runs offline and burns nothing.
"""

from __future__ import annotations

import pytest

from ligandai.contrastive import (
    AvoidancePolicy,
    ContrastiveGuidance,
    ContrastiveReference,
    PocketFeatureMatrix,
    V6_9_POCKET_FEATURE_DIM,
)
from ligandai.errors import (
    LigandAIInvalidConfig,
    LigandAIParameterNotForwarded,
    LigandAITimeoutError,
    LigandAIValidationError,
)
from ligandai.resources.campaigns import Campaigns, PanelLeg, PanelResult
from ligandai.resources.peptides import _validate_cysteine_mode

A = "ACDEFGHIKLMNP"
B = "WYVTSRQNKHGFE"
C = "LLLLIIIVVVAAA"


def _rows(dim: int = V6_9_POCKET_FEATURE_DIM, n: int = 6, fill: float = 0.25):
    return [[fill + 0.001 * i + 0.0001 * j for j in range(dim)] for i in range(n)]


def _ref(gene: str, dim: int = V6_9_POCKET_FEATURE_DIM) -> ContrastiveReference:
    return ContrastiveReference(gene=gene, features=PocketFeatureMatrix(rows=_rows(dim)))


# ─────────────────────────────────────────────────────────────────────────────
# Step 1 — contrastive guidance is a first-class, validated object
# ─────────────────────────────────────────────────────────────────────────────


def test_contrastive_guidance_carries_the_panel_and_the_width():
    g = ContrastiveGuidance([_ref("ENPP1"), _ref("ENPP6")])
    assert g.genes == ["ENPP1", "ENPP6"]
    assert g.expected_dim == 107
    assert g.cfg_reference == "paralog_mean"
    assert "ENPP1" in g.describe() and "paralog_mean" in g.describe()


def test_all_zero_reference_is_refused():
    zero = PocketFeatureMatrix(rows=[[0.0] * V6_9_POCKET_FEATURE_DIM for _ in range(4)])
    with pytest.raises(LigandAIValidationError, match="all zeros"):
        ContrastiveReference(gene="ENPP1", features=zero)


def test_empty_panel_is_refused():
    with pytest.raises(LigandAIValidationError, match="at least one reference"):
        ContrastiveGuidance([])


def test_width_mismatch_is_refused_not_padded():
    with pytest.raises(LigandAIValidationError, match="NOT convertible"):
        ContrastiveGuidance([_ref("ENPP1", dim=48)], expected_dim=107)


def test_duplicate_reference_gene_is_refused():
    with pytest.raises(LigandAIValidationError, match="duplicate"):
        ContrastiveGuidance([_ref("ENPP1"), _ref("enpp1")])


def test_uncond_operator_is_not_offered():
    with pytest.raises(LigandAIValidationError, match="paralog_mean"):
        ContrastiveGuidance([_ref("ENPP1")], cfg_reference="uncond")  # type: ignore[arg-type]


def test_guidance_weight_is_refused_because_no_request_field_reaches_it():
    with pytest.raises(LigandAIValidationError, match="not settable per request"):
        ContrastiveGuidance([_ref("ENPP1")], guidance_weight=2.0)


def test_v65_model_with_references_is_refused_client_side():
    g = ContrastiveGuidance([_ref("ENPP1")])
    with pytest.raises(LigandAIValidationError, match="does not use a CFG reference"):
        g.validate_for_model("v6.5.21")


def test_unknown_model_is_forwarded_not_guessed():
    # A published SDK cannot know a model released after it. Silence, not a veto.
    ContrastiveGuidance([_ref("ENPP1")]).validate_for_model("v7.0.0-unreleased")


def test_wire_emits_only_fields_the_platform_reads():
    wire = ContrastiveGuidance([_ref("ENPP1"), _ref("ENPP6")]).to_wire()
    assert set(wire) == {"reference_pocket_features", "referencePocketFeatures"}
    # cfg_reference / pocket_feature_dim are model properties, not request keys;
    # emitting them would look like configuration and be read by nothing.
    assert "cfg_reference" not in wire
    assert "pocket_feature_dim" not in wire
    assert len(wire["reference_pocket_features"]) == 2
    assert len(wire["reference_pocket_features"][0][0]) == 107


def test_unforwarded_guidance_raises_rather_than_silently_doing_nothing():
    # The passthrough landed (bd-dre-f4mc9), so the refusal is now opt-in for a
    # server build that predates it — `server_forwards=False`. A panel sent to a
    # CURRENT server is forwarded and must NOT be refused.
    g = ContrastiveGuidance([_ref("ENPP1")])
    g.require_forwardable()
    with pytest.raises(LigandAIParameterNotForwarded) as exc:
        g.require_forwardable(server_forwards=False)
    # The override must name the kwarg the CALLING surface has; see
    # test_the_override_error_names_the_kwarg_the_caller_actually_has.
    assert exc.value.override == "allow_unforwarded_contrastive=True"
    assert exc.value.tracking == "bd-dre-if562"
    # The escape suppresses that pinned-old-server refusal.
    g.require_forwardable(server_forwards=False, allow_unforwarded=True)


# ─────────────────────────────────────────────────────────────────────────────
# Step 1b — cyclisable designs: terminal_pair is reachable and typos are caught
# ─────────────────────────────────────────────────────────────────────────────


@pytest.mark.parametrize("mode", ["terminal_pair", "terminal_only", "disulfide_only"])
def test_cysteine_modes_the_platform_accepts_are_accepted(mode):
    assert _validate_cysteine_mode(mode) == mode


def test_unknown_cysteine_mode_is_rejected_client_side():
    # The platform substitutes disulfide_only WITHOUT an error, so a hyphen typo
    # would silently run a different experiment.
    with pytest.raises(LigandAIInvalidConfig) as exc:
        _validate_cysteine_mode("terminal-pair")
    assert "terminal_pair" in exc.value.allowed


# ─────────────────────────────────────────────────────────────────────────────
# Avoidance
# ─────────────────────────────────────────────────────────────────────────────


def test_hard_negatives_are_filtered_and_both_halves_returned():
    pol = AvoidancePolicy(hard_negatives=[A, "  " + B.lower() + " "])
    kept, rejected = pol.filter_sequences([A, B, C])
    assert kept == [C]
    assert sorted(rejected) == sorted([A, B])


def test_avoidance_generation_kwargs_map_to_the_real_knobs():
    pol = AvoidancePolicy(
        avoid_residues=[{"start": 34, "end": 37, "chain": "A"}],
        avoid_shell_angstrom=8.0,
        off_target_genes=["ENPP1"],
        hard_negatives=[A],
    )
    kw = pol.generation_kwargs()
    assert set(kw) == {"avoid_residues", "avoid_shell_angstrom"}
    # hard_negatives is NOT on the wire: no request field carries it.
    assert "hard_negatives" not in kw


# ─────────────────────────────────────────────────────────────────────────────
# Steps 2-4 — panel, Δ selection, and the four selection defects
# ─────────────────────────────────────────────────────────────────────────────


def _panel(spec: dict[tuple[str, str], list[float]], *, draws: int = 1,
           scaffold: set[tuple[str, str]] | None = None) -> PanelResult:
    scaffold = scaffold or set()
    legs: dict[tuple[str, str], PanelLeg] = {}
    for (seq, recep), values in spec.items():
        legs[(seq, recep)] = PanelLeg(
            sequence=seq,
            receptor=recep,
            fold_job_ids=[f"job-{seq[:3]}-{recep}-{i}" for i in range(len(values))],
            peptide_ipsae_draws=list(values),
            quality_tiers=[None] * len(values),
            scaffold_mediated_draws=[(seq, recep) in scaffold] * len(values),
        )
    return PanelResult(
        target="ENPP3", off_targets=["ENPP6", "ENPP1"], legs=legs, requested_draws=draws
    )


def test_delta_is_target_minus_the_worst_offtarget_not_the_mean():
    p = _panel({
        (A, "ENPP3"): [0.90],
        (A, "ENPP6"): [0.20],
        (A, "ENPP1"): [0.60],   # the one that binds BEST is the constraint
    })
    complete, partial, summary = p.deltas(band=0.10, target_leg_min=0.66)
    assert not partial
    row = complete[0]
    assert row.worst_off_target == "ENPP1"
    assert row.delta == pytest.approx(0.30)
    assert row.verdict == "PASS_BOTH"
    assert summary["n_pass_both"] == 1


def test_partial_panel_is_an_upper_bound_never_a_verdict():
    # The exact defect: +0.70 over ONE comparator, with the second missing.
    p = _panel({
        (A, "ENPP3"): [0.90],
        (A, "ENPP6"): [0.20],
        # ENPP1 leg absent
    })
    complete, partial, summary = p.deltas(band=0.10, target_leg_min=0.66)
    assert complete == []
    assert len(partial) == 1
    assert partial[0].delta_upper_bound == pytest.approx(0.70)
    assert partial[0].comparators_missing == ("ENPP1",)
    assert summary["n_complete"] == 0
    assert summary["n_partial_upper_bounds_not_verdicts"] == 1


def test_delta_inside_the_band_is_never_a_slight_preference():
    """A good target leg with an unreadable Δ is TARGET_ONLY, not a hit.

    This is the shape most easily misread as "binds the target, mildly
    selective". ``resolvable`` is False whatever the verdict rung says.
    """
    p = _panel({
        (A, "ENPP3"): [0.90],
        (A, "ENPP6"): [0.85],
        (A, "ENPP1"): [0.84],
    })
    complete, _partial, summary = p.deltas(band=0.23, target_leg_min=0.66)
    row = complete[0]
    assert row.delta == pytest.approx(0.05)
    assert row.verdict == "TARGET_ONLY"
    assert row.resolvable is False
    assert summary["n_delta_below_band"] == 1
    assert summary["n_pass_both"] == 0
    assert p.survivors(band=0.23, target_leg_min=0.66) == []


def test_indistinguishable_is_the_rung_for_a_failed_target_leg_and_no_delta():
    p = _panel({
        (A, "ENPP3"): [0.40],
        (A, "ENPP6"): [0.38],
        (A, "ENPP1"): [0.35],
    })
    complete, _partial, summary = p.deltas(band=0.23, target_leg_min=0.66)
    assert complete[0].verdict == "INDISTINGUISHABLE"
    assert complete[0].resolvable is False
    assert summary["n_indistinguishable"] == 1


def test_offtarget_preferred_is_distinct_from_indistinguishable():
    p = _panel({
        (A, "ENPP3"): [0.30],
        (A, "ENPP6"): [0.90],
        (A, "ENPP1"): [0.10],
    })
    complete, _p, summary = p.deltas(band=0.23, target_leg_min=0.66)
    assert complete[0].verdict == "OFFTARGET_PREFERRED"
    assert summary["n_offtarget_preferred"] == 1


def test_near_tie_is_reported_separately_from_the_exact_worst():
    p = _panel({
        (A, "ENPP3"): [0.90],
        (A, "ENPP6"): [0.500],
        (A, "ENPP1"): [0.495],   # within the declared 0.011 tolerance
    })
    complete, _p, summary = p.deltas(band=0.10, target_leg_min=0.66, tie_tolerance=0.011)
    row = complete[0]
    assert row.worst_off_target == "ENPP6"
    assert set(row.ties_within_tolerance) == {"ENPP6", "ENPP1"}
    assert summary["exact_worst_offtarget_counts"] == {"ENPP6": 1}
    assert summary["within_tolerance_of_worst_counts"]["ENPP1"] == 1


def test_scaffold_mediated_row_is_excluded_and_counted_not_silently_dropped():
    p = _panel(
        {
            (A, "ENPP3"): [0.95],
            (A, "ENPP6"): [0.10],
            (A, "ENPP1"): [0.10],
        },
        scaffold={(A, "ENPP3")},
    )
    complete, _p, summary = p.deltas(band=0.10, target_leg_min=0.66)
    assert complete == []
    assert summary["n_excluded_scaffold_mediated"] == 1


def test_band_is_required_and_has_no_default():
    p = _panel({(A, "ENPP3"): [0.9], (A, "ENPP6"): [0.1], (A, "ENPP1"): [0.1]})
    with pytest.raises(TypeError):
        p.deltas(target_leg_min=0.66)  # type: ignore[call-arg]


def test_incomplete_draws_do_not_count_as_a_complete_leg():
    # requested 2 draws, ENPP1 only returned 1 -> incomplete, upper bound only.
    p = _panel(
        {
            (A, "ENPP3"): [0.90, 0.88],
            (A, "ENPP6"): [0.20, 0.22],
            (A, "ENPP1"): [0.60],
        },
        draws=2,
    )
    complete, partial, _s = p.deltas(band=0.10, target_leg_min=0.66)
    assert complete == []
    assert partial and partial[0].comparators_missing == ("ENPP1",)


def test_null_peptide_ipsae_is_indeterminate_not_zero():
    leg = PanelLeg(sequence=A, receptor="ENPP3", peptide_ipsae_draws=[], not_computed=2)
    assert leg.value() is None
    assert leg.n_draws == 0
    assert leg.not_computed == 2


# ─────────────────────────────────────────────────────────────────────────────
# Step 5 — the band comes from measured spread, and replication is the verdict
# ─────────────────────────────────────────────────────────────────────────────


def test_observed_spread_is_a_range_and_the_cross_draw_band_widens_it():
    p = _panel(
        {
            (A, "ENPP3"): [0.90, 0.80],   # range 0.10
            (A, "ENPP6"): [0.20, 0.20],   # range 0.00
            (A, "ENPP1"): [0.50, 0.30],   # range 0.20
        },
        draws=2,
    )
    spread = p.observed_spread()
    assert spread["n_legs"] == 3
    assert spread["median_range"] == pytest.approx(0.10)
    assert spread["max_range"] == pytest.approx(0.20)
    # Two independent legs -> none of the noise cancels.
    assert p.cross_draw_band() == pytest.approx(0.10 * 2 ** 0.5, abs=1e-4)


def test_hold_rate_separates_lost_from_never_measured():
    fresh = _panel({
        (A, "ENPP3"): [0.90], (A, "ENPP6"): [0.10], (A, "ENPP1"): [0.10],  # holds
        (B, "ENPP3"): [0.90], (B, "ENPP6"): [0.89], (B, "ENPP1"): [0.10],  # lost
        # C absent entirely -> untested, NOT lost
    })
    rep = fresh.hold_rate([A, B, C], band=0.23, target_leg_min=0.66)
    assert rep.held == (A,)
    assert rep.lost == (B,)
    assert rep.untested == (C,)
    assert rep.hold_rate == pytest.approx(0.5)   # 1 of 2 MEASURED, not 1 of 3


def test_survivors_are_pass_both_only():
    p = _panel({
        (A, "ENPP3"): [0.90], (A, "ENPP6"): [0.10], (A, "ENPP1"): [0.10],
        (B, "ENPP3"): [0.50], (B, "ENPP6"): [0.10], (B, "ENPP1"): [0.10],  # Δ ok, target fails
    })
    assert p.survivors(band=0.23, target_leg_min=0.66) == [A]


# ─────────────────────────────────────────────────────────────────────────────
# Step 2 wiring — fold_panel submits the complete panel, and per-receptor
# ─────────────────────────────────────────────────────────────────────────────


class _FakeBatch:
    def __init__(self, receptor, seqs, draw):
        self.receptor = receptor
        self.jobs = [
            {"job_id": f"{receptor}-{draw}-{i}", "peptide_index": i, "sequence": s}
            for i, s in enumerate(seqs)
        ]
        self.waited = False

    def wait(self, **_kw):
        self.waited = True
        return []


class _FakePeptides:
    def __init__(self):
        self.calls = []

    def fold_batch(self, *, peptides, target_gene, **kw):
        self.calls.append({"receptor": target_gene, "n": len(peptides), **kw})
        draw = sum(1 for c in self.calls if c["receptor"] == target_gene) - 1
        return _FakeBatch(target_gene, peptides, draw)


class _FakeEvaluator:
    """Returns peptide_ipsae keyed off the receptor in the job id."""

    VALUES = {"ENPP3": 0.90, "ENPP6": 0.20, "ENPP1": 0.30}

    def filter_folds(self, ids, criteria=None):
        rows = []
        for jid in ids:
            recep = jid.split("-")[0]
            rows.append({
                "fold_job_id": jid,
                "quality_tier": {"tier": "great_plus"},
                "metrics": {
                    "peptide_ipsae": self.VALUES[recep],
                    "iptm": 0.81,
                    "plddt_mean": 89.0,
                    "scaffold_mediated": False,
                },
            })
        return {"results": rows}


class _FakeClient:
    def __init__(self):
        self.peptides = _FakePeptides()
        self.evaluator = _FakeEvaluator()


def test_fold_panel_submits_every_receptor_for_every_replicate():
    client = _FakeClient()
    camp = Campaigns(transport=None, client=client)  # type: ignore[arg-type]
    panel = camp.fold_panel(
        [A, B], target="ENPP3", off_targets=["ENPP6", "ENPP1"], replicates=2
    )
    # 3 receptors x 2 replicates
    assert len(client.peptides.calls) == 6
    # The first submission per receptor uses the dedupe; every later one must NOT,
    # or the "replicate" is a cached echo of the first draw.
    per_receptor = {}
    for c in client.peptides.calls:
        per_receptor.setdefault(c["receptor"], []).append(c["force_resubmit"])
    assert all(v == [False, True] for v in per_receptor.values())
    # 2 designs x 3 receptors legs, 2 draws each
    assert len(panel.legs) == 6
    assert all(leg.n_draws == 2 for leg in panel.legs.values())


def test_fold_panel_metrics_come_from_peptide_ipsae_and_produce_a_delta():
    client = _FakeClient()
    camp = Campaigns(transport=None, client=client)  # type: ignore[arg-type]
    panel = camp.fold_panel([A], target="ENPP3", off_targets=["ENPP6", "ENPP1"])
    complete, _p, summary = panel.deltas(band=0.10, target_leg_min=0.66)
    assert len(complete) == 1
    # worst off-target is ENPP1 at 0.30 -> 0.90 - 0.30
    assert complete[0].worst_off_target == "ENPP1"
    assert complete[0].delta == pytest.approx(0.60)
    assert complete[0].quality_tier == "great_plus"
    assert summary["n_pass_both"] == 1


def test_fold_panel_refuses_a_panel_with_no_offtarget():
    camp = Campaigns(transport=None, client=_FakeClient())  # type: ignore[arg-type]
    with pytest.raises(LigandAIValidationError, match="at least one off-target"):
        camp.fold_panel([A], target="ENPP3", off_targets=[])


def test_fold_panel_refuses_target_listed_as_its_own_offtarget():
    camp = Campaigns(transport=None, client=_FakeClient())  # type: ignore[arg-type]
    with pytest.raises(LigandAIValidationError, match="zero by construction"):
        camp.fold_panel([A], target="ENPP3", off_targets=["ENPP3", "ENPP6"])


# ─────────────────────────────────────────────────────────────────────────────
# Step 6 — the loop carries state forward
# ─────────────────────────────────────────────────────────────────────────────


def test_recursive_campaign_runs_two_rounds_and_carries_hard_negatives():
    client = _FakeClient()
    camp = Campaigns(transport=None, client=client)  # type: ignore[arg-type]
    run = camp.recursive(
        target="ENPP3",
        off_targets=["ENPP6", "ENPP1"],
        band=0.10,
        target_leg_min=0.66,
    )
    r1 = run.run_round(sequences=[A, B], confirm=True)
    assert r1.index == 1
    assert set(r1.survivors) == {A, B}
    # confirm=True re-folds the survivors at an independent draw
    assert r1.replication is not None
    assert set(r1.confirmed) == {A, B}
    assert run.confirmed == r1.confirmed

    # A design measured to prefer an off-target becomes a hard negative and is
    # not re-proposed in a later round.
    run.avoidance.hard_negatives.append(B)
    r2 = run.run_round(sequences=[A, B, C], confirm=False)
    assert B in r2.filtered_as_hard_negative
    assert B not in [row.sequence for row in r2.complete]

    report = run.report()
    assert report["n_rounds"] == 2
    assert report["target"] == "ENPP3"
    assert report["rounds"][0]["hold_rate"] == pytest.approx(1.0)


def test_single_replicate_round_says_the_band_was_not_verified():
    client = _FakeClient()
    camp = Campaigns(transport=None, client=client)  # type: ignore[arg-type]
    run = camp.recursive(
        target="ENPP3", off_targets=["ENPP6"], band=0.10, target_leg_min=0.66
    )
    rnd = run.run_round(sequences=[A], replicates=1, confirm=False)
    assert any("band not verified" in n for n in rnd.notes)


# ─────────────────────────────────────────────────────────────────────────────
# An unmounted route does not reliably 404
# ─────────────────────────────────────────────────────────────────────────────


class _SpaShellTransport:
    """A server that answers an unknown /api path with 200 and the app shell.

    Indistinguishable from "the endpoint is not mounted" at the client, and the
    fix is the same, so it must produce the same named error rather than an
    opaque KeyError-shaped one.
    """

    def request(self, method, path, **kw):
        return {"index": "<!doctype html>"}


class _NotFoundTransport:
    def request(self, method, path, **kw):
        from ligandai.errors import LigandAINotFoundError

        raise LigandAINotFoundError("not found")


@pytest.mark.parametrize("transport", [_SpaShellTransport(), _NotFoundTransport()])
def test_unmounted_pocket_features_route_gives_one_named_error(transport):
    camp = Campaigns(transport=transport, client=_FakeClient())  # type: ignore[arg-type]
    with pytest.raises(LigandAIParameterNotForwarded) as exc:
        camp.pocket_features("ENPP1", dim=107)
    assert exc.value.parameter == "pocket_features"
    assert exc.value.tracking == "bd-dre-eadc7"


def test_pocket_features_width_mismatch_is_refused_not_padded():
    class _WrongWidth:
        def request(self, method, path, **kw):
            return {"gene": "ENPP1", "features": _rows(dim=48), "pdb_code": "1ABC"}

    camp = Campaigns(transport=_WrongWidth(), client=_FakeClient())  # type: ignore[arg-type]
    with pytest.raises(LigandAIValidationError, match="NOT convertible"):
        camp.pocket_features("ENPP1", dim=107)


# ─── pocket-feature width per model (bd-LIGANDAI_ALPHA_V2-akqqj) ─────────────
#
# 48 / 65 / 107 are three DIFFERENT vectors and nothing converts between them.
# v6.9.21 and v6.9.22 are both "v6.9 line" and take DIFFERENT widths — mapping
# both to 107 would width-check a correct 65-wide v6.9.22 panel as wrong.
# Authority: fastapi_backend/services/paralog_reference_service.py:29.
def test_pocket_feature_width_is_per_model_not_per_line():
    from ligandai.contrastive import model_pocket_feature_dim

    assert model_pocket_feature_dim("v6.5") == 48
    assert model_pocket_feature_dim("v6.5.21") == 48
    assert model_pocket_feature_dim("v6.5.22") == 48
    assert model_pocket_feature_dim("v6.9.21") == 107
    assert model_pocket_feature_dim("v6.9.22") == 65
    # An id this build has never heard of must answer "ask the server", never
    # a guessed width.
    assert model_pocket_feature_dim("v7.0.0") is None


# ─────────────────────────────────────────────────────────────────────────────
# The panel's WAIT path — verified against the real BatchFoldJob contract
#
# Every test above this line stubs fold_batch with a fake whose `wait()` takes
# `**_kw`, so it accepted any kwarg the caller invented. `fold_panel` invented
# one: `raise_on_failure=False`, which `BatchFoldJob.wait` has never had. The
# published 0.14.0 therefore submitted (and charged for) the whole panel and
# then died with a bare TypeError on the very first wait — taking `replicate()`
# and every `RecursiveCampaign.run_round()` with it. A fake that accepts
# anything cannot catch that; these use the REAL signature.
# ─────────────────────────────────────────────────────────────────────────────


class _StrictBatch(_FakeBatch):
    """A batch whose ``wait`` has BatchFoldJob's exact parameter list."""

    def wait(self, timeout=7200.0, poll_interval=5.0, on_progress=None):
        self.waited = True
        return []


class _StrictPeptides(_FakePeptides):
    def fold_batch(self, *, peptides, target_gene, **kw):
        self.calls.append({"receptor": target_gene, "n": len(peptides), **kw})
        draw = sum(1 for c in self.calls if c["receptor"] == target_gene) - 1
        return _StrictBatch(target_gene, peptides, draw)


class _StrictClient(_FakeClient):
    def __init__(self):
        super().__init__()
        self.peptides = _StrictPeptides()


def test_fold_panel_only_passes_wait_kwargs_batchfoldjob_actually_accepts():
    """Signature contract: the panel may not invent parameters on wait()."""
    import inspect

    from ligandai.resources.peptides import BatchFoldJob

    accepted = set(inspect.signature(BatchFoldJob.wait).parameters) - {"self"}
    # These are the only kwargs fold_panel forwards to the batch wait.
    assert {"timeout", "poll_interval"} <= accepted
    # The kwarg 0.14.0 passed and BatchFoldJob has never had.
    assert "raise_on_failure" not in accepted


def test_fold_panel_waits_without_dying_on_the_real_wait_signature():
    client = _StrictClient()
    camp = Campaigns(transport=None, client=client)  # type: ignore[arg-type]
    panel = camp.fold_panel([A, B], target="ENPP3", off_targets=["ENPP6", "ENPP1"])
    assert len(panel.legs) == 6
    assert panel.wait_errors == []
    # And the metrics still landed, i.e. the wait really ran to completion.
    assert panel.leg(A, "ENPP3").peptide_ipsae_draws == [0.90]


def test_one_receptor_timing_out_does_not_discard_the_rest_of_the_panel():
    """Those folds are submitted and paid for; the surviving legs are real."""

    class _TimeoutOnOne(_StrictPeptides):
        def fold_batch(self, *, peptides, target_gene, **kw):
            batch = super().fold_batch(peptides=peptides, target_gene=target_gene, **kw)
            if target_gene == "ENPP1":
                def _boom(timeout=7200.0, poll_interval=5.0, on_progress=None):
                    raise LigandAITimeoutError("ENPP1 batch did not finish")
                batch.wait = _boom  # type: ignore[method-assign]
            return batch

    client = _StrictClient()
    client.peptides = _TimeoutOnOne()
    camp = Campaigns(transport=None, client=client)  # type: ignore[arg-type]
    panel = camp.fold_panel([A], target="ENPP3", off_targets=["ENPP6", "ENPP1"])
    # The failure is REPORTED, with the receptor it belongs to...
    assert [r for r, _e in panel.wait_errors] == ["ENPP1"]
    assert isinstance(panel.wait_errors[0][1], LigandAITimeoutError)
    # ...and the rest of the panel is still here rather than lost with it.
    assert panel.leg(A, "ENPP3").peptide_ipsae_draws == [0.90]
    assert panel.leg(A, "ENPP6").peptide_ipsae_draws == [0.20]
    assert len(panel.legs) == 3


# ─────────────────────────────────────────────────────────────────────────────
# The dedupe is not a measurement
#
# `fold_batch` answers a submission inside the local 24h dedupe window with a
# cached batch handle carrying NO per-sub-job entries. `fold_panel` builds legs
# by walking `job.jobs`, so such a handle yields ZERO legs — a panel that reads
# like a measured null. `replicate()` walked straight into it: the shortlist it
# is handed was, by construction, folded minutes earlier by the panel that
# selected it.
# ─────────────────────────────────────────────────────────────────────────────


class _CachedBatch:
    """What fold_batch returns on a dedupe hit: a batch id and nothing else."""

    def __init__(self):
        self.jobs: list[dict[str, object]] = []

    def wait(self, timeout=7200.0, poll_interval=5.0, on_progress=None):
        return []


class _DedupingPeptides(_StrictPeptides):
    """Returns a cached (empty) handle unless force_resubmit was requested."""

    def fold_batch(self, *, peptides, target_gene, **kw):
        self.calls.append({"receptor": target_gene, "n": len(peptides), **kw})
        if not kw.get("force_resubmit"):
            return _CachedBatch()
        draw = sum(1 for c in self.calls if c["receptor"] == target_gene) - 1
        return _StrictBatch(target_gene, peptides, draw)


def test_a_cached_batch_handle_is_recorded_not_silently_an_empty_panel():
    client = _StrictClient()
    client.peptides = _DedupingPeptides()
    camp = Campaigns(transport=None, client=client)  # type: ignore[arg-type]
    panel = camp.fold_panel([A], target="ENPP3", off_targets=["ENPP6", "ENPP1"])
    assert panel.legs == {}
    # The caller must be able to tell "nothing was measured" from "measured null".
    assert sorted(panel.unmapped_submissions) == ["ENPP1", "ENPP3", "ENPP6"]


def test_replicate_bypasses_the_dedupe_on_every_draw_not_just_the_second():
    """An echo of the selecting panel is not an independent draw."""
    client = _StrictClient()
    client.peptides = _DedupingPeptides()
    camp = Campaigns(transport=None, client=client)  # type: ignore[arg-type]
    panel, report = camp.replicate(
        [A], target="ENPP3", off_targets=["ENPP6", "ENPP1"],
        band=0.05, target_leg_min=0.66,
    )
    assert all(c["force_resubmit"] for c in client.peptides.calls)
    assert panel.unmapped_submissions == []
    assert len(panel.legs) == 3
    # A real measurement, so the design is held or lost — never "untested".
    assert report.untested == ()
    assert report.held == (A,)


def test_force_fresh_is_opt_in_on_fold_panel_and_off_by_default():
    client = _StrictClient()
    camp = Campaigns(transport=None, client=client)  # type: ignore[arg-type]
    camp.fold_panel([A], target="ENPP3", off_targets=["ENPP6"])
    assert [c["force_resubmit"] for c in client.peptides.calls] == [False, False]

    client2 = _StrictClient()
    camp2 = Campaigns(transport=None, client=client2)  # type: ignore[arg-type]
    camp2.fold_panel([A], target="ENPP3", off_targets=["ENPP6"], force_fresh=True)
    assert [c["force_resubmit"] for c in client2.peptides.calls] == [True, True]


def test_recursive_round_confirm_leg_inherits_the_fresh_draw():
    """`run_round(confirm=True)` re-folds designs it folded seconds earlier."""
    client = _StrictClient()
    client.peptides = _DedupingPeptides()
    camp = Campaigns(transport=None, client=client)  # type: ignore[arg-type]
    run = camp.recursive(
        target="ENPP3", off_targets=["ENPP6", "ENPP1"],
        band=0.05, target_leg_min=0.66,
    )
    rnd = run.run_round(sequences=[A], confirm=True)
    # The selecting panel is the non-fresh one and comes back unmeasurable...
    assert rnd.panel is not None and rnd.panel.unmapped_submissions
    # ...but the confirm leg must not ALSO be a cached echo. With no survivors
    # there is nothing to confirm, which is a fact about the panel, not a bug.
    assert rnd.survivors == []
    assert rnd.confirmed == []


def test_force_resubmit_is_rejected_with_the_right_knob_named():
    client = _StrictClient()
    camp = Campaigns(transport=None, client=client)  # type: ignore[arg-type]
    with pytest.raises(LigandAIValidationError, match="force_fresh=True, not force_resubmit"):
        camp.fold_panel(
            [A], target="ENPP3", off_targets=["ENPP6"], force_resubmit=True,
        )
    # Nothing was submitted, so the mistake costs no folds.
    assert client.peptides.calls == []


def test_an_unmeasured_round_says_so_instead_of_reporting_zero_survivors():
    client = _StrictClient()
    client.peptides = _DedupingPeptides()
    camp = Campaigns(transport=None, client=client)  # type: ignore[arg-type]
    run = camp.recursive(
        target="ENPP3", off_targets=["ENPP6"], band=0.05, target_leg_min=0.66,
    )
    rnd = run.run_round(sequences=[A], confirm=False)
    assert rnd.survivors == []
    assert any("UNMEASURED" in n for n in rnd.notes), rnd.notes
    assert any("force_fresh" in n for n in rnd.notes), rnd.notes


def test_the_override_error_names_the_kwarg_the_caller_actually_has():
    """`allow_unforwarded=True` on generate() is swallowed and does nothing."""
    import inspect

    from ligandai.resources.peptides import Peptides

    gen = set(inspect.signature(Peptides.generate).parameters)
    guidance = ContrastiveGuidance([_ref("ENPP1")])
    with pytest.raises(LigandAIParameterNotForwarded) as exc:
        guidance.require_forwardable(server_forwards=False)
    # The name the error hands the user must exist on the calling surface...
    assert exc.value.override == "allow_unforwarded_contrastive=True"
    assert "allow_unforwarded_contrastive" in gen
    # ...and the name it used to hand them must NOT, which is why it was wrong.
    assert "allow_unforwarded" not in gen
    assert "allow_unforwarded_contrastive=True" in str(exc.value)


def test_quality_tier_is_read_from_the_shape_filter_folds_actually_returns():
    """Observed live 2026-09-22: neither "tier" nor "name" is in the payload."""

    class _RealShapeEvaluator:
        def filter_folds(self, ids, criteria=None):
            return {"results": [
                {
                    "fold_job_id": jid,
                    "quality_tier": {
                        "criterion": "great_plus",
                        "verdict": "indeterminate",
                        "coValidated": False,
                        "summary": "Cannot be judged against Great+ "
                                   "(peptide_ipsae_missing)",
                    },
                    "metrics": {
                        "peptide_ipsae": None,   # NULL — indeterminate, not zero
                        "iptm": 0.7612702517885668,
                        "plddt_mean": 0.8180682347731644,
                        "scaffold_mediated": None,
                    },
                }
                for jid in ids
            ]}

    client = _StrictClient()
    client.evaluator = _RealShapeEvaluator()
    camp = Campaigns(transport=None, client=client)  # type: ignore[arg-type]
    panel = camp.fold_panel([A], target="ENPP3", off_targets=["ENPP6"])
    leg = panel.leg(A, "ENPP3")
    assert leg is not None
    # The platform's judgement reaches the leg instead of silently becoming None.
    assert leg.quality_tiers == ["indeterminate"]
    assert panel.to_rows()[0]["quality_tier"] == "indeterminate"
    # A NULL peptide_ipsae is counted as indeterminate, never averaged as a zero.
    assert leg.peptide_ipsae_draws == []
    assert leg.not_computed == 1
    assert leg.value() is None
    # ...and iptm/plddt still land, so the row is not empty.
    assert leg.iptm_draws == [0.7612702517885668]
    # No complete row can be built without the binder metric.
    complete, partial, _s = panel.deltas(band=0.05, target_leg_min=0.66)
    assert complete == []


def test_async_campaigns_says_sync_only_instead_of_bare_attributeerror():
    """The guard one line above it existed; these four fell through it."""
    from ligandai.errors import LigandAISurfaceNotInThisPackage
    from ligandai.resources.campaigns import AsyncCampaigns

    a = AsyncCampaigns.__new__(AsyncCampaigns)
    for name in ("fold_panel", "replicate", "recursive", "contrastive_references"):
        with pytest.raises(LigandAISurfaceNotInThisPackage) as exc:
            getattr(a, name)
        assert "only on the SYNC client" in str(exc.value)
        assert exc.value.surface == name
    # The platform-API guard still wins for its own names...
    with pytest.raises(LigandAISurfaceNotInThisPackage, match="does not exist in the"):
        a.mature
    # ...and a real typo is still a plain AttributeError, not a lecture.
    with pytest.raises(AttributeError):
        a.fold_pannel


def test_the_platforms_reason_for_a_null_binder_metric_is_not_dropped():
    """Live 2026-09-22: peptide_ipsae null + peptide_ipsae_status "not_computed"."""

    class _NotComputedEvaluator:
        def filter_folds(self, ids, criteria=None):
            return {"results": [
                {
                    "fold_job_id": jid,
                    "quality_tier": {"criterion": "great_plus",
                                     "verdict": "indeterminate",
                                     "summary": "peptide_ipsae_missing"},
                    "metrics": {
                        "peptide_ipsae": None,
                        "peptide_ipsae_status": "not_computed",
                        "iptm": 0.18390754262716416,
                        "plddt_mean": 0.524366280969091,
                        "scaffold_mediated": None,
                    },
                }
                for jid in ids
            ]}

    client = _StrictClient()
    client.evaluator = _NotComputedEvaluator()
    camp = Campaigns(transport=None, client=client)  # type: ignore[arg-type]
    panel = camp.fold_panel([A], target="ENPP3", off_targets=["ENPP6"])
    leg = panel.leg(A, "ENPP3")
    assert leg is not None and leg.not_computed_reasons == ("not_computed",)
    assert panel.to_rows()[0]["peptide_ipsae_status"] == ["not_computed"]
    _c, _p, summary = panel.deltas(band=0.05, target_leg_min=0.66)
    assert summary["peptide_ipsae_not_computed_reasons"] == {"not_computed": 2}


def test_a_round_that_scored_nothing_does_not_report_it_as_nothing_binding():
    class _NotComputedEvaluator:
        def filter_folds(self, ids, criteria=None):
            return {"results": [
                {"fold_job_id": jid,
                 "metrics": {"peptide_ipsae": None,
                             "peptide_ipsae_status": "not_computed",
                             "iptm": 0.2}}
                for jid in ids
            ]}

    client = _StrictClient()
    client.evaluator = _NotComputedEvaluator()
    camp = Campaigns(transport=None, client=client)  # type: ignore[arg-type]
    run = camp.recursive(target="ENPP3", off_targets=["ENPP6"],
                         band=0.05, target_leg_min=0.66)
    rnd = run.run_round(sequences=[A], confirm=False)
    assert rnd.survivors == []
    assert any("measured NOTHING" in n for n in rnd.notes), rnd.notes
    assert any("not_computed" in n for n in rnd.notes), rnd.notes
