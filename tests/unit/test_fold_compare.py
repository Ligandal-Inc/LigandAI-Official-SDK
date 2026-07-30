# Copyright © 2026 Ligandal, Inc. All rights reserved.
"""Unit tests for peptides.fold_compare + validate_selectivity (bd-dre-7x9bc).

Covers the selectivity / negative-selection surface ported from the ligandai-api
reference into the canonical peptides resource:

  * fold_compare POSTs to /api/v1/fold-compare/start with the binder + engine
  * effective species is carried into the body, fail-closed (mouse -> human
    when not entitled)
  * organism wins over species; raw sequences are species-agnostic
  * wait=False returns the raw submit; wait=True polls {job}/status to terminal
  * validation errors (empty binder / missing on_target / top_n < 1)
  * validate_selectivity pulls elite via get_elite, ranks by iPSAE, fans out
    fold_compare over the top_n
  * async parity

HTTP is fully mocked with ``pytest-httpx``.
"""

from __future__ import annotations

import json

import pytest
from pytest_httpx import HTTPXMock

from ligandai import LigandAI
from ligandai.errors import LigandAIValidationError

BASE = "http://api.ligandai.test"
ENTITLEMENT_URL = f"{BASE}/api/cross-species/entitlement"
START_URL = f"{BASE}/api/v1/fold-compare/start"


def _client(**kwargs) -> LigandAI:
    return LigandAI(
        api_key="lgai_pro_testkey0123456789ABCDEF",
        base_url=BASE,
        max_retries=1,
        **kwargs,
    )


def _mock_entitlement(httpx_mock: HTTPXMock, entitled: bool) -> None:
    httpx_mock.add_response(
        url=ENTITLEMENT_URL,
        method="GET",
        json={
            "success": True,
            "capability": "species_targeting",
            "entitled": entitled,
            "isSuperAdmin": entitled,
            "orgGranted": False,
            "species": ["human", "mouse"] if entitled else ["human"],
            "defaultSpecies": "human",
        },
        is_reusable=True,
    )


def _start_body(httpx_mock: HTTPXMock) -> dict:
    reqs = [r for r in httpx_mock.get_requests() if str(r.url).startswith(START_URL)]
    assert reqs, "no fold-compare/start request captured"
    return json.loads(reqs[-1].content)


# --------------------------------------------------------------------------- #
# fold_compare — submit shape + species                                         #
# --------------------------------------------------------------------------- #


def test_fold_compare_submit_shape(httpx_mock: HTTPXMock) -> None:
    httpx_mock.add_response(url=START_URL, method="POST", json={"job_id": "j1"})
    client = _client()
    out = client.peptides.fold_compare(
        "PEPTIDESEQ", "EGFR", binder_id="b1", wait=False
    )
    assert out == {"job_id": "j1"}
    body = _start_body(httpx_mock)
    assert body["binder"] == {"id": "b1", "sequence": "PEPTIDESEQ"}
    assert body["engine"] == "boltz2"
    assert body["on_target"] == {"gene": "EGFR"}
    # Species always stamped (fail-closed default human).
    assert body["organism"] == "human"
    assert body["species"] == "human"
    client.close()


def test_fold_compare_isoforms_and_off_targets(httpx_mock: HTTPXMock) -> None:
    httpx_mock.add_response(url=START_URL, method="POST", json={"job_id": "j1"})
    client = _client()
    client.peptides.fold_compare(
        "SEQ",
        "EGFR",
        isoforms="all",
        off_targets=["ERBB2", "MDGDGSEQ"],
        wait=False,
    )
    body = _start_body(httpx_mock)
    assert body["isoforms"] == "all"
    assert body["off_targets"] == ["ERBB2", "MDGDGSEQ"]
    client.close()


def test_fold_compare_on_target_sequence(httpx_mock: HTTPXMock) -> None:
    httpx_mock.add_response(url=START_URL, method="POST", json={"job_id": "j1"})
    client = _client()
    client.peptides.fold_compare(
        "SEQ", "EGFR", on_target_sequence="mtargetseq", wait=False
    )
    body = _start_body(httpx_mock)
    assert body["on_target"] == {"gene": "EGFR", "sequence": "MTARGETSEQ"}
    client.close()


def test_fold_compare_mouse_when_entitled(httpx_mock: HTTPXMock) -> None:
    _mock_entitlement(httpx_mock, entitled=True)
    httpx_mock.add_response(url=START_URL, method="POST", json={"job_id": "j1"})
    client = _client()
    client.peptides.fold_compare("SEQ", "Egfr", organism="mouse", wait=False)
    body = _start_body(httpx_mock)
    assert body["organism"] == "mouse"
    assert body["species"] == "mouse"
    client.close()


def test_fold_compare_mouse_fail_closed(httpx_mock: HTTPXMock) -> None:
    _mock_entitlement(httpx_mock, entitled=False)
    httpx_mock.add_response(url=START_URL, method="POST", json={"job_id": "j1"})
    client = _client()
    client.peptides.fold_compare("SEQ", "EGFR", organism="mouse", wait=False)
    body = _start_body(httpx_mock)
    assert body["organism"] == "human", "non-entitled fold_compare must not force mouse"
    client.close()


def test_fold_compare_organism_wins_over_species(httpx_mock: HTTPXMock) -> None:
    _mock_entitlement(httpx_mock, entitled=True)
    httpx_mock.add_response(url=START_URL, method="POST", json={"job_id": "j1"})
    client = _client()
    client.peptides.fold_compare(
        "SEQ", "Egfr", organism="mouse", species="human", wait=False
    )
    body = _start_body(httpx_mock)
    assert body["organism"] == "mouse"
    client.close()


# --------------------------------------------------------------------------- #
# fold_compare — wait / poll                                                    #
# --------------------------------------------------------------------------- #


def test_fold_compare_wait_polls_to_terminal(httpx_mock: HTTPXMock) -> None:
    httpx_mock.add_response(url=START_URL, method="POST", json={"job_id": "j1"})
    status_url = f"{BASE}/api/v1/fold-compare/j1/status"
    httpx_mock.add_response(url=status_url, method="GET", json={"status": "running"})
    httpx_mock.add_response(
        url=status_url,
        method="GET",
        json={"status": "completed", "ranking": [{"target": "EGFR", "ipsae": 0.8}]},
    )
    client = _client()
    report = client.peptides.fold_compare(
        "SEQ", "EGFR", wait=True, poll_interval=0.0
    )
    assert report["status"] == "completed"
    assert report["ranking"][0]["ipsae"] == 0.8
    client.close()


def test_fold_compare_no_job_id_raises(httpx_mock: HTTPXMock) -> None:
    httpx_mock.add_response(url=START_URL, method="POST", json={"queued": True})
    client = _client()
    with pytest.raises(LigandAIValidationError):
        client.peptides.fold_compare("SEQ", "EGFR", wait=True, poll_interval=0.0)
    client.close()


# --------------------------------------------------------------------------- #
# fold_compare — input validation                                              #
# --------------------------------------------------------------------------- #


def test_fold_compare_empty_binder_raises() -> None:
    client = _client()
    with pytest.raises(LigandAIValidationError):
        client.peptides.fold_compare("", "EGFR", wait=False)
    client.close()


def test_fold_compare_missing_on_target_raises() -> None:
    client = _client()
    with pytest.raises(LigandAIValidationError):
        client.peptides.fold_compare("SEQ", "", wait=False)
    client.close()


# --------------------------------------------------------------------------- #
# validate_selectivity — elite sourcing + ranking + fan-out                     #
# --------------------------------------------------------------------------- #


def test_validate_selectivity_ranks_and_fans_out(httpx_mock: HTTPXMock) -> None:
    elite_url = f"{BASE}/api/ptf/parallel/sid1/elite"
    httpx_mock.add_response(
        url=elite_url,
        method="GET",
        json=[
            {"sequence": "LOWSEQ", "ipsae": 0.30, "peptide_id": 101},
            {"sequence": "HIGHSEQ", "ipsae": 0.90, "peptide_id": 102},
            {"sequence": "MIDSEQ", "ipsae": 0.60, "peptide_id": 103},
        ],
    )
    httpx_mock.add_response(
        url=START_URL, method="POST", json={"job_id": "j1"}, is_reusable=True
    )
    client = _client()
    out = client.peptides.validate_selectivity(
        "sid1", "EGFR", top_n=2, isoforms="all", wait=False
    )
    assert len(out) == 2
    # Ranked by iPSAE desc: HIGH (102) then MID (103).
    ordered_ids = [
        json.loads(r.content)["binder"]["id"]
        for r in httpx_mock.get_requests()
        if str(r.url).startswith(START_URL)
    ]
    assert ordered_ids == ["102", "103"]
    assert out[0]["binder"].sequence == "HIGHSEQ"
    assert out[0]["fold_compare"] == {"job_id": "j1"}
    client.close()


def test_validate_selectivity_top_n_zero_raises() -> None:
    client = _client()
    with pytest.raises(LigandAIValidationError):
        client.peptides.validate_selectivity("sid1", "EGFR", top_n=0)
    client.close()


# --------------------------------------------------------------------------- #
# async parity                                                                  #
# --------------------------------------------------------------------------- #


async def test_async_fold_compare_submit(httpx_mock: HTTPXMock) -> None:
    from ligandai import AsyncLigandAI

    httpx_mock.add_response(url=START_URL, method="POST", json={"job_id": "j1"})
    async with AsyncLigandAI(
        api_key="lgai_pro_test", base_url=BASE, max_retries=1
    ) as ac:
        out = await ac.peptides.fold_compare("SEQ", "EGFR", wait=False)
    assert out == {"job_id": "j1"}
    body = _start_body(httpx_mock)
    assert body["binder"]["sequence"] == "SEQ"
    assert body["organism"] == "human"


async def test_async_validate_selectivity(httpx_mock: HTTPXMock) -> None:
    from ligandai import AsyncLigandAI

    elite_url = f"{BASE}/api/ptf/parallel/sid1/elite"
    httpx_mock.add_response(
        url=elite_url,
        method="GET",
        json=[{"sequence": "HIGHSEQ", "ipsae": 0.9, "peptide_id": 102}],
    )
    httpx_mock.add_response(url=START_URL, method="POST", json={"job_id": "j1"})
    async with AsyncLigandAI(
        api_key="lgai_pro_test", base_url=BASE, max_retries=1
    ) as ac:
        out = await ac.peptides.validate_selectivity(
            "sid1", "EGFR", top_n=1, wait=False
        )
    assert len(out) == 1
    assert out[0]["binder"].sequence == "HIGHSEQ"
