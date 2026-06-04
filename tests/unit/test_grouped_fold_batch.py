# Copyright © 2026 Ligandal, Inc. All rights reserved.
"""Unit tests for the grouped batch-fold API + per-entity MSA control.

bd-LIGANDAI_ALPHA_V2-wymqb. Verifies the SDK ``fold_batch(groups=...)`` body
builder + normalization helpers route MSA flags correctly at both the target
and partner level, and that the flat (legacy) form is unchanged.
"""

from __future__ import annotations

import json

import pytest

from ligandai.resources.peptides import (
    _build_batch_fold_body,
    _build_grouped_batch_fold_body,
    _normalize_fold_groups,
    _normalize_group_partner,
    _normalize_group_target,
)


# Long enough that a length heuristic alone would treat the protein partner as
# an MSA target; the test asserts the FLAG (not length) drives MSA routing.
_PROTEIN_PARTNER = "MKLVAAGTRLLPAAGTRLLPAAGTRLLPAAGTRLLP"
_TARGET_SEQ = "MEEPQSDPSVEPPLSQETFSDLWKLLPENNVLSPLPSQAMDDLMLSPDDIEQWFTEDPGP"


def test_grouped_body_routes_msa_flags():
    groups = [
        {
            "target": {"gene": "EGFR", "use_msa": True},
            "partners": [
                {"sequence": "ACDEFGHIK", "type": "peptide"},
                {"sequence": _PROTEIN_PARTNER, "type": "protein", "use_msa": True},
            ],
        },
        {
            "target": {"sequence": _TARGET_SEQ},
            "partners": [{"sequence": "WYLKPRSTV", "type": "peptide"}],
        },
    ]
    body = _build_grouped_batch_fold_body(
        groups=groups,
        diffusion_samples=4,
        sampling_steps=50,
        recycling_steps=None,
        step_scale=None,
        glycosylation=None,
        template_mode=False,
        n_parallel_gpus=None,
        session_id=None,
        contribute_to_receptordb=None,
        num_trajectories=None,
        msa_depth=None,
        use_potentials=True,
    )
    g = body["groups"]
    assert len(g) == 2

    # Target = chain A, protein, MSA ON.
    t0 = g[0]["target"]
    assert t0["chainId"] == "A"
    assert t0["type"] == "protein"
    assert t0["use_msa"] is True
    assert t0["gene"] == "EGFR"

    # Peptide partner -> chain B, no MSA, flagged isPeptide + role.
    p0 = g[0]["partners"][0]
    assert p0["chainId"] == "B"
    assert p0["use_msa"] is False
    assert p0["isPeptide"] is True
    assert p0["role"] == "peptide"

    # Protein partner with explicit use_msa=True -> chain C, MSA ON, NOT peptide.
    p1 = g[0]["partners"][1]
    assert p1["chainId"] == "C"
    assert p1["use_msa"] is True
    assert "isPeptide" not in p1

    # Group 1 target sequence mode; default-typed peptide partner -> no MSA.
    assert g[1]["target"]["sequence"] == _TARGET_SEQ
    assert g[1]["target"]["use_msa"] is True
    p2 = g[1]["partners"][0]
    assert p2["use_msa"] is False
    assert p2["isPeptide"] is True

    # Steering potentials forwarded in both casings.
    assert body["use_potentials"] is True
    assert body["usePotentials"] is True
    assert body["diffusion_samples"] == 4
    assert body["sampling_steps"] == 50


def test_target_msa_defaults_on_when_unset():
    t = _normalize_group_target({"gene": "CD47"}, group_index=0)
    assert t["use_msa"] is True


def test_target_msa_explicit_off():
    t = _normalize_group_target({"gene": "CD47", "use_msa": False}, group_index=0)
    assert t["use_msa"] is False


def test_protein_partner_defaults_msa_on():
    p = _normalize_group_partner(
        {"sequence": _PROTEIN_PARTNER, "type": "protein"}, group_index=0, partner_index=0
    )
    assert p["use_msa"] is True
    assert "isPeptide" not in p


def test_peptide_partner_defaults_msa_off():
    p = _normalize_group_partner(
        {"sequence": "ACDEFGHIK", "type": "peptide"}, group_index=0, partner_index=0
    )
    assert p["use_msa"] is False
    assert p["isPeptide"] is True


def test_protein_partner_explicit_msa_off_is_not_peptide_but_guarded():
    p = _normalize_group_partner(
        {"sequence": _PROTEIN_PARTNER, "type": "protein", "use_msa": False},
        group_index=0,
        partner_index=0,
    )
    assert p["use_msa"] is False
    # An explicit protein opt-out is still a no-MSA chain but NOT labelled a peptide.
    assert "isPeptide" not in p


def test_bare_string_partner_is_peptide():
    p = _normalize_group_partner("ACDEFGHIK", group_index=0, partner_index=0)
    assert p["use_msa"] is False
    assert p["isPeptide"] is True


def test_chain_ids_assigned_in_order():
    groups = _normalize_fold_groups(
        [
            {
                "target": {"gene": "EGFR"},
                "partners": [
                    {"sequence": "AAAA", "type": "peptide"},
                    {"sequence": "CCCC", "type": "peptide"},
                    {"sequence": "DDDD", "type": "peptide"},
                ],
            }
        ]
    )
    chains = [groups[0]["target"]["chainId"]] + [p["chainId"] for p in groups[0]["partners"]]
    assert chains == ["A", "B", "C", "D"]


def test_target_requires_exactly_one_mode():
    with pytest.raises(ValueError):
        _normalize_group_target({"gene": "EGFR", "sequence": "AAAA"}, group_index=0)
    with pytest.raises(ValueError):
        _normalize_group_target({}, group_index=0)


def test_partner_rejects_bad_type():
    with pytest.raises(ValueError):
        _normalize_group_partner(
            {"sequence": "AAAA", "type": "rna"}, group_index=0, partner_index=0
        )


def test_partner_requires_sequence():
    with pytest.raises(ValueError):
        _normalize_group_partner({"type": "peptide"}, group_index=0, partner_index=0)


def test_empty_groups_rejected():
    with pytest.raises(ValueError):
        _normalize_fold_groups([])


def test_group_requires_partners():
    with pytest.raises(ValueError):
        _normalize_fold_groups([{"target": {"gene": "EGFR"}, "partners": []}])


def test_flat_form_body_unchanged():
    """The legacy flat form must not gain a ``groups`` key."""
    body = _build_batch_fold_body(
        peptides=["ACDEFGHIK", "WYLKPRSTV"],
        target_gene="EGFR",
        receptor_pdb=None,
        receptor_sequence=None,
        receptor_name=None,
        diffusion_samples=4,
        sampling_steps=50,
        recycling_steps=None,
        step_scale=None,
        msa_enabled=None,
        glycosylation=None,
        template_mode=False,
        n_parallel_gpus=None,
        session_id=None,
        contribute_to_receptordb=None,
        use_potentials=True,
    )
    assert body["peptides"] == ["ACDEFGHIK", "WYLKPRSTV"]
    assert body["target_gene"] == "EGFR"
    assert "groups" not in body


def test_grouped_body_is_json_serializable():
    body = _build_grouped_batch_fold_body(
        groups=[
            {"target": {"gene": "EGFR"}, "partners": [{"sequence": "ACDEFGHIK"}]}
        ],
        diffusion_samples=1,
        sampling_steps=50,
        recycling_steps=3,
        step_scale=None,
        glycosylation=None,
        template_mode=False,
        n_parallel_gpus=None,
        session_id=None,
        contribute_to_receptordb=None,
        num_trajectories=None,
        msa_depth=16,
        use_potentials=None,
    )
    # Round-trips through JSON (wire safety).
    assert json.loads(json.dumps(body))["msa_depth"] == 16
    # use_potentials None omits the key.
    assert "use_potentials" not in body
