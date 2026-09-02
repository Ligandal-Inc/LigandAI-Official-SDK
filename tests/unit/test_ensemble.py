# Copyright © 2025 Ligandal, Inc. All rights reserved.
"""Ensemble co-fold surface. bd-dre-3jqok.

The tests that matter here are the refusals, not the happy path: this interface
exists because silently dropping a cofactor, or reading a missing metric as a
measurement, produces a confident number for the wrong thing.
"""
from __future__ import annotations

import pytest

from ligandai.errors import LigandAIInvalidConfig
from ligandai.resources.ensemble import (
    COFACTOR_SUPPORT,
    CofactorSpec,
    Ensemble,
    _pick_metric,
    _spearman,
)


# --------------------------------------------------------------------------- #
# cofactors
# --------------------------------------------------------------------------- #
def test_cofactor_spec_requires_exactly_one_of_ccd_or_smiles():
    with pytest.raises(LigandAIInvalidConfig):
        CofactorSpec()
    with pytest.raises(LigandAIInvalidConfig):
        CofactorSpec(ccd="ZN", smiles="[Zn+2]")
    assert CofactorSpec(ccd="ZN").as_dict()["ccd"] == "ZN"


def test_cofactor_spec_rejects_zero_copies():
    with pytest.raises(LigandAIInvalidConfig):
        CofactorSpec(ccd="ZN", copies=0)


def test_cofactor_support_separates_engine_capability_from_reachability():
    """An engine that CAN carry a ligand but has no route to receive one must
    not be reported as simply 'unsupported' — the two facts are different and a
    caller acts on them differently."""
    sup = Ensemble.cofactor_support(["boltz2", "openfold3", "opendde", "promera"])
    assert sup["boltz2"]["engine_can_carry"] is True
    # openfold3 takes chains, but its chain spec accepts ccd_codes — "takes
    # chains" is NOT the same fact as "cannot carry a cofactor"
    assert sup["openfold3"]["engine_can_carry"] is True
    assert sup["opendde"]["engine_can_carry"] is False
    # unverified engines are declared unsupported, never assumed supported
    assert sup["promera"]["engine_can_carry"] is False
    # neither is reachable through the hosted route today
    assert sup["boltz2"]["reachable_today"] is False
    assert "ChainSpec" in sup["boltz2"]["reason"]
    assert "chains/sequences only" in sup["opendde"]["reason"]


def test_requesting_a_cofactor_no_arm_can_carry_raises_by_default():
    """⛔ The whole point. ENPP3 loses its binding interface without its two
    catalytic zincs; a zinc-free fold silently substituted for a zinc-bearing
    one is a measurement of a different molecule."""
    with pytest.raises(LigandAIInvalidConfig) as e:
        Ensemble._check_cofactors(["boltz2", "esmfold2"], [CofactorSpec(ccd="ZN")], False)
    msg = str(e.value)
    assert "WITHOUT the cofactor" in msg
    assert "allow_cofactor_absent=True" in msg


def test_cofactor_absent_opt_out_is_explicit_and_labels_the_arms():
    absent = Ensemble._check_cofactors(["boltz2"], [CofactorSpec(ccd="ZN")], True)
    assert absent == ["boltz2"]


def test_no_cofactor_request_never_raises():
    assert Ensemble._check_cofactors(["boltz2", "opendde"], [], False) == []


# --------------------------------------------------------------------------- #
# the 0.0 / wrong-quantity defect class
# --------------------------------------------------------------------------- #
def test_absent_metric_is_none_not_zero():
    assert _pick_metric({}, "peptide_ipsae") is None
    assert _pick_metric(None, "peptide_ipsae") is None


def test_a_different_quantity_is_never_substituted_for_the_requested_one():
    """Production has shipped plddt_interface/100 in place of a missing ipSAE
    and then counted the result as elite. Asking for ipSAE gets ipSAE or None."""
    assert _pick_metric({"ptm": 0.99, "plddt": 95.0}, "peptide_ipsae") is None


def test_metric_aliases_are_the_same_quantity_only():
    assert _pick_metric({"peptideIpsae": 0.71}, "peptide_ipsae") == 0.71
    assert _pick_metric({"peptide_ipsae": {"best": 0.66}}, "peptide_ipsae") == 0.66


def test_bool_is_not_a_score():
    assert _pick_metric({"iptm": True}, "iptm") is None


def test_a_real_zero_is_kept():
    """0.0 is a legal ipSAE — no interface at all. The guard is against an
    ABSENT field, not against a measured zero."""
    assert _pick_metric({"peptide_ipsae": 0.0}, "peptide_ipsae") == 0.0


# --------------------------------------------------------------------------- #
# consensus
# --------------------------------------------------------------------------- #
def _result():
    return Ensemble._build_result(
        0.67, ["boltz2", "esmfold2", "protenix"], "peptide_ipsae",
        {
            "UNANIMOUS": {"boltz2": 0.80, "esmfold2": 0.71, "protenix": 0.69},
            "CONTESTED": {"boltz2": 0.78, "esmfold2": 0.30, "protenix": 0.20},
            "REJECTED": {"boltz2": 0.20, "esmfold2": 0.10, "protenix": 0.15},
            "PARTIAL": {"boltz2": 0.90, "esmfold2": None, "protenix": None},
        },
        {"boltz2": True, "esmfold2": True, "protenix": True}, [], [], None, 1.0, None)


def test_consensus_counts_engines_above_the_bar():
    r = _result()
    by = {x.sequence: x for x in r.rows}
    assert by["UNANIMOUS"].n_engines_above_bar == 3
    assert by["CONTESTED"].n_engines_above_bar == 1
    assert by["REJECTED"].n_engines_above_bar == 0


def test_unanimous_is_relative_to_engines_that_actually_returned():
    """A peptide only one engine scored is not 'unanimous across three'. The
    denominator is what came back, and n_engines_scored says what that was."""
    by = {x.sequence: x for x in _result().rows}
    assert by["PARTIAL"].n_engines_scored == 1
    assert by["PARTIAL"].unanimous is True
    assert by["PARTIAL"].n_engines_above_bar == 1


def test_contested_flags_a_split_decision():
    by = {x.sequence: x for x in _result().rows}
    assert by["CONTESTED"].contested is True
    assert by["UNANIMOUS"].contested is False
    assert by["REJECTED"].contested is False


def test_rank_agreement_states_n_for_every_pair():
    r = _result()
    for a in r.rank_agreement():
        assert "n" in a
        if a["n"] < 3:
            assert a["spearman"] is None


def test_rank_agreement_uses_only_peptides_both_engines_scored():
    r = _result()
    pair = [a for a in r.rank_agreement()
            if {a["a"], a["b"]} == {"boltz2", "esmfold2"}][0]
    assert pair["n"] == 3          # PARTIAL excluded: esmfold2 returned nothing


def test_result_serialises_with_the_arms_kept_separable():
    d = _result().to_dict()
    assert set(d) >= {"bar", "engines", "metric", "cofactors", "consensus_histogram",
                      "rank_agreement", "failures", "designs"}
    assert all("cofactor_honoured" in x for x in d["designs"])


def test_spearman_matches_a_hand_computed_case():
    assert _spearman([1, 2, 3], [1, 2, 3]) == pytest.approx(1.0)
    assert _spearman([1, 2, 3], [3, 2, 1]) == pytest.approx(-1.0)
    assert _spearman([1, 2], [1, 2]) is None


# --------------------------------------------------------------------------- #
# cost ceiling
# --------------------------------------------------------------------------- #
class _FakeTransport:
    def __init__(self, total=707):
        self.total = total
        self.calls = []

    def request(self, method, path, **kw):
        self.calls.append((method, path, kw))
        return {"estimatedTotal": self.total, "subtotal": self.total,
                "discountPercent": 15,
                "perEngine": [{"engine": "boltz2", "trajectories": 5,
                               "rate": 50, "cost": 250}],
                "breakdown": "test"}


def test_estimate_scales_by_peptide_count_and_never_reprices_client_side():
    t = _FakeTransport(total=707)
    e = Ensemble(t, client=None)
    est = e.estimate(["boltz2", "esmfold2", "protenix"], n_peptides=25)
    assert est.credits_total == 707 * 25
    assert est.n_complexes == 25
    assert est.usd_total == pytest.approx(707 * 25 / 100.0, abs=0.01)
    # the server priced it; the SDK asked for ONE complex and multiplied
    method, path, kw = t.calls[0]
    assert (method, path) == ("POST", "/api/v1/cofold/estimate")
    assert kw["json"]["complexCount"] == 1


def test_estimate_warns_when_a_declared_cofactor_cannot_reach_an_arm():
    est = Ensemble(_FakeTransport(), client=None).estimate(
        ["boltz2", "opendde"], n_peptides=5, cofactors=["ZN"])
    assert est.warnings
    assert "WITHOUT them" in est.warnings[0]
    assert est.cofactor_arms == {"boltz2": False, "opendde": False}


def test_estimate_rejects_a_nonsense_peptide_count():
    with pytest.raises(LigandAIInvalidConfig):
        Ensemble(_FakeTransport(), client=None).estimate(["boltz2"], n_peptides=0)


def test_six_engines_are_accepted_because_the_server_accepts_six():
    """The SDK guard said 5 while fastapi MAX_ENGINES = 6 and the 6-engine
    bundle carries a 25% discount — the largest ensemble the platform sells was
    unreachable from Python."""
    from ligandai.resources.peptides import COFOLD_ENGINES, _cofold_normalize_engines
    assert len(COFOLD_ENGINES) == 6
    assert _cofold_normalize_engines(list(COFOLD_ENGINES)) == list(COFOLD_ENGINES)
