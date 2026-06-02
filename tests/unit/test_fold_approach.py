# Copyright © 2026 Ligandal, Inc. All rights reserved.
"""Tests for the dvckm.1 (v0.7.0) fold_approach + ESMFold2 hooks."""

from __future__ import annotations

import pytest

from ligandai.resources.peptides import (
    _fold_body,
    _resolve_fold_approach,
    _VALID_FOLD_APPROACHES,
)


class TestResolveApproach:
    def test_default_is_boltz2_affinity(self):
        assert _resolve_fold_approach(None) == "boltz2_affinity"
        assert _resolve_fold_approach("") == "boltz2_affinity"

    @pytest.mark.parametrize("alias", ["boltz2", "boltz", "boltz_2", "boltz_affinity",
                                        "BOLTZ2", "Boltz2"])
    def test_boltz_aliases(self, alias):
        assert _resolve_fold_approach(alias) == "boltz2_affinity"

    def test_esmfold_alias(self):
        assert _resolve_fold_approach("esmfold") == "esmfold2"

    @pytest.mark.parametrize("name", ["esmfold2", "esmfold2_fast", "boltz2_affinity"])
    def test_canonical_names(self, name):
        assert _resolve_fold_approach(name) == name

    def test_invalid_approach_raises(self):
        with pytest.raises(ValueError, match="fold_approach"):
            _resolve_fold_approach("alphafold")

    def test_valid_set_contains_three(self):
        assert set(_VALID_FOLD_APPROACHES) == {
            "boltz2_affinity", "esmfold2", "esmfold2_fast",
        }


class TestFoldBodyApproachWiring:
    def test_default_body_uses_boltz2_affinity(self):
        body = _fold_body(["MPEPTIDESEQ"])
        assert body["model"] == "boltz2_affinity"
        assert body["foldApproach"] == "boltz2_affinity"
        assert body["fold_approach"] == "boltz2_affinity"

    def test_explicit_esmfold2(self):
        body = _fold_body(["MPEPTIDESEQ"], fold_approach="esmfold2")
        assert body["model"] == "esmfold2"
        assert body["foldApproach"] == "esmfold2"

    def test_esmfold2_fast(self):
        body = _fold_body(["MPEPTIDESEQ"], fold_approach="esmfold2_fast")
        assert body["model"] == "esmfold2_fast"

    def test_num_seeds_overrides_trajectories(self):
        body = _fold_body(["MPEPTIDESEQ"], num_seeds=8, num_trajectories=2)
        assert body["diffusionSamples"] == 8
        assert body["numSeeds"] == 8
        assert body["num_seeds"] == 8

    def test_num_recycles_round_trip(self):
        body = _fold_body(["MPEPTIDESEQ"], fold_approach="esmfold2",
                          num_recycles=3)
        assert body["numRecycles"] == 3
        assert body["num_recycles"] == 3

    def test_return_pdb_true(self):
        body = _fold_body(["MPEPTIDESEQ"], return_pdb=True)
        assert body["returnPdb"] is True
        assert body["return_pdb"] is True

    def test_return_pdb_false(self):
        body = _fold_body(["MPEPTIDESEQ"], return_pdb=False)
        assert body["returnPdb"] is False

    def test_no_new_keys_when_not_set(self):
        body = _fold_body(["MPEPTIDESEQ"])
        # Trajectory key always present, but seeds/recycles/pdb only when set.
        for k in ("numSeeds", "num_seeds", "numRecycles", "num_recycles",
                  "returnPdb", "return_pdb"):
            assert k not in body, f"Unexpected key {k!r} in default body"
