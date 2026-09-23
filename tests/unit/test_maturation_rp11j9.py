# Copyright © 2026 Ligandal, Inc. All rights reserved.
"""The published maturation client, and the ways it must refuse. bd-dre-rp11j.9.1.

The platform has run an affinity/selectivity maturation loop server-side since
``bd-dre-vd1fk.4.1`` — ``POST /api/campaign/mature`` plus the readouts on the
campaign it returns. No published SDK carried a client for ANY of it, while
``client.campaigns`` — an unrelated LOCAL panel-folding helper — answered to the
same word, imported cleanly, and did something else. A customer reaching for
``client.campaigns.mature`` could not tell "this package cannot do maturation"
from "I called it wrong".

These tests cover the client and the nine ways a maturation number goes quietly
wrong on the way to a customer:

1. a Kd withheld for one of five NOT-ASSESSED reasons read as a design that
   did badly;
2. an absent instrument reading (``IPSAE_UNAVAILABLE``) collapsed into a
   below-gate result (``IPSAE_BELOW_GATE``);
3. a whole-complex ipSAE mirrored into the binder field;
4. the pre-fold PREDICTED ipSAE column (>1.0) standing in for the measured one;
5. a variant compared against a parent that was never re-folded in its cycle;
6. a selectivity margin quoted over an incomplete panel or an unmatched frame;
7. an unassessed pair sitting in the denominator of a win rate;
8. a ``do_relax`` knob advertised on a path whose scorer has no such parameter;
9. a withdrawn correlation coefficient passed through to a customer.

No network: the transport is a fake that returns recorded response shapes.
"""

from __future__ import annotations

import pytest

from ligandai.errors import LigandAISurfaceNotInThisPackage, LigandAIValidationError
from ligandai.resources.maturation import (
    AffinityRankingCaveat,
    AsyncMaturation,
    ControlFrame,
    CycleRecord,
    GateReason,
    HotspotFrame,
    IpsaeSource,
    LeaderboardSort,
    Maturation,
    MaturationLeaderboard,
    MaturationReadout,
    MaturationSettings,
    MaturationSpec,
    MaturedDesign,
    Objective,
    ParentDelta,
    PocketConditioning,
    Receptor,
    Seed,
    SelectivityReadout,
    TargetReadout,
)

CID = "camp_" + "a" * 26
REQUIRED_HEAD = "v10_boltz2_autodetected"

RECEPTOR_SEQ = "MKTAYIAKQRQISFVKSHFSRQLEERLGLIEVQ"
OFF_SEQ = "MKTAYIAKQRQISFVKSHFSRQLEERLGLIEVA"


# ─────────────────────────────────────────────────────────────────────────────
# Fakes
# ─────────────────────────────────────────────────────────────────────────────


class FakeTransport:
    """Records calls and replays a canned body per (method, path)."""

    def __init__(self, responses: dict[tuple[str, str], object] | None = None) -> None:
        self.responses = responses or {}
        self.calls: list[dict] = []

    def request(self, method, path, *, params=None, json=None, expect_json=True,
                idempotency_key=None, **kwargs):
        self.calls.append(
            {"method": method, "path": path, "params": params, "json": json,
             "idempotency_key": idempotency_key}
        )
        try:
            return self.responses[(method, path)]
        except KeyError:  # pragma: no cover - a test asked for an unrecorded call
            raise AssertionError(f"unrecorded call {method} {path}")

    def stream_lines(self, method, path, **kwargs):
        self.calls.append({"method": method, "path": path, "stream": True})
        yield from self.responses.get((method, path), [])


def a_spec(**overrides) -> MaturationSpec:
    kwargs = dict(
        seeds=[Seed("DEWQRALLSYFPE", seed_id="lead_A")],
        target=Receptor(
            gene="BRDET1",
            receptor_sequence=RECEPTOR_SEQ,
            pocket_residues=[31, 34, 38],
            avoid_residues=[77],
        ),
        objective=Objective(target_kd_nM=5.0),
    )
    kwargs.update(overrides)
    return MaturationSpec(**kwargs)


def a_readout(**overrides) -> dict:
    """A clean, quotable on-target readout."""
    row = {
        "gene": "BRDET1",
        "n_poses_total": 4,
        "n_poses_in_domain": 3,
        "ipsae_median": 0.78,
        "kd_nM": 3.2,
        "delta_g": -11.4,
        "gated": False,
        "gate_reason": None,
        "ipsae_gate": 0.67,
        "min_poses_in_domain": 2,
        "n_contacts": 41,
        "hot_hit": 2,
        "avoid_hit": 2,
        "scorer_provenance": {
            "scorer": "deltaforge_v10_2_unified_parallel",
            "scorer_version": "v10_2_unified_parallel_2026_05_08",
            "calibration_head": REQUIRED_HEAD,
        },
    }
    row.update(overrides)
    return row


# ─────────────────────────────────────────────────────────────────────────────
# 1. The capability exists at all, and the shadow points at it
# ─────────────────────────────────────────────────────────────────────────────


def test_client_exposes_a_maturation_namespace_distinct_from_campaigns():
    from ligandai import AsyncLigandAI, LigandAI

    sync = LigandAI(api_key="lgai_test_key", base_url="http://127.0.0.1:1")
    assert isinstance(sync.maturation, Maturation)
    assert sync.maturation is not sync.campaigns

    a = AsyncLigandAI(api_key="lgai_test_key", base_url="http://127.0.0.1:1")
    assert isinstance(a.maturation, AsyncMaturation)


@pytest.mark.parametrize("attr,expected", [
    ("mature", "client.maturation.start"),
    ("maturation", "client.maturation.readout"),
    ("leaderboard", "client.maturation.leaderboard"),
    ("stop", "client.maturation.stop"),
    ("stream", "client.maturation.stream"),
])
def test_campaigns_shadow_names_the_real_surface(attr, expected):
    """⛔ THE POINT OF THE WHOLE BEAD.

    ``client.campaigns.mature`` must not be a bare AttributeError (reads as a
    typo) and must not be a message that says the package cannot do this (no
    longer true). It has to name the shipped surface.
    """
    from ligandai import LigandAI

    client = LigandAI(api_key="lgai_test_key", base_url="http://127.0.0.1:1")
    with pytest.raises(LigandAISurfaceNotInThisPackage) as excinfo:
        getattr(client.campaigns, attr)
    msg = str(excinfo.value)
    assert expected in msg
    # and it still explains the collision rather than implying a rename
    assert "share a name" in msg
    assert "DIFFERENT capability" in msg
    # AttributeError subclass keeps hasattr()/duck-typing honest
    assert isinstance(excinfo.value, AttributeError)


def test_campaigns_shadow_no_longer_claims_the_package_cannot_do_this():
    from ligandai import LigandAI

    client = LigandAI(api_key="lgai_test_key", base_url="http://127.0.0.1:1")
    with pytest.raises(LigandAISurfaceNotInThisPackage) as excinfo:
        client.campaigns.mature
    assert "no version of this package has ever carried" not in str(excinfo.value)


def test_a_genuinely_unknown_attribute_is_still_a_plain_attribute_error():
    from ligandai import LigandAI

    client = LigandAI(api_key="lgai_test_key", base_url="http://127.0.0.1:1")
    with pytest.raises(AttributeError) as excinfo:
        client.campaigns.definitely_not_a_method
    assert not isinstance(excinfo.value, LigandAISurfaceNotInThisPackage)


# ─────────────────────────────────────────────────────────────────────────────
# 2. The request payload — what it says, and what it must never say
# ─────────────────────────────────────────────────────────────────────────────


def _walk(obj):
    if isinstance(obj, dict):
        for k, v in obj.items():
            yield k
            yield from _walk(v)
    elif isinstance(obj, (list, tuple)):
        for v in obj:
            yield from _walk(v)


def test_payload_never_carries_do_relax():
    """⛔ score_deltaforge_v102 has no do_relax parameter.

    ``do_relax`` belongs to compute_deltaforge_energy, the non-production path,
    where it returns min(native, relaxed) — a best-of-2 selection estimator, not
    a relaxed energy. Advertising it here would name a quality knob that is inert
    at best.
    """
    payload = a_spec().to_payload()
    assert "do_relax" not in set(_walk(payload))
    assert not hasattr(a_spec().screening, "do_relax")


def test_payload_states_the_frozen_settings_rather_than_relying_on_defaults():
    m = a_spec().to_payload()["maturation"]
    assert m["engine"] == "mutagenesis"
    assert m["pose_statistic"] == "median"
    assert m["refold_parent_controls"] is True
    assert m["include_uaa"] is False
    # and there is no way to set any of them from the SDK
    for frozen in ("engine", "pose_statistic", "refold_parent_controls", "include_uaa"):
        assert not hasattr(MaturationSettings(), frozen)


def test_defaults_are_the_measured_ones():
    m = MaturationSettings()
    assert m.ipsae_gate == 0.67
    assert m.min_poses_in_domain == 2
    assert m.pocket_conditioning is PocketConditioning.VARIABLE_ONLY
    assert m.hotspot_frame is HotspotFrame.MSA_MATCHED


def test_min_poses_of_one_is_refused_locally_as_a_best_of_one():
    with pytest.raises(LigandAIValidationError) as excinfo:
        MaturationSettings(min_poses_in_domain=1)
    assert "best-of-1" in str(excinfo.value)
    assert "MIN_POSES_TOO_LOW" in str(excinfo.value)


def test_non_canonical_seed_is_refused_before_it_costs_a_round_trip():
    with pytest.raises(LigandAIValidationError) as excinfo:
        Seed("DEWQRALLSYFPEX")
    assert "SEED_NON_CANONICAL" in str(excinfo.value)


def test_seed_id_colliding_with_a_generated_default_is_refused():
    """⛔ The case a validator over EXPLICIT ids alone cannot see.

    "seed_001" given explicitly at index 0 collides with the ``seed_001`` the
    server stamps for the unnamed seed at index 1 — and two lineage roots sharing
    one id make the inherited-avoidance readout resolve to whichever was written
    last.
    """
    with pytest.raises(LigandAIValidationError) as excinfo:
        a_spec(seeds=[Seed("DEWQRALLSYFPE", seed_id="seed_001"), Seed("QQWWEERRTTYY")])
    assert "SEED_ID_DUPLICATE" in str(excinfo.value)


def test_selectivity_without_a_panel_is_refused():
    with pytest.raises(LigandAIValidationError) as excinfo:
        a_spec(objective_mode="selectivity")
    assert "SELECTIVITY_PANEL_MISSING" in str(excinfo.value)


def test_selectivity_in_the_raw_hotspot_frame_is_refused():
    off = Receptor(gene="BRDET2", receptor_sequence=OFF_SEQ, pocket_residues=[30, 33])
    with pytest.raises(LigandAIValidationError) as excinfo:
        a_spec(objective_mode="selectivity", panel=[off],
               maturation=MaturationSettings(hotspot_frame="raw"))
    assert "SELECTIVITY_FRAME_UNMATCHED" in str(excinfo.value)


def test_avoiding_mode_without_avoid_residues_is_refused():
    target = Receptor(gene="BRDET1", receptor_sequence=RECEPTOR_SEQ, pocket_residues=[31])
    with pytest.raises(LigandAIValidationError) as excinfo:
        a_spec(target=target, objective_mode="affinity_avoiding")
    assert "AVOID_MODE_NEEDS_AVOID" in str(excinfo.value)
    # ...and the message must not imply the mode MINIMISES avoid_hit
    assert "does not minimise" in str(excinfo.value)


def test_the_target_cannot_be_its_own_off_target():
    same = Receptor(gene="BRDET1", receptor_sequence=OFF_SEQ, pocket_residues=[30])
    with pytest.raises(LigandAIValidationError):
        a_spec(panel=[same])


def test_a_panel_entry_needs_its_own_residue_frame():
    with pytest.raises(LigandAIValidationError) as excinfo:
        Receptor(gene="BRDET2", receptor_sequence=OFF_SEQ, pocket_residues=[])
    assert "PANEL_FRAME_MISSING" in str(excinfo.value)


def test_start_omits_the_quote_token_when_no_quote_was_accepted():
    """The FastAPI model is extra='forbid', so an always-present quoteToken would
    422 against the engine directly. One client, both routes."""
    t = FakeTransport({("POST", "/api/campaign/mature"): {"campaign_id": CID}})
    Maturation(t).start(a_spec())
    assert "quoteToken" not in t.calls[0]["json"]


def test_start_forwards_the_quote_token_when_one_is_accepted():
    t = FakeTransport({("POST", "/api/campaign/mature"): {"campaign_id": CID}})
    Maturation(t).start(a_spec(), quote="tok_abc")
    assert t.calls[0]["json"]["quoteToken"] == "tok_abc"


def test_a_start_response_without_a_campaign_id_is_an_error_not_a_handle():
    t = FakeTransport({("POST", "/api/campaign/mature"): {"status": "queued"}})
    with pytest.raises(LigandAIValidationError):
        Maturation(t).start(a_spec())


# ─────────────────────────────────────────────────────────────────────────────
# 3. The gate — five of six reasons are NOT a verdict about the design
# ─────────────────────────────────────────────────────────────────────────────


def test_a_clean_readout_is_quotable():
    r = TargetReadout.from_wire(a_readout(), peptide_length=13)
    assert r.kd_is_quotable and r.kd_nM == 3.2
    assert r.was_assessed and not r.is_negative_result


def test_only_below_gate_is_a_negative_result():
    for reason in GateReason:
        expected = reason is GateReason.IPSAE_BELOW_GATE
        assert reason.is_negative_result is expected
        assert reason.is_absent_measurement is (not expected)


def test_no_poses_beats_every_other_condition_including_a_long_peptide():
    """⛔ Claiming LENGTH_BIN_UNCALIBRATED for a fold that produced NOTHING
    asserts we measured something we never folded."""
    r = TargetReadout.from_wire(
        a_readout(n_poses_total=0, n_poses_in_domain=0, ipsae_median=None,
                  kd_nM=None, delta_g=None, gated=True),
        peptide_length=55,
    )
    assert r.gate_reason is GateReason.NO_POSES


def test_zero_in_domain_with_no_ipsae_is_unavailable_not_below_gate():
    r = TargetReadout.from_wire(
        a_readout(n_poses_in_domain=0, ipsae_median=None, kd_nM=None,
                  delta_g=None, gated=True),
        peptide_length=13,
    )
    assert r.gate_reason is GateReason.IPSAE_UNAVAILABLE
    assert not r.is_negative_result          # the instrument did not read
    assert not r.was_assessed


def test_zero_in_domain_with_a_read_ipsae_is_below_gate_and_IS_a_verdict():
    r = TargetReadout.from_wire(
        a_readout(n_poses_in_domain=0, ipsae_median=0.41, kd_nM=None,
                  delta_g=None, gated=True),
        peptide_length=13,
    )
    assert r.gate_reason is GateReason.IPSAE_BELOW_GATE
    assert r.is_negative_result and r.was_assessed


def test_a_fallback_ipsae_is_refused_and_gates_as_unavailable():
    """⛔ overall_fallback mirrors the WHOLE-COMPLEX value into the binder field.

    The refusal must run BEFORE the gate is derived, or an unread instrument
    reads as a design that fell below the gate.
    """
    r = TargetReadout.from_wire(
        a_readout(ipsae_source="overall_fallback", kd_nM=None, delta_g=None, gated=True),
        peptide_length=13,
    )
    assert r.gate_reason is GateReason.IPSAE_UNAVAILABLE
    assert r.ipsae_median is None
    assert r.withheld_ipsae_median == 0.78        # visible, not deleted
    assert r.ipsae_frame_valid is False


def test_an_absent_ipsae_source_stays_admissible():
    """The engine does not emit the field; ruling absent as UNAVAILABLE would
    gate every row and kill the capability."""
    r = TargetReadout.from_wire(a_readout(), peptide_length=13)
    assert r.ipsae_source is None
    assert r.ipsae_frame_valid is None
    assert r.kd_is_quotable


def test_too_few_in_domain_poses_withholds_the_number():
    r = TargetReadout.from_wire(
        a_readout(n_poses_in_domain=1, kd_nM=None, delta_g=None, gated=True),
        peptide_length=13,
    )
    assert r.gate_reason is GateReason.INSUFFICIENT_POSES
    assert not r.was_assessed


def test_a_long_peptide_withholds_the_energy_but_not_the_structure_metrics():
    r = TargetReadout.from_wire(
        a_readout(kd_nM=None, delta_g=None, gated=True), peptide_length=44
    )
    assert r.gate_reason is GateReason.LENGTH_BIN_UNCALIBRATED
    assert r.ipsae_median == 0.78                 # ipSAE is valid at every length


def test_a_wrong_calibration_head_withholds_the_kd():
    prov = {"scorer": "deltaforge_v10_2_unified_parallel", "calibration_head": "v9hq_xray"}
    r = TargetReadout.from_wire(
        a_readout(scorer_provenance=prov, gated=False), peptide_length=13
    )
    assert r.gate_reason is GateReason.SCORER_HEAD_MISMATCH
    assert r.kd_nM is None and r.withheld_kd_nM == 3.2
    assert not r.scorer_is_production


def test_no_provenance_at_all_withholds_the_kd():
    r = TargetReadout.from_wire(
        a_readout(scorer_provenance=None, gated=False), peptide_length=13
    )
    assert r.gate_reason is GateReason.SCORER_HEAD_MISMATCH


def test_the_forbidden_v9_scorer_is_detectable_in_provenance():
    prov = {"scorer": "compute_deltaforge_energy", "calibration_head": REQUIRED_HEAD}
    r = TargetReadout.from_wire(a_readout(scorer_provenance=prov), peptide_length=13)
    assert r.scorer_provenance is not None
    assert r.scorer_provenance.uses_forbidden_scorer


def test_an_unknown_gate_reason_still_withholds_and_keeps_its_raw_string():
    r = TargetReadout.from_wire(
        a_readout(gate_reason="SOME_FUTURE_REASON", kd_nM=None, delta_g=None, gated=True),
        peptide_length=13,
    )
    assert r.gate_reason is GateReason.UNRECOGNIZED
    assert r.gate_reason_raw == "SOME_FUTURE_REASON"
    assert not r.kd_is_quotable
    assert not r.is_negative_result          # unknown is never a verdict


def test_a_payload_that_both_gates_and_quotes_is_refused_not_repaired():
    with pytest.raises(LigandAIValidationError) as excinfo:
        TargetReadout.from_wire(
            a_readout(gated=True, gate_reason="IPSAE_BELOW_GATE"), peptide_length=13
        )
    assert "contradictory" in str(excinfo.value)


def test_an_ipsae_above_one_is_refused_as_the_prefold_predicted_column():
    """⛔ Refused, not clamped: a clamped value passes every gate below it."""
    with pytest.raises(LigandAIValidationError) as excinfo:
        TargetReadout.from_wire(a_readout(ipsae_median=1.34), peptide_length=13)
    assert "pre-fold" in str(excinfo.value)


# ─────────────────────────────────────────────────────────────────────────────
# 4. The parent comparison — same cycle or nothing
# ─────────────────────────────────────────────────────────────────────────────


def test_an_absent_control_frame_yields_no_verdict_even_if_numbers_arrive():
    """⛔ A Kd from a previous cycle on a different pose set is not a control.

    If a server ever sends paired numbers alongside control_frame='absent', they
    describe a comparison across fold conditions — the client drops them rather
    than presenting them as a refinement result.
    """
    d = ParentDelta.from_wire({
        "parent_design_id": "d1", "parent_kd_nM": 9.0, "variant_kd_nM": 3.0,
        "fold_improvement": 3.0, "delta_avoid_hit": -1, "is_win": True,
        "control_frame": "absent",
    })
    assert d.control_frame is ControlFrame.ABSENT
    assert d.is_win is None                  # never False-because-missing
    assert d.fold_improvement is None
    assert d.parent_kd_nM is None
    assert d.avoid_constraint_held is None   # never True-by-default
    assert "not a loss" in d.uncomparable_reason.lower()


def test_a_same_cycle_control_carries_the_verdict_and_the_paired_deltas():
    d = ParentDelta.from_wire({
        "parent_design_id": "d1", "parent_kd_nM": 3.2, "variant_kd_nM": 1.6,
        "fold_improvement": 2.0, "delta_avoid_hit": 0, "delta_hot_hit": -1,
        "delta_n_contacts": 30, "control_frame": "same_cycle", "is_win": True,
    })
    assert d.comparable and d.is_win is True
    assert d.fold_improvement == 2.0
    # ⛔ hot_hit FELL while Kd improved — which is why it is never an objective.
    assert d.delta_hot_hit == -1
    assert d.avoid_constraint_held is True


def test_an_unrecognised_control_frame_is_never_same_cycle():
    d = ParentDelta.from_wire({"control_frame": "some_new_frame", "is_win": True})
    assert d.control_frame is ControlFrame.UNRECOGNIZED
    assert not d.has_same_cycle_control and d.is_win is None


# ─────────────────────────────────────────────────────────────────────────────
# 5. Selectivity — complete AND matched, or None
# ─────────────────────────────────────────────────────────────────────────────


def test_an_incomplete_panel_withholds_the_margin():
    s = SelectivityReadout.from_wire({
        "panel_genes_expected": ["BRDET2", "BRDET3"],
        "panel_genes_evaluated": ["BRDET2"],
        "panel_complete": False,
        "hotspot_frame": "msa_matched",
        "selectivity_margin": 5.5, "selectivity_fold": 12.0,
    })
    assert s.selectivity_margin is None and s.selectivity_fold is None
    assert s.missing_genes == ("BRDET3",)
    assert "upper bound" in s.withheld_reason


def test_a_raw_hotspot_frame_withholds_the_margin_even_on_a_complete_panel():
    s = SelectivityReadout.from_wire({
        "panel_genes_expected": ["BRDET2"], "panel_genes_evaluated": ["BRDET2"],
        "panel_complete": True, "hotspot_frame": "raw",
        "selectivity_margin": 5.5,
    })
    assert not s.quotable and s.selectivity_margin is None
    assert "unnormalised COUNT" in s.withheld_reason


def test_a_complete_matched_panel_quotes_the_margin():
    s = SelectivityReadout.from_wire({
        "panel_genes_expected": ["BRDET2"], "panel_genes_evaluated": ["BRDET2"],
        "panel_complete": True, "hotspot_frame": "msa_matched",
        "hotspot_frame_resolved": "msa_matched",
        "on_target_margin": 5.5, "worst_off_target_margin": -1.75,
        "worst_off_target_gene": "BRDET2",
        "selectivity_margin": 7.25, "selectivity_fold": 11.0,
    })
    assert s.quotable and s.selectivity_margin == 7.25
    assert s.withheld_reason is None


def test_a_resolved_frame_that_fell_back_to_raw_still_withholds():
    s = SelectivityReadout.from_wire({
        "panel_genes_expected": ["BRDET2"], "panel_genes_evaluated": ["BRDET2"],
        "panel_complete": True, "hotspot_frame": "msa_matched",
        "hotspot_frame_resolved": "raw", "selectivity_margin": 5.5,
    })
    assert not s.quotable and s.selectivity_margin is None


# ─────────────────────────────────────────────────────────────────────────────
# 6. Rates — the denominator counts ASSESSED pairs only
# ─────────────────────────────────────────────────────────────────────────────


def test_win_rate_divides_by_assessed_pairs_not_by_every_paired_row():
    """⛔ The measured failure: 5 of 6 variants returned NO_POSES and the cycle
    reported wins=1, comparisons=6 — a 17% win rate where the figure is 1/1."""
    rec = CycleRecord.from_wire({
        "cycle": 1, "wins_vs_parent": 1, "comparisons": 1,
        "pairs_with_same_cycle_control": 6, "pairs_not_comparable": 5,
        "control_frame_valid": True,
    })
    assert rec.win_rate == 1.0
    assert "ASSESSED pairs" in rec.win_rate_caveat


def test_a_cycle_with_no_assessed_pairs_has_no_win_rate_rather_than_zero():
    rec = CycleRecord.from_wire({
        "cycle": 1, "wins_vs_parent": 0, "comparisons": 0,
        "pairs_with_same_cycle_control": 4, "control_frame_valid": True,
    })
    assert rec.win_rate is None                 # ⛔ not 0.0
    assert "never measured" in rec.win_rate_caveat


def test_an_invalid_control_frame_has_no_win_rate():
    rec = CycleRecord.from_wire({
        "cycle": 1, "wins_vs_parent": 3, "comparisons": 4, "control_frame_valid": False,
    })
    assert rec.win_rate is None


def test_run_level_wins_are_none_when_any_cycle_frame_is_invalid():
    readout = MaturationReadout.from_wire({
        "campaign_id": CID,
        "cycles": [
            {"cycle": 1, "wins_vs_parent": 5, "comparisons": 6, "control_frame_valid": True},
            {"cycle": 2, "wins_vs_parent": 2, "comparisons": 3, "control_frame_valid": False},
        ],
    })
    assert readout.total_wins_vs_parent is None   # ⛔ never a partial sum
    assert readout.win_rate is None


def test_run_level_wins_sum_when_every_frame_is_valid():
    readout = MaturationReadout.from_wire({
        "campaign_id": CID,
        "cycles": [
            {"cycle": 1, "wins_vs_parent": 5, "comparisons": 6, "control_frame_valid": True},
            {"cycle": 2, "wins_vs_parent": 2, "comparisons": 2, "control_frame_valid": True},
        ],
    })
    assert readout.total_wins_vs_parent == 7
    assert readout.total_comparisons == 8


# ─────────────────────────────────────────────────────────────────────────────
# 7. The ranking caveat — no coefficient, ever, in either direction
# ─────────────────────────────────────────────────────────────────────────────


def test_the_caveat_drops_a_withdrawn_coefficient_and_says_it_dropped_it():
    """⛔ bd-LIGANDAI_ALPHA_V2-jb73b.6 withdrew the pair. A deployment that has
    not caught up must not hand a customer a retracted number — and the removal
    must be visible in the data rather than invisible in the client."""
    caveat = AffinityRankingCaveat.from_wire({
        "ranking_is_validated": False,
        "within_target_spearman": -0.0736,
        "length_baseline_spearman": 0.1285,
        "n_rows": 12,
        "note": "some server note",
    })
    assert caveat.dropped_statistics == ("within_target_spearman", "length_baseline_spearman")
    assert "within_target_spearman" not in caveat.raw
    assert "length_baseline_spearman" not in caveat.raw
    assert not hasattr(caveat, "within_target_spearman")
    assert caveat.n_rows == 12


def test_the_sdk_never_states_a_correlation_coefficient_itself():
    import re

    from ligandai.resources import maturation as mod

    src = open(mod.__file__).read()
    # a bare coefficient-looking float attached to a correlation word
    assert not re.search(r"[Ss]pearman[^\n]{0,80}[-+]?0\.\d", src)
    assert "0.0736" not in src and "0.1285" not in src


def test_a_server_note_asserting_the_withdrawn_claim_is_replaced_not_relayed():
    """⛔ Stripping the coefficient while keeping the sentence ships the retracted
    claim in prose. "binder length out-ranked the model" IS the withdrawn finding:
    the length half reproduced only by bucketing NULL-gene rows as a target."""
    caveat = AffinityRankingCaveat.from_wire({
        "note": ("Neither DeltaForge head is validated for WITHIN-TARGET affinity "
                 "ranking. On the gated retrospective, binder length out-ranked "
                 "the model. Treat this ordering as a triage prior."),
    })
    assert "out-ranked" not in caveat.note
    assert "no measured-affinity evaluation" in caveat.note
    # ⛔ replaced, and it SAYS it replaced — a silently rewritten note is its own
    # kind of dishonesty, and the original stays inspectable.
    assert "note:length_outranked_the_model" in caveat.dropped_statistics
    assert "out-ranked" in caveat.raw["note_withheld"]


def test_a_legitimate_server_note_is_relayed_verbatim():
    caveat = AffinityRankingCaveat.from_wire({"note": "Scored on 12 gated poses."})
    assert caveat.note == "Scored on 12 gated poses."
    assert caveat.dropped_statistics == ()


def test_ranking_is_validated_is_frozen_false_even_if_a_server_claims_otherwise():
    caveat = AffinityRankingCaveat.from_wire({"ranking_is_validated": True})
    assert caveat.ranking_is_validated is False


def test_a_kd_sorted_page_always_carries_a_caveat_even_when_the_server_omits_it():
    """⛔ Omission is the failure this object exists to prevent."""
    page = MaturationLeaderboard.from_wire({"campaign_id": CID, "designs": [], "total": 0})
    assert page.ranking_caveat.ranking_is_validated is False
    assert "triage prior" in page.ranking_caveat.note.lower()


def test_sorting_by_contact_margin_is_refused():
    t = FakeTransport()
    with pytest.raises(LigandAIValidationError) as excinfo:
        Maturation(t).leaderboard(CID, sort=LeaderboardSort.MARGIN)
    msg = str(excinfo.value)
    assert "REPORTED, never ranked" in msg
    assert "scramble control" in msg
    assert not t.calls              # refused locally — no request was made


def test_a_missing_spend_grant_is_not_reported_as_a_low_balance():
    """⛔ Observed live once the metered route landed: POST /mature denied with
    402 credit_authorization_required / SPEND_GRANT_MISSING, and a generic 402
    handler answered "you need at least 2000 credits". Those are different fixes
    — one sends the customer to top up, the other to accept a quote."""
    from ligandai.errors import LigandAICreditError
    from ligandai.resources.maturation import _interpret_maturation_error

    exc = LigandAICreditError(
        '{"error":"credit_authorization_required"}',
        status_code=402,
        response={"detail": {"error": "credit_authorization_required",
                             "reason": "SPEND_GRANT_MISSING", "grantId": None},
                  "error": "credit_authorization_required"},
    )
    with pytest.raises(LigandAICreditError) as excinfo:
        _interpret_maturation_error(exc)
    msg = str(excinfo.value)
    assert "SPEND_GRANT_MISSING" in msg
    assert "Accept a quote" in msg
    assert "nothing was charged" in msg
    assert "at least" not in msg          # ⛔ not a balance problem


@pytest.mark.parametrize("reason,marker", [
    ("SPEND_GRANT_EXPIRED", "needs a NEW acceptance"),
    ("SPEND_GRANT_REPLAYED", "single-use"),
    ("SPEND_GRANT_REQUEST_MISMATCH", "EXACT inputs"),
    ("SPEND_GRANT_CEILING_EXCEEDED", "will not raise a ceiling"),
    ("SPEND_GRANT_UNCONFIGURED", "not your call being wrong"),
])
def test_each_spend_grant_reason_says_what_to_do_about_it(reason, marker):
    from ligandai.errors import LigandAIError
    from ligandai.resources.maturation import _interpret_maturation_error

    exc = LigandAIError("denied", status_code=409,
                        response={"detail": {"reason": reason}})
    with pytest.raises(LigandAIError) as excinfo:
        _interpret_maturation_error(exc)
    assert marker in str(excinfo.value)


def test_a_real_low_balance_still_reads_as_a_low_balance():
    from ligandai.errors import LigandAICreditError
    from ligandai.resources.maturation import _interpret_maturation_error

    exc = LigandAICreditError("insufficient credits", status_code=402,
                              response={"error": "insufficient_credits"})
    with pytest.raises(LigandAICreditError) as excinfo:
        _interpret_maturation_error(exc)
    assert "2000 credits" in str(excinfo.value)


# ─────────────────────────────────────────────────────────────────────────────
# 8. Leaderboard rows and their populations
# ─────────────────────────────────────────────────────────────────────────────


def test_a_leaderboard_separates_quotable_failed_and_never_measured():
    page = MaturationLeaderboard.from_wire({
        "campaign_id": CID,
        "total": 3,
        "designs": [
            {"design_id": "d1", "sequence": "DEWQRALLSYFPE", "cycle": 1,
             "on_target": a_readout()},
            {"design_id": "d2", "sequence": "DEWQRALLSYFPQ", "cycle": 1,
             "on_target": a_readout(n_poses_in_domain=0, ipsae_median=0.40,
                                    kd_nM=None, delta_g=None, gated=True)},
            {"design_id": "d3", "sequence": "DEWQRALLSYFPW", "cycle": 1,
             "on_target": a_readout(n_poses_total=0, n_poses_in_domain=0,
                                    ipsae_median=None, kd_nM=None,
                                    delta_g=None, gated=True)},
        ],
    })
    assert [d.design_id for d in page.quotable] == ["d1"]
    assert [d.design_id for d in page.assessed_and_failed] == ["d2"]
    assert [d.design_id for d in page.not_assessed] == ["d3"]
    # ⛔ "gated" would have lumped d2 and d3 together
    assert len(page.assessed_and_failed) + len(page.not_assessed) == 2


def test_a_design_exposes_the_binder_metric_not_the_whole_complex_one():
    d = MaturedDesign.from_wire({
        "design_id": "d1", "sequence": "DEWQRALLSYFPE",
        "on_target": a_readout(), "ipsae": 0.95,        # whole-complex decoy
    })
    assert d.peptide_ipsae == 0.78


def test_lineage_root_is_carried_separately_from_the_immediate_parent():
    d = MaturedDesign.from_wire({
        "design_id": "d9", "sequence": "DEWQRALLSYFPE", "cycle": 3,
        "parent_id": "d5", "seed_id": "lead_A", "root_seed_id": "lead_A",
        "on_target": a_readout(avoid_hit_inherited_from_seed=True),
    })
    assert d.parent_id == "d5" and d.root_seed_id == "lead_A"
    assert d.on_target.avoid_hit_inherited_from_seed is True
    assert not d.is_root_seed


# ─────────────────────────────────────────────────────────────────────────────
# 9. Transport plumbing
# ─────────────────────────────────────────────────────────────────────────────


def test_a_malformed_campaign_id_fails_locally_instead_of_reading_as_not_yours():
    t = FakeTransport()
    with pytest.raises(LigandAIValidationError):
        Maturation(t).status("not-a-campaign-id")
    assert not t.calls


def test_every_endpoint_is_reached_at_its_documented_path():
    t = FakeTransport({
        ("POST", "/api/campaign/mature"): {"campaign_id": CID, "status": "queued"},
        ("GET", f"/api/campaign/{CID}/status"): {"campaign_id": CID, "mode": "maturation",
                                                 "status": "running"},
        ("GET", f"/api/campaign/{CID}/maturation"): {"campaign_id": CID, "cycles": []},
        ("GET", f"/api/campaign/{CID}/leaderboard"): {"campaign_id": CID, "designs": []},
        ("POST", f"/api/campaign/{CID}/stop"): {"campaign_id": CID, "status": "stopping"},
        ("GET", "/api/campaign"): {"campaigns": [{"campaign_id": CID, "mode": "maturation"}]},
    })
    m = Maturation(t)
    run = m.start(a_spec())
    assert run.campaign_id == CID
    assert run.status().is_maturation
    assert isinstance(run.readout(), MaturationReadout)
    assert isinstance(run.leaderboard(), MaturationLeaderboard)
    assert run.stop().status == "stopping"
    assert m.list()[0].campaign_id == CID
    assert [c["path"] for c in t.calls] == [
        "/api/campaign/mature",
        f"/api/campaign/{CID}/status",
        f"/api/campaign/{CID}/maturation",
        f"/api/campaign/{CID}/leaderboard",
        f"/api/campaign/{CID}/stop",
        "/api/campaign",
    ]


def test_the_stream_reassembles_named_sse_events():
    t = FakeTransport({
        ("GET", f"/api/campaign/{CID}/stream"): [
            "event: snapshot", 'data: {"status": "running"}',
            "event: cycle_complete", 'data: {"cycle": 1, "wins_vs_parent": 5}',
            "event: campaign_complete", 'data: {"status": "completed"}',
        ]
    })
    events = list(Maturation(t).stream(CID))
    assert [e.event for e in events] == ["snapshot", "cycle_complete", "campaign_complete"]
    assert events[1].data["wins_vs_parent"] == 5
    assert events[-1].is_terminal


def test_iter_designs_pages_until_the_server_runs_out():
    page1 = {"campaign_id": CID, "total": 3, "limit": 2, "offset": 0,
             "designs": [{"design_id": "d1", "on_target": a_readout()},
                         {"design_id": "d2", "on_target": a_readout()}]}
    page2 = {"campaign_id": CID, "total": 3, "limit": 2, "offset": 2,
             "designs": [{"design_id": "d3", "on_target": a_readout()}]}
    pages = iter([page1, page2])

    class Paging(FakeTransport):
        def request(self, method, path, **kwargs):
            self.calls.append({"method": method, "path": path, **kwargs})
            return next(pages)

    got = [d.design_id for d in Maturation(Paging()).iter_designs(CID, page_size=2)]
    assert got == ["d1", "d2", "d3"]


def test_the_async_surface_mirrors_the_sync_one():
    sync_methods = {
        n for n in dir(Maturation)
        if not n.startswith("_") and callable(getattr(Maturation, n))
    }
    async_methods = {
        n for n in dir(AsyncMaturation)
        if not n.startswith("_") and callable(getattr(AsyncMaturation, n))
    }
    assert sync_methods <= async_methods, sync_methods - async_methods
