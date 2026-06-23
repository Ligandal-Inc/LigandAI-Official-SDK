# Copyright © 2026 Ligandal, Inc. All rights reserved.
"""Unit tests for the `analysis` resource (0.7.4).

Covers request shape + response parsing for:
  - ``client.analysis.interface_residues``  → POST /api/analysis/interface
  - ``client.analysis.immunogenicity``      → POST /api/analysis/immunogenicity
  - ``client.analysis.batch_immunogenicity``→ POST /api/analysis/immunogenicity/batch
  - ``client.analysis.score_expression``    → POST /api/synthesis/score-expression
  - sequence validation
  - the resource is registered on both sync + async clients
"""

from __future__ import annotations

import json as _json

import pytest
from pytest_httpx import HTTPXMock

from ligandai import AsyncLigandAI, LigandAI

BASE = "http://api.ligandai.test"

_PDB = "ATOM      1  N   MET A   1      0.000   0.000   0.000  1.00  0.00           N\n"


@pytest.fixture
def pro_client() -> LigandAI:
    return LigandAI(api_key="lgai_pro_test", base_url=BASE, max_retries=1)


def test_analysis_resource_registered(pro_client: LigandAI) -> None:
    assert hasattr(pro_client, "analysis")
    for m in ("interface_residues", "immunogenicity", "batch_immunogenicity", "score_expression"):
        assert hasattr(pro_client.analysis, m), m
    async_client = AsyncLigandAI(api_key="lgai_pro_test", base_url=BASE)
    assert hasattr(async_client, "analysis")
    for m in ("interface_residues", "immunogenicity", "batch_immunogenicity", "score_expression"):
        assert hasattr(async_client.analysis, m), m


def test_interface_residues_request_and_parse(
    httpx_mock: HTTPXMock, pro_client: LigandAI
) -> None:
    httpx_mock.add_response(
        url=f"{BASE}/api/analysis/interface",
        json={
            "chain_a": "A",
            "chain_b": "P",
            "chain_a_residues": [12, 14, 16],
            "chain_b_residues": [3, 5],
            "distance_cutoff_used": 8.0,
            "contact_definition": "heavy_atom_8A",
        },
    )
    out = pro_client.analysis.interface_residues(_PDB, "A", "P", distance_cutoff=5.0)
    assert out["chain_a_residues"] == [12, 14, 16]
    assert out["chain_b_residues"] == [3, 5]
    assert out["contact_definition"] == "heavy_atom_8A"

    body = _json.loads(httpx_mock.get_request().read())
    assert body["pdb_content"] == _PDB
    assert body["chain_a"] == "A"
    assert body["chain_b"] == "P"
    assert body["distance_cutoff"] == 5.0


def test_immunogenicity_request_and_parse(
    httpx_mock: HTTPXMock, pro_client: LigandAI
) -> None:
    httpx_mock.add_response(
        url=f"{BASE}/api/analysis/immunogenicity",
        json={
            "sequence": "MKFLILLFNILCLFPVLA",
            "species": ["human"],
            "threshold": 0.3,
            "score": 0.12,
            "passes": True,
            "per_species": [{"species": "human", "score": 0.12}],
            "recommendations": [],
        },
    )
    out = pro_client.analysis.immunogenicity("mkflillfnilclfpvla")
    assert out["passes"] is True
    assert out["score"] == pytest.approx(0.12)

    body = _json.loads(httpx_mock.get_request().read())
    # sequence is upper-cased + stripped by the client
    assert body["sequence"] == "MKFLILLFNILCLFPVLA"
    assert body["species"] == ["human"]
    assert body["threshold"] == 0.3
    assert body["include_positions"] is False


def test_batch_immunogenicity_unwraps_scores(
    httpx_mock: HTTPXMock, pro_client: LigandAI
) -> None:
    httpx_mock.add_response(
        url=f"{BASE}/api/analysis/immunogenicity/batch",
        json={
            "scores": [
                {"sequence": "ACDEFGHIK", "score": 0.1, "passes": True},
                {"sequence": "LMNPQRSTV", "score": 0.4, "passes": False},
            ]
        },
    )
    out = pro_client.analysis.batch_immunogenicity(["ACDEFGHIK", "LMNPQRSTV"])
    assert isinstance(out, list)
    assert len(out) == 2
    assert out[1]["passes"] is False

    body = _json.loads(httpx_mock.get_request().read())
    assert body["sequences"] == ["ACDEFGHIK", "LMNPQRSTV"]


def test_score_expression_single_to_list(
    httpx_mock: HTTPXMock, pro_client: LigandAI
) -> None:
    httpx_mock.add_response(
        url=f"{BASE}/api/synthesis/score-expression",
        json={"results": [{"sequence": "AKEFSKLIRYNANPVRGQKQ"}], "ranking": []},
    )
    out = pro_client.analysis.score_expression("AKEFSKLIRYNANPVRGQKQ")
    assert "results" in out

    body = _json.loads(httpx_mock.get_request().read())
    # a single sequence is normalized to a one-element list
    assert body["sequences"] == ["AKEFSKLIRYNANPVRGQKQ"]
    assert body["include_bli"] is True


def test_invalid_sequence_rejected(pro_client: LigandAI) -> None:
    with pytest.raises(ValueError):
        pro_client.analysis.immunogenicity("MKFL123")
    with pytest.raises(ValueError):
        pro_client.analysis.batch_immunogenicity([])


def test_interface_requires_chains_and_pdb(pro_client: LigandAI) -> None:
    with pytest.raises(ValueError):
        pro_client.analysis.interface_residues("", "A", "P")
    with pytest.raises(ValueError):
        pro_client.analysis.interface_residues(_PDB, "", "P")
