# Copyright © 2026 Ligandal, Inc. All rights reserved.
"""Unit tests for the v0.2.0 paid-only peptides surface.

Covers:
  - ``Peptides.by_gene`` request shape + GeneSummary parsing
  - ``Peptides.list`` request shape
  - ``Peptides.get`` thin response, include= gating, validation
  - Client-side paid-tier rejection raising LigandAIPaidTierRequired
  - Cysteine controls via ``extra={...}`` emit DeprecationWarning
  - Cysteine controls via typed kwargs produce identical wire bodies (snapshot)
"""

from __future__ import annotations

import warnings

import pytest
from pytest_httpx import HTTPXMock

from ligandai import (
    GeneSummary,
    LigandAI,
    LigandAIPaidTierRequired,
    PeptideDetail,
)

BASE = "http://api.ligandai.test"


@pytest.fixture
def pro_client() -> LigandAI:
    return LigandAI(api_key="lgai_pro_test", base_url=BASE, max_retries=1)


@pytest.fixture
def free_client() -> LigandAI:
    return LigandAI(api_key="lgai_free_test", base_url=BASE, max_retries=1)


# ---------- by_gene -------------------------------------------------------


def test_by_gene_parses_gene_summary(httpx_mock: HTTPXMock, pro_client: LigandAI) -> None:
    httpx_mock.add_response(
        url=f"{BASE}/api/v1/peptides/by-gene?limit=50&offset=0",
        json={
            "success": True,
            "rows": [
                {
                    "gene": "GRIN1",
                    "foldedCount": 1211,
                    "eliteCount": 132,
                    "greatPlusCount": 410,
                    "bestIpsae": 0.94,
                    "bestDeltaforgeDg": -7.6,
                    "bestDeltaforgeV10Dg": -9.4,
                    "deltaforgeV10ScoredCount": 18,
                    "deltaforgeV10ScorerVersion": "v10_boltz2_gbr_2026_03_29",
                    "sessionCount": 12,
                    "programCount": 3,
                    "lastActivityAt": "2026-04-30T12:00:00Z",
                },
            ],
            "count": 1,
            "total": 1,
        },
    )
    rows = pro_client.peptides.by_gene()
    assert len(rows) == 1
    r = rows[0]
    assert isinstance(r, GeneSummary)
    assert r.gene == "GRIN1"
    assert r.folded_count == 1211
    assert r.elite_count == 132
    assert r.great_plus_count == 410
    assert r.best_ipsae == pytest.approx(0.94)
    assert r.best_deltaforge_dg == pytest.approx(-7.6)
    assert r.best_deltaforge_v10_dg == pytest.approx(-9.4)
    assert r.deltaforge_v10_scored_count == 18
    assert r.deltaforge_v10_scorer_version == "v10_boltz2_gbr_2026_03_29"


def test_by_gene_forwards_filter_params(httpx_mock: HTTPXMock, pro_client: LigandAI) -> None:
    httpx_mock.add_response(
        url=f"{BASE}/api/v1/peptides/by-gene?limit=20&offset=10&genes=EGFR%2CKCNJ4&minIpsae=0.66&programId=42",
        json={"success": True, "rows": [], "count": 0, "total": 0},
    )
    pro_client.peptides.by_gene(
        genes=["EGFR", "kcnj4"],  # case-insensitive: SDK upper-cases
        min_ipsae=0.66,
        program_id=42,
        limit=20,
        offset=10,
    )


def test_by_gene_empty_response(httpx_mock: HTTPXMock, pro_client: LigandAI) -> None:
    httpx_mock.add_response(
        url=f"{BASE}/api/v1/peptides/by-gene?limit=50&offset=0",
        json={"success": True, "rows": [], "count": 0, "total": 0},
    )
    assert pro_client.peptides.by_gene() == []


# ---------- list ----------------------------------------------------------


def test_list_returns_peptides(httpx_mock: HTTPXMock, pro_client: LigandAI) -> None:
    # v0.5.0: peptides.list now hits /api/v1/peptides/list (richer schema, accepts program_id)
    httpx_mock.add_response(
        url=f"{BASE}/api/v1/peptides/list?limit=5&offset=0&gene=GRIN1",
        json={
            "peptides": [
                {"sequence": "ACDEFG", "targetGene": "GRIN1", "ipsae": 0.91, "foldId": "123"},
                {"sequence": "HIKLMN", "targetGene": "GRIN1", "ipsae": 0.85, "foldId": "124"},
            ],
            "total": 2,
            "limit": 5,
            "offset": 0,
            "_tier": "pro",
            "_tier_redacted": False,
        },
    )
    peptides = pro_client.peptides.list("GRIN1", limit=5)
    assert len(peptides) == 2
    assert peptides[0].sequence == "ACDEFG"
    assert peptides[0].fold_id == "123"


def test_list_by_program_id_positional(httpx_mock: HTTPXMock, pro_client: LigandAI) -> None:
    """v0.5.0: peptides.list(program_id_int) works (a user's TypeError fix)."""
    httpx_mock.add_response(
        url=f"{BASE}/api/v1/peptides/list?limit=10&offset=0&program_id=42",
        json={
            "peptides": [
                {"sequence": "ACDEFG", "targetGene": "EGFR", "ipsae": 0.92, "peptide_id": 100, "foldId": "100"},
            ],
            "total": 1,
            "limit": 10,
            "offset": 0,
            "_tier": "pro",
            "_tier_redacted": False,
        },
    )
    peptides = pro_client.peptides.list(42, limit=10)
    assert len(peptides) == 1
    assert peptides[0].peptide_id == 100


def test_list_rejects_empty_gene(pro_client: LigandAI) -> None:
    with pytest.raises(ValueError):
        pro_client.peptides.list("")
    with pytest.raises(ValueError):
        pro_client.peptides.list("   ")


# ---------- search --------------------------------------------------------


def test_search_forwards_super_elite_affinity_param(
    httpx_mock: HTTPXMock, pro_client: LigandAI
) -> None:
    httpx_mock.add_response(
        url=f"{BASE}/api/v1/peptides/search?limit=5&offset=0&sort=ipsae&order=desc&super_elite_affinity=true",
        json={"peptides": [], "total": 0},
    )

    assert pro_client.peptides.search(super_elite_affinity=True, limit=5) == []


def test_search_accepts_deprecated_super_elite_thermo_alias(
    httpx_mock: HTTPXMock, pro_client: LigandAI
) -> None:
    httpx_mock.add_response(
        url=f"{BASE}/api/v1/peptides/search?limit=5&offset=0&sort=ipsae&order=desc&super_elite_affinity=true",
        json={"peptides": [], "total": 0},
    )

    with pytest.warns(DeprecationWarning, match="super_elite_thermo is deprecated"):
        assert pro_client.peptides.search(super_elite_thermo=True, limit=5) == []


# ---------- get -----------------------------------------------------------


def test_get_thin_response(httpx_mock: HTTPXMock, pro_client: LigandAI) -> None:
    httpx_mock.add_response(
        url=f"{BASE}/api/v1/peptides/777",
        json={
            "id": 777,
            "gene": "GRIN1",
            "sessionId": "sess_abc",
            "sequence": "ACDEFGHIK",
            "conformation": "monomer_A",
            "ipsae": 0.92,
            "ptm": 0.81,
            "iptm": 0.88,
            "plddt": 89.5,
            "deltaG": -6.4,
            "predictedKd": 0.0023,
            "predictedBinder": True,
            "predictedBinderCall": "binder",
            "predictedBinderLabel": "Predicted binder",
            "binderCallMethod": "deterministic_joint_ipSAE_ipTM_DeltaForge_gate",
            "deltaforgeV10": {
                "delta_g": -9.25,
                "kd_nm": 166.0,
                "predicted_binder": True,
                "predicted_binder_call": "binder",
                "scorer": "deltaforge_v10_boltz2_calibrated_gbr",
                "scorer_version": "v10_boltz2_gbr_2026_03_29",
                "best_pair": {
                    "receptor_chain": "A",
                    "peptide_chain": "B",
                    "delta_g": -9.25,
                    "kd_nm": 166.0,
                },
                "pair_scores": [
                    {
                        "receptor_chain": "A",
                        "peptide_chain": "B",
                        "delta_g": -9.25,
                        "kd_nm": 166.0,
                        "contacts": 42,
                    }
                ],
            },
            "createdAt": "2026-04-30T12:00:00Z",
        },
    )
    detail = pro_client.peptides.get(777)
    assert isinstance(detail, PeptideDetail)
    assert detail.id == 777
    assert detail.gene == "GRIN1"
    assert detail.session_id == "sess_abc"
    assert detail.sequence == "ACDEFGHIK"
    assert detail.predicted_binder is True
    assert detail.predicted_binder_call == "binder"
    assert detail.deltaforge_v10 is not None
    assert detail.deltaforge_v10.predicted_binder is True
    assert detail.deltaforge_v10.predicted_binder_call == "binder"
    assert detail.deltaforge_v10.scorer_version == "v10_boltz2_gbr_2026_03_29"
    assert detail.deltaforge_v10.best_pair is not None
    assert detail.deltaforge_v10.best_pair.receptor_chain == "A"
    assert detail.deltaforge_v10.pair_scores is not None
    assert detail.deltaforge_v10.pair_scores[0].contacts == 42
    # Heavy fields default to None when not requested
    assert detail.pocket_features_48_dim is None
    assert detail.pocket_features_metadata is None
    assert detail.peptide_per_receptor is None
    assert detail.disulfide_analysis is None
    assert detail.pdb_content is None


def test_get_with_pocket_features(httpx_mock: HTTPXMock, pro_client: LigandAI) -> None:
    httpx_mock.add_response(
        url=f"{BASE}/api/v1/peptides/777?include=pocket_features",
        json={
            "id": 777,
            "gene": "GRIN1",
            "sessionId": "sess_abc",
            "sequence": "ACDEFGHIK",
            "conformation": None,
            "ipsae": 0.92,
            "ptm": None,
            "iptm": None,
            "plddt": None,
            "deltaG": None,
            "predictedKd": None,
            "createdAt": "2026-04-30T12:00:00Z",
            "pocketFeatures48Dim": [[0.1] * 48, [0.2] * 48],
            "pocketFeaturesMetadata": {
                "pocket_residue_count": 2,
                "targeted": True,
                "conformation_name": "complex_A+B",
            },
        },
    )
    detail = pro_client.peptides.get(777, include=["pocket_features"])
    assert detail.pocket_features_48_dim == [[0.1] * 48, [0.2] * 48]
    assert detail.pocket_features_metadata is not None
    assert detail.pocket_features_metadata["targeted"] is True


def test_get_with_pdb(httpx_mock: HTTPXMock, pro_client: LigandAI) -> None:
    httpx_mock.add_response(
        url=f"{BASE}/api/v1/peptides/777?include=pdb",
        json={
            "id": 777,
            "gene": "GRIN1",
            "sessionId": "sess_abc",
            "sequence": "ACDE",
            "ipsae": 0.5,
            "createdAt": "2026-04-30T12:00:00Z",
            "pdbContent": "HEADER    PEPTIDE\nATOM      1  N   ALA A   1\n",
        },
    )
    detail = pro_client.peptides.get(777, include=["pdb"])
    assert detail.pdb_content is not None
    assert "ATOM" in detail.pdb_content


def test_get_rejects_invalid_id(pro_client: LigandAI) -> None:
    with pytest.raises(ValueError):
        pro_client.peptides.get(0)
    with pytest.raises(ValueError):
        pro_client.peptides.get(-1)
    with pytest.raises(ValueError):
        pro_client.peptides.get("not-a-number")


def test_get_rejects_unknown_include(pro_client: LigandAI) -> None:
    with pytest.raises(ValueError, match="Unknown include value"):
        pro_client.peptides.get(1, include=["bogus"])  # type: ignore[list-item]


# ---------- DeltaForge V10 / raw PDB scoring -----------------------------


def test_score_complex_forwards_deltaforge_scorer(
    httpx_mock: HTTPXMock, pro_client: LigandAI
) -> None:
    httpx_mock.add_response(
        url=f"{BASE}/api/binder-scoring/fold-and-score",
        json={"jobId": "score_job_1"},
    )
    job = pro_client.peptides.score_complex(
        binder_sequence="ACDEFG",
        target_sequence="HIKLMNP",
        scorer="v10",
    )
    assert job.id == "score_job_1"
    request = httpx_mock.get_request()
    assert request is not None
    import json as _json
    body = _json.loads(request.read())
    assert body["scorer"] == "v10"


def test_score_pdb_posts_raw_pdb_and_parses_v10_decomposition(
    httpx_mock: HTTPXMock, pro_client: LigandAI
) -> None:
    httpx_mock.add_response(
        url=f"{BASE}/api/v1/deltaforge/score-pdb",
        json={
            "success": True,
            "delta_g": -9.25,
            "kd_nm": 166.0,
            "scorer": "deltaforge_v10_boltz2_calibrated_gbr",
            "scorer_version": "v10_boltz2_gbr_2026_03_29",
            "predicted_affinity_tier": "sub_uM",
            "predicted_binder": False,
            "predicted_binder_call": "not_binder",
            "predicted_binder_label": "Not predicted binder",
            "binder_call_method": "deterministic_joint_ipSAE_ipTM_DeltaForge_gate",
            "predicted_non_binder_reasons": ["ipSAE < 0.67"],
            "missing_binder_gate_inputs": [],
            "structural_energy_gates": {
                "predicted_binder": False,
                "predicted_binder_call": "not_binder",
                "failed_gate_reasons": ["ipSAE < 0.67"],
                "missing_gate_inputs": [],
            },
            "model_sha256": "sha",
            "aggregate_method": "boltzmann_parallel",
            "best_pair": {
                "receptor_chain": "A",
                "peptide_chain": "B",
                "delta_g": -9.25,
                "kd_nm": 166.0,
            },
            "pair_scores": [
                {
                    "receptor_chain": "A",
                    "peptide_chain": "B",
                    "delta_g": -9.25,
                    "kd_nm": 166.0,
                    "contacts": 42,
                }
            ],
        },
    )

    score = pro_client.peptides.score_pdb(
        pdb_content="ATOM      1  CA  ALA A   1       0.0     0.0     0.0  1.00 85.00           C\n",
        receptor_chains=["A"],
        peptide_chain="B",
        scorer="v10",
        fold_ipsae=0.51,
        fold_iptm=0.84,
        fold_complex_plddt=91.2,
    )

    assert score.dg == pytest.approx(-9.25)
    assert score.kd_nm == pytest.approx(166.0)
    assert score.predicted_binder is False
    assert score.predicted_binder_call == "not_binder"
    assert score.predicted_affinity_tier == "sub_uM"
    assert score.predicted_non_binder_reasons == ["ipSAE < 0.67"]
    assert score.structural_energy_gates is not None
    assert score.structural_energy_gates.predicted_binder is False
    assert score.scorer_version == "v10_boltz2_gbr_2026_03_29"
    assert score.best_pair is not None
    assert score.best_pair.receptor_chain == "A"
    assert score.pair_scores is not None
    assert score.pair_scores[0].contacts == 42

    request = httpx_mock.get_request()
    assert request is not None
    import json as _json
    body = _json.loads(request.read())
    assert body["receptorChains"] == ["A"]
    assert body["peptideChain"] == "B"
    assert body["scorer"] == "v10"
    assert body["foldIpsae"] == 0.51
    assert body["foldIptm"] == 0.84
    assert body["foldComplexPlddt"] == 91.2


# ---------- tier-open peptide reads --------------------------------------


def test_free_tier_by_gene_uses_flexible_api(httpx_mock: HTTPXMock, free_client: LigandAI) -> None:
    """v0.5.3: free users can read their own aggregate peptide rows."""
    httpx_mock.add_response(
        url=f"{BASE}/api/v1/peptides/by-gene?limit=50&offset=0",
        json={"success": True, "rows": [], "count": 0, "total": 0, "_tier": "free"},
    )
    assert free_client.peptides.by_gene() == []


def test_free_tier_raises_paid_tier_required_on_get(free_client: LigandAI) -> None:
    with pytest.raises(LigandAIPaidTierRequired):
        free_client.peptides.get(1)


def test_pro_tier_passes_paid_check(httpx_mock: HTTPXMock, pro_client: LigandAI) -> None:
    httpx_mock.add_response(
        url=f"{BASE}/api/v1/peptides/by-gene?limit=50&offset=0",
        json={"success": True, "rows": [], "count": 0, "total": 0},
    )
    # Should not raise — pro is allowed.
    pro_client.peptides.by_gene()


def test_server_402_upgrade_required_maps_to_paid_tier_error(
    httpx_mock: HTTPXMock,
) -> None:
    """When the SDK can't infer tier locally (anonymous key), the server's
    402 must surface as LigandAIPaidTierRequired via error_from_response."""
    # Use a custom-prefix key so _detect_tier returns None and the server
    # response is the source of truth.
    client = LigandAI(api_key="custom_unknown_prefix", base_url=BASE, max_retries=1)
    httpx_mock.add_response(
        url=f"{BASE}/api/v1/peptides/by-gene?limit=50&offset=0",
        status_code=402,
        json={
            "error": "upgrade_required",
            "message": "Upgrade!",
            "tier_required": "pro",
            "current_tier": "free",
        },
    )
    with pytest.raises(LigandAIPaidTierRequired) as excinfo:
        client.peptides.by_gene()
    assert excinfo.value.required_tier == "pro"
    assert excinfo.value.current_tier == "free"


# ---------- Cysteine extra={} deprecation -------------------------------


def test_cys_via_extra_emits_deprecation_warning(
    httpx_mock: HTTPXMock, pro_client: LigandAI
) -> None:
    httpx_mock.add_response(
        url=f"{BASE}/api/ptf/parallel/generate",
        json={"sessionId": "sess_test"},
    )
    with warnings.catch_warnings(record=True) as caught:
        warnings.simplefilter("always")
        # Pass cys_mode through **extra (legacy path)
        pro_client.peptides.generate(
            gene="EGFR",
            num_peptides=10,
            cys_mode="allow_all",  # leaks into extra
        )
    deprecation = [w for w in caught if issubclass(w.category, DeprecationWarning)]
    assert deprecation, "Expected DeprecationWarning for cys_mode via extra"
    assert "cys_mode" in str(deprecation[0].message)


def test_cys_via_typed_kwarg_does_not_warn(
    httpx_mock: HTTPXMock, pro_client: LigandAI
) -> None:
    httpx_mock.add_response(
        url=f"{BASE}/api/ptf/parallel/generate",
        json={"sessionId": "sess_test"},
    )
    with warnings.catch_warnings(record=True) as caught:
        warnings.simplefilter("always")
        pro_client.peptides.generate(
            gene="EGFR",
            num_peptides=10,
            cysteine_mode="allow_all",  # blessed typed kwarg
        )
    deprecation = [
        w for w in caught
        if issubclass(w.category, DeprecationWarning)
        and "cys" in str(w.message).lower()
    ]
    assert not deprecation, "Typed cysteine_mode kwarg should NOT warn"


def test_cys_typed_kwargs_produce_expected_wire_body(
    httpx_mock: HTTPXMock, pro_client: LigandAI
) -> None:
    """Snapshot test — server contract for cysteine controls."""
    httpx_mock.add_response(
        url=f"{BASE}/api/ptf/parallel/generate",
        json={"sessionId": "sess_test"},
    )
    pro_client.peptides.generate(
        gene="EGFR",
        num_peptides=10,
        cysteine_mode="allow_all",
        cyclic_mode="disulfide",
        cyclic_strength=2.5,
        strict_recombinant=True,
    )
    request = httpx_mock.get_request()
    assert request is not None
    import json as _json
    body = _json.loads(request.read())
    # cysteine policy ends up under cysteineMode (camelCase, server contract)
    assert body.get("cysteineMode") == "allow_all"
    # cyclic block
    assert body.get("cyclicMode") == "disulfide"
    assert body.get("cyclicStrength") == 2.5
    assert body.get("strictRecombinant") is True


def test_generate_forwards_ptf_fold_controls(
    httpx_mock: HTTPXMock, pro_client: LigandAI
) -> None:
    httpx_mock.add_response(
        url=f"{BASE}/api/ptf/parallel/generate",
        json={"sessionId": "sess_test"},
    )
    pro_client.peptides.generate(
        gene="IL31",
        num_peptides=25,
        auto_fold=True,
        top_n_fold=5,
        fold_gpus=2,
        folding_mode="parallel",
        fold_strategy="ensemble",
        folding_conformations=["generation", "apo"],
        max_folds_per_target=7,
        enable_expansion=False,
        auto_conformation_expansion=False,
        clash_resolution_enabled=False,
        md_relaxation_enabled=True,
        num_trajectories=4,
    )

    request = httpx_mock.get_request()
    assert request is not None
    import json as _json

    body = _json.loads(request.read())
    assert body["autoFoldEnabled"] is True
    assert body["maxFoldsPerTarget"] == 7
    assert body["foldingGpus"] == 2
    assert body["foldingMode"] == "parallel"
    assert body["foldStrategy"] == "ensemble"
    assert body["foldingConformations"] == ["generation", "apo"]
    assert body["enableExpansion"] is False
    assert body["autoConformationExpansion"] is False
    assert body["clashResolutionEnabled"] is False
    assert body["mdRelaxationEnabled"] is True
    assert body["numTrajectories"] == 4
    assert body["diffusionSamples"] == 4
