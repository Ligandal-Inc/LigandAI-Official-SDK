# Copyright © 2026 Ligandal, Inc. All rights reserved.
"""Unit tests for merging session fold_results onto peptides.

Refs: bd-LIGANDAI_ALPHA_V2-u4gss.

The session detail endpoint returns the pre-fold peptide list and the post-fold
structural scores (``fold_results``) as SEPARATE structures; the server only
stamps ``peptide.folded=true``. Without the merge, generate(auto_fold=True)
reports n_folded=0 even when folding succeeded. These tests exercise the merge
helper directly across the gene-keyed dict shape, the flat-list shape, the
best-by-iPSAE tie-break, and the no-op guards.
"""

from __future__ import annotations

from ligandai.resources.peptides import (
    _iter_session_fold_results,
    _merge_fold_results_into_peptides,
    _to_optional_float,
)


def test_to_optional_float() -> None:
    assert _to_optional_float(None) is None
    assert _to_optional_float("3.5") == 3.5
    assert _to_optional_float("nan-ish") is None
    assert _to_optional_float(2) == 2.0


def test_iter_gene_keyed_and_list_shapes() -> None:
    gene_keyed = {"PRLR": [{"sequence": "AC"}, {"sequence": "DE"}]}
    pairs = list(_iter_session_fold_results(gene_keyed))
    assert pairs[0][0] == "PRLR" and pairs[0][1]["sequence"] == "AC"
    flat = [{"sequence": "AC"}]
    pairs2 = list(_iter_session_fold_results(flat))
    assert pairs2[0][0] is None and pairs2[0][1]["sequence"] == "AC"


def test_merge_stamps_metrics_on_flat_peptides() -> None:
    peps = [{"sequence": "ACDEF", "targetGene": "PRLR"}]
    fold_results = {
        "PRLR": [
            {
                "sequence": "ACDEF",
                "ipsae": 0.82,
                "iptm": 0.71,
                "ptm": 0.66,
                "plddt": 88.0,
                "deltaG": -9.1,
                "scores": {"predictedKd": 12.3},
            }
        ]
    }
    _merge_fold_results_into_peptides(peps, fold_results)
    p = peps[0]
    assert p["folded"] is True
    assert p["ipsae"] == 0.82
    assert p["iptm"] == 0.71
    assert p["ptm"] == 0.66
    assert p["plddt"] == 88.0
    assert p["deltaforgeDg"] == -9.1
    assert p["predictedKd"] == 12.3


def test_merge_keeps_best_ipsae_pose() -> None:
    peps = [{"sequence": "ACDEF", "gene": "PRLR"}]
    fold_results = {
        "PRLR": [
            {"sequence": "ACDEF", "ipsae": 0.40, "iptm": 0.30},
            {"sequence": "ACDEF", "ipsae": 0.90, "iptm": 0.85},
        ]
    }
    _merge_fold_results_into_peptides(peps, fold_results)
    # Highest-iPSAE pose wins.
    assert peps[0]["ipsae"] == 0.90
    assert peps[0]["iptm"] == 0.85


def test_merge_gene_keyed_peptides_dict() -> None:
    peps = {"PRLR": [{"sequence": "ACDEF"}]}
    fold_results = {"PRLR": [{"sequence": "ACDEF", "ipsae": 0.5}]}
    _merge_fold_results_into_peptides(peps, fold_results)
    p = peps["PRLR"][0]
    assert p["folded"] is True
    assert p["ipsae"] == 0.5
    # gene stamped onto the peptide for downstream matching.
    assert p["targetGene"] == "PRLR"


def test_merge_does_not_overwrite_existing_values() -> None:
    peps = [{"sequence": "ACDEF", "ipsae": 0.99}]
    fold_results = [{"sequence": "ACDEF", "ipsae": 0.10}]
    _merge_fold_results_into_peptides(peps, fold_results)
    # _set_if_missing must not clobber a pre-existing value.
    assert peps[0]["ipsae"] == 0.99
    assert peps[0]["folded"] is True


def test_merge_noop_on_empty_fold_results() -> None:
    peps = [{"sequence": "ACDEF"}]
    _merge_fold_results_into_peptides(peps, None)
    _merge_fold_results_into_peptides(peps, {})
    assert "folded" not in peps[0]


def test_merge_unmatched_sequence_left_untouched() -> None:
    peps = [{"sequence": "ZZZZ"}]
    fold_results = [{"sequence": "ACDEF", "ipsae": 0.7}]
    _merge_fold_results_into_peptides(peps, fold_results)
    assert "folded" not in peps[0]
