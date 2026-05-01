# Copyright © 2025 Ligandal, Inc. All rights reserved.
"""Unit tests for the v0.2.0 paid-only peptides surface (LIGANDAI_ALPHA_V2-afspr).

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
    httpx_mock.add_response(
        url=f"{BASE}/api/ptf/generated-peptides/by-gene/GRIN1?limit=5&offset=0",
        json=[
            {"sequence": "ACDEFG", "targetGene": "GRIN1", "ipsae": 0.91, "foldId": "123"},
            {"sequence": "HIKLMN", "targetGene": "GRIN1", "ipsae": 0.85, "foldId": "124"},
        ],
    )
    peptides = pro_client.peptides.list("GRIN1", limit=5)
    assert len(peptides) == 2
    assert peptides[0].sequence == "ACDEFG"
    assert peptides[0].fold_id == "123"


def test_list_rejects_empty_gene(pro_client: LigandAI) -> None:
    with pytest.raises(ValueError):
        pro_client.peptides.list("")
    with pytest.raises(ValueError):
        pro_client.peptides.list("   ")


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
            "createdAt": "2026-04-30T12:00:00Z",
        },
    )
    detail = pro_client.peptides.get(777)
    assert isinstance(detail, PeptideDetail)
    assert detail.id == 777
    assert detail.gene == "GRIN1"
    assert detail.session_id == "sess_abc"
    assert detail.sequence == "ACDEFGHIK"
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


# ---------- paid-tier validation -----------------------------------------


def test_free_tier_raises_paid_tier_required_on_by_gene(free_client: LigandAI) -> None:
    """Client-side fail-fast — no network call is made for free keys."""
    with pytest.raises(LigandAIPaidTierRequired) as excinfo:
        free_client.peptides.by_gene()
    assert excinfo.value.current_tier == "free"
    assert excinfo.value.required_tier == "pro"


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
