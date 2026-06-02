# Copyright © 2026 Ligandal, Inc. All rights reserved.
"""Unit tests for the DeltaForge scoring namespace (w1rcj).

Covers:
  - ``client.deltaforge.score_fold`` request shape + parsing (incl. fold metrics + PAE)
  - ``client.deltaforge.score_pdb`` include_pae passthrough
  - ``client.deltaforge.batch_score_fold`` request shape + envelope parsing
  - ``client.deltaforge.batch_score_fold_csv`` raw CSV text
  - ``client.peptide`` singular alias is the same object as ``client.peptides``
  - Input validation (missing fold_job_id / empty batch list)
"""

from __future__ import annotations

import json as _json

import pytest
from pytest_httpx import HTTPXMock

from ligandai import LigandAI

BASE = "http://api.ligandai.test"


@pytest.fixture
def pro_client() -> LigandAI:
    return LigandAI(api_key="lgai_pro_test", base_url=BASE, max_retries=1)


def test_peptide_singular_alias(pro_client: LigandAI) -> None:
    assert pro_client.peptide is pro_client.peptides
    assert hasattr(pro_client.peptide, "score_pdb")


def test_score_fold_posts_fold_id_and_parses_metrics(
    httpx_mock: HTTPXMock, pro_client: LigandAI
) -> None:
    httpx_mock.add_response(
        url=f"{BASE}/api/v1/deltaforge/score-fold",
        json={
            "success": True,
            "foldJobId": "fold_1780410230005_wzgke1p5f",
            "delta_g": -8.4,
            "kd_nm": 42.0,
            "scorer": "deltaforge_v10_2_unified",
            "scorer_version": "v10_2_unified_parallel_2026_05_08",
            "iptm": 0.83,
            "ptm": 0.79,
            "ipsae": 0.71,
            "plddt_mean": 88.5,
            "classification": "Strong binder",
        },
    )

    score = pro_client.deltaforge.score_fold(
        "fold_1780410230005_wzgke1p5f", include_pae=False
    )

    assert score.dg == pytest.approx(-8.4)
    assert score.kd_nm == pytest.approx(42.0)
    assert score.iptm == pytest.approx(0.83)
    assert score.ptm == pytest.approx(0.79)
    assert score.ipsae == pytest.approx(0.71)
    assert score.plddt_mean == pytest.approx(88.5)
    assert score.scorer_version == "v10_2_unified_parallel_2026_05_08"
    assert score.fold_job_id == "fold_1780410230005_wzgke1p5f"

    request = httpx_mock.get_request()
    assert request is not None
    body = _json.loads(request.read())
    assert body["foldJobId"] == "fold_1780410230005_wzgke1p5f"
    assert body["includePae"] is False
    assert body["scorer"] == "auto"


def test_score_fold_with_pae(httpx_mock: HTTPXMock, pro_client: LigandAI) -> None:
    httpx_mock.add_response(
        url=f"{BASE}/api/v1/deltaforge/score-fold",
        json={
            "success": True,
            "foldJobId": "fold_X",
            "delta_g": -6.1,
            "kd_nm": 900.0,
            "scorer_version": "v10_2_unified_parallel_2026_05_08",
            "pae": [[0.0, 3.2], [3.2, 0.0]],
            "paeStatus": "ok",
        },
    )
    score = pro_client.deltaforge.score_fold("fold_X", include_pae=True)
    assert score.pae == [[0.0, 3.2], [3.2, 0.0]]
    assert score.pae_status == "ok"
    body = _json.loads(httpx_mock.get_request().read())
    assert body["includePae"] is True


def test_score_pdb_include_pae_passthrough(
    httpx_mock: HTTPXMock, pro_client: LigandAI
) -> None:
    httpx_mock.add_response(
        url=f"{BASE}/api/v1/deltaforge/score-pdb",
        json={"success": True, "delta_g": -5.5, "kd_nm": 1200.0, "paeStatus": "pending"},
    )
    score = pro_client.deltaforge.score_pdb(
        pdb_content="ATOM      1  CA  ALA A   1       0.0     0.0     0.0  1.00 85.00           C\n",
        receptor_chains=["A"],
        peptide_chain="B",
        include_pae=True,
    )
    assert score.pae_status == "pending"
    body = _json.loads(httpx_mock.get_request().read())
    assert body["includePae"] is True
    assert body["receptorChains"] == ["A"]
    assert body["peptideChain"] == "B"


def test_batch_score_fold_envelope(httpx_mock: HTTPXMock, pro_client: LigandAI) -> None:
    httpx_mock.add_response(
        url=f"{BASE}/api/v1/deltaforge/batch-score-fold",
        json={
            "success": True,
            "scored": 2,
            "failed": 0,
            "results": [
                {
                    "foldJobId": "f1", "sequence": "ACDEFG", "receptorChains": ["A"],
                    "peptideChain": "B", "delta_g": -7.0, "kd_nm": 88.0,
                    "classification": "Strong binder", "iptm": 0.8, "ptm": 0.75,
                    "ipsae": 0.7, "plddt_mean": 90.0,
                },
                {
                    "foldJobId": "f2", "sequence": "WYKLMN", "receptorChains": ["A"],
                    "peptideChain": "B", "delta_g": -2.0, "kd_nm": 5000.0,
                    "classification": "Weak binder", "iptm": 0.4, "ptm": 0.5,
                    "ipsae": 0.3, "plddt_mean": 70.0,
                },
            ],
            "errors": [],
        },
    )
    out = pro_client.deltaforge.batch_score_fold(["f1", "f2"], include_pae=False)
    assert out["scored"] == 2
    assert len(out["results"]) == 2
    assert out["results"][0]["foldJobId"] == "f1"
    body = _json.loads(httpx_mock.get_request().read())
    assert body["foldJobIds"] == ["f1", "f2"]


def test_batch_score_fold_csv(httpx_mock: HTTPXMock, pro_client: LigandAI) -> None:
    csv_text = (
        "foldJobId,sequence,receptorChains,peptideChain,delta_g,kd_nm,classification\n"
        "f1,ACDEFG,A,B,-7.0,88.0,Strong binder\n"
    )
    httpx_mock.add_response(
        url=f"{BASE}/api/v1/deltaforge/batch-score-fold?format=csv",
        text=csv_text,
        headers={"Content-Type": "text/csv"},
    )
    out = pro_client.deltaforge.batch_score_fold_csv(["f1"])
    assert out.startswith("foldJobId,sequence")
    assert "Strong binder" in out


def test_score_fold_requires_fold_id(pro_client: LigandAI) -> None:
    with pytest.raises(ValueError):
        pro_client.deltaforge.score_fold("")


def test_batch_requires_nonempty_list(pro_client: LigandAI) -> None:
    with pytest.raises(ValueError):
        pro_client.deltaforge.batch_score_fold([])
