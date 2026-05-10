# Copyright © 2026 Ligandal, Inc. All rights reserved.
"""Tests for ligandai.resources.folds (Stream D / W4 + b73dt phase C δ)."""

from __future__ import annotations

import io

import numpy as np
import pytest
from pytest_httpx import HTTPXMock

from ligandai import LigandAI
from ligandai.errors import LigandAITierError

BASE = "http://api.ligandai.test"


@pytest.fixture
def client() -> LigandAI:
    return LigandAI(api_key="lgai_pro_test123", base_url=BASE, max_retries=1)


@pytest.fixture
def free_client() -> LigandAI:
    return LigandAI(api_key="lgai_free_test123", base_url=BASE, max_retries=1)


@pytest.fixture
def academia_client() -> LigandAI:
    return LigandAI(api_key="lgai_edu_test123", base_url=BASE, max_retries=1)


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


# -- PAE download / summary (b73dt phase C δ) --------------------------------


def _uint8_npy_bytes(arr: np.ndarray) -> bytes:
    """Serialize a uint8 ndarray to a .npy buffer the way the server does."""
    buf = io.BytesIO()
    np.save(buf, arr, allow_pickle=False)
    return buf.getvalue()


def test_download_pae_tier_gate_blocks_free(free_client: LigandAI) -> None:
    """Free tier raises LigandAITierError without making the HTTP call."""
    with pytest.raises(LigandAITierError) as excinfo:
        free_client.folds.download_pae(123)
    assert excinfo.value.required_tier == "academia"


def test_download_pae_decodes_to_float32_angstroms(
    httpx_mock: HTTPXMock, academia_client: LigandAI
) -> None:
    """uint8 .npy bytes are decoded to a float32 ndarray scaled to Å."""
    n = 4
    arr = np.array(
        [[0, 64, 128, 192], [255, 0, 32, 64], [16, 48, 80, 112], [200, 160, 120, 80]],
        dtype=np.uint8,
    )
    payload = _uint8_npy_bytes(arr)
    scale = 32.0 / 255.0
    httpx_mock.add_response(
        url=f"{BASE}/api/v1/folds/9001/pae",
        method="GET",
        content=payload,
        headers={
            "Content-Type": "application/octet-stream",
            "X-Pae-Scale-Angstrom": str(scale),
        },
    )
    pae = academia_client.folds.download_pae(9001)
    assert isinstance(pae, np.ndarray)
    assert pae.shape == (n, n)
    assert pae.dtype == np.float32
    np.testing.assert_allclose(pae, arr.astype(np.float32) * scale, rtol=1e-6)


def test_download_pae_raw_bytes_when_decode_false(
    httpx_mock: HTTPXMock, academia_client: LigandAI
) -> None:
    arr = np.zeros((2, 2), dtype=np.uint8)
    payload = _uint8_npy_bytes(arr)
    httpx_mock.add_response(
        url=f"{BASE}/api/v1/folds/9002/pae",
        method="GET",
        content=payload,
        headers={"Content-Type": "application/octet-stream"},
    )
    raw = academia_client.folds.download_pae(9002, decode=False)
    assert isinstance(raw, bytes)
    assert raw == payload


def test_get_pae_summary_open_to_free_tier(
    httpx_mock: HTTPXMock, free_client: LigandAI
) -> None:
    """PAE summary is open to all tiers (no client-side gate)."""
    body = {
        "shape": [128, 128],
        "min": 0.5,
        "max": 31.4,
        "mean": 8.7,
        "p95": 22.1,
        "per_chain_pair_max": {"A_B": 12.3, "A_C": 31.4},
        "scale_angstrom_per_unit": 0.12549019607843137,
    }
    httpx_mock.add_response(
        url=f"{BASE}/api/v1/folds/7777/pae/summary",
        method="GET",
        json=body,
    )
    out = free_client.folds.get_pae_summary(7777)
    assert out["shape"] == [128, 128]
    assert out["per_chain_pair_max"]["A_C"] == 31.4
    assert out["mean"] == 8.7


def test_peptide_per_chain_iptm_field_round_trips() -> None:
    """Peptide accepts per-chain iPTM + fold_metric_details from server alias."""
    from ligandai.types import Peptide

    p = Peptide.model_validate(
        {
            "sequence": "ACDEFGHIKL",
            "peptideInterfaceIptm": 0.82,
            "chainPairIptm": {"A_B": 0.91, "A_C": 0.78},
            "foldMetricDetails": {"overall": {"iptm": 0.85}, "perChain": {"A": {"plddt": 88.2}}},
        }
    )
    assert p.peptide_interface_iptm == 0.82
    assert p.chain_pair_iptm == {"A_B": 0.91, "A_C": 0.78}
    assert p.fold_metric_details["perChain"]["A"]["plddt"] == 88.2


def test_fold_result_pae_matrix_uri_round_trips() -> None:
    """FoldResult accepts pae_matrix_uri / peptide_interface_iptm via aliases."""
    from ligandai.types import FoldResult

    fr = FoldResult.model_validate(
        {
            "jobId": "job_xyz",
            "iptm": 0.74,
            "peptideInterfaceIptm": 0.81,
            "paeMatrixPath": "pae://abc123:0",
            "foldMetricDetails": {"overall": {"iptm": 0.74}},
        }
    )
    assert fr.peptide_interface_iptm == 0.81
    assert fr.pae_matrix_uri == "pae://abc123:0"
    assert fr.fold_metric_details["overall"]["iptm"] == 0.74
