# Copyright © 2026 Ligandal, Inc. All rights reserved.
"""Tests for ligandai.resources.folds (Stream D / W4)."""

from __future__ import annotations

import pytest
from pytest_httpx import HTTPXMock

from ligandai import LigandAI

BASE = "http://api.ligandai.test"


@pytest.fixture
def client() -> LigandAI:
    return LigandAI(api_key="lgai_pro_test123", base_url=BASE, max_retries=1)


def test_partition_by_hotspot(httpx_mock: HTTPXMock, client: LigandAI) -> None:
    httpx_mock.add_response(
        url=f"{BASE}/api/folds/partition-by-hotspot",
        method="POST",
        json={
            "session_id": "ptf_test",
            "distance_threshold_a": 5.0,
            "passes_hotspot": [
                {
                    "fold_id": 1,
                    "gene": "EGFR",
                    "sequence": "ACDEFGHIKL",
                    "matched_hotspots": [{"chain": "C", "residue": 148, "boltz_residue": 130, "min_distance_a": 3.2}],
                    "min_distance_a": 3.2,
                },
            ],
            "passes_pocket": [],
            "wrong_interface": [
                {"fold_id": 2, "gene": "EGFR", "sequence": "MNPQRS", "reason": "no_match"},
            ],
            "stats": {
                "total": 2,
                "passes_hotspot": 1,
                "passes_pocket": 0,
                "wrong_interface": 1,
                "unscored": 0,
            },
        },
    )
    result = client.folds.partition_by_hotspot(
        session_id="ptf_test",
        hotspots=[{"chain": "C", "residue": 148, "numbering": "pdb"}],
        distance_threshold_a=5.0,
    )
    assert result["stats"]["total"] == 2
    assert result["stats"]["passes_hotspot"] == 1
    assert result["passes_hotspot"][0]["fold_id"] == 1
    assert result["passes_hotspot"][0]["matched_hotspots"][0]["residue"] == 148


def test_partition_by_hotspot_with_pocket(httpx_mock: HTTPXMock, client: LigandAI) -> None:
    httpx_mock.add_response(
        url=f"{BASE}/api/folds/partition-by-hotspot",
        method="POST",
        json={
            "session_id": "s",
            "passes_hotspot": [],
            "passes_pocket": [{"fold_id": 5, "gene": "X", "sequence": "AAAA"}],
            "wrong_interface": [],
            "stats": {"total": 1, "passes_hotspot": 0, "passes_pocket": 1, "wrong_interface": 0, "unscored": 0},
        },
    )
    result = client.folds.partition_by_hotspot(
        session_id="s",
        hotspots=[],
        pocket_residues=[
            {"chain": "A", "residue": 145},
            {"chain": "A", "residue": 146},
        ],
    )
    assert result["stats"]["passes_pocket"] == 1


def test_expand_hotspot(httpx_mock: HTTPXMock, client: LigandAI) -> None:
    httpx_mock.add_response(
        url=f"{BASE}/api/folds/expand-hotspot?session_id=ptf_test&chain=C&residue=148&radius_a=8.0",
        method="GET",
        json={
            "session_id": "ptf_test",
            "fold_id": 9,
            "chain": "C",
            "hotspot_residue": 148,
            "radius_a": 8.0,
            "pocket_residues": [
                {"chain": "C", "residue": 145, "resname": "VAL", "distance_a": 3.8},
                {"chain": "C", "residue": 150, "resname": "GLY", "distance_a": 6.2},
            ],
            "n_pocket_residues": 2,
        },
    )
    out = client.folds.expand_hotspot(
        session_id="ptf_test",
        chain="C",
        residue=148,
        radius_a=8.0,
    )
    assert out["n_pocket_residues"] == 2
    assert out["pocket_residues"][0]["distance_a"] == 3.8


def test_expand_hotspot_default_radius(httpx_mock: HTTPXMock, client: LigandAI) -> None:
    httpx_mock.add_response(
        url=f"{BASE}/api/folds/expand-hotspot?session_id=s&chain=A&residue=10&radius_a=8.0",
        method="GET",
        json={
            "session_id": "s",
            "chain": "A",
            "hotspot_residue": 10,
            "radius_a": 8.0,
            "pocket_residues": [],
            "n_pocket_residues": 0,
        },
    )
    out = client.folds.expand_hotspot(session_id="s", chain="A", residue=10)
    assert out["radius_a"] == 8.0
