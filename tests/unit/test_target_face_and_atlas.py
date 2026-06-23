# Copyright © 2026 Ligandal, Inc. All rights reserved.
"""Unit tests for receptor-targeting intelligence surfaces (bd-LIGANDAI_ALPHA_V2-q3z1b).

Covers:
  - target_face serialization on peptides.generate(): each face value lands in
    the wire body as ``targetFace``; ``None`` (default) omits the field so the
    backend featurizer applies its receptor default (extracellular + TM, IC
    excluded).
  - proteins.receptor_atlas(gene, full=...) builds the right URL: ``/atlas/{gene}``
    for hard-data and ``/atlas/{gene}/full`` for the superadmin ndLF/nanoGPT model.

All tests use pytest-httpx to intercept the request and inspect URL/body without
touching a real server.
"""

from __future__ import annotations

import json

import pytest
from pytest_httpx import HTTPXMock

from ligandai import LigandAI, ReceptorAtlas

BASE = "http://api.ligandai.test"
GEN_URL = f"{BASE}/api/ptf/parallel/generate"

_QUEUED = {"sessionId": "sid_test", "status": "queued"}


@pytest.fixture
def client() -> LigandAI:
    return LigandAI(api_key="lgai_pro_test", base_url=BASE, max_retries=1)


@pytest.fixture
def superadmin_client() -> LigandAI:
    return LigandAI(api_key="lgai_superadmin_test", base_url=BASE, max_retries=1)


def _body(httpx_mock: HTTPXMock) -> dict:
    req = httpx_mock.get_request()
    assert req is not None
    return json.loads(req.content)


# ---------------------------------------------------------------------------
# target_face serialization
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "face",
    ["extracellular", "ec_tm", "transmembrane", "intracellular", "full"],
)
def test_target_face_value_in_wire_body(
    httpx_mock: HTTPXMock, client: LigandAI, face: str
) -> None:
    """Each documented target_face value serializes verbatim to body['targetFace']."""
    httpx_mock.add_response(url=GEN_URL, method="POST", json=_QUEUED)
    client.peptides.generate(gene="GPER1", target_face=face)
    body = _body(httpx_mock)
    assert body["targetFace"] == face


def test_target_face_none_omits_field(httpx_mock: HTTPXMock, client: LigandAI) -> None:
    """Unset target_face must NOT appear in the body — the backend featurizer then
    applies its receptor default (extracellular + TM, intracellular excluded)."""
    httpx_mock.add_response(url=GEN_URL, method="POST", json=_QUEUED)
    client.peptides.generate(gene="GPER1")
    body = _body(httpx_mock)
    assert "targetFace" not in body


def test_target_face_default_is_none(httpx_mock: HTTPXMock, client: LigandAI) -> None:
    """The generate() default for target_face is None (auto)."""
    import inspect

    from ligandai.resources.peptides import Peptides

    sig = inspect.signature(Peptides.generate)
    assert sig.parameters["target_face"].default is None
    # And a call without it omits the field.
    httpx_mock.add_response(url=GEN_URL, method="POST", json=_QUEUED)
    client.peptides.generate(gene="EGFR")
    assert "targetFace" not in _body(httpx_mock)


def test_target_face_intracellular_coexists_with_target_residues(
    httpx_mock: HTTPXMock, client: LigandAI
) -> None:
    """target_face and explicit target_residues both serialize (server precedence
    is resolved server-side; the SDK forwards both faithfully)."""
    from ligandai.types import ResidueRange

    httpx_mock.add_response(url=GEN_URL, method="POST", json=_QUEUED)
    client.peptides.generate(
        gene="GPER1",
        target_face="intracellular",
        target_residues=[ResidueRange(chain="A", start=300, end=310, label="CY")],
        targeting_strategy="pocket_targeted",
    )
    body = _body(httpx_mock)
    assert body["targetFace"] == "intracellular"
    assert body["targets"][0]["targetResidues"] == [
        {"chain": "A", "start": 300, "end": 310, "label": "CY"}
    ]


async def test_target_face_async_generate_serializes(httpx_mock: HTTPXMock) -> None:
    """Async generate() also threads target_face into the wire body."""
    httpx_mock.add_response(url=GEN_URL, method="POST", json=_QUEUED)
    from ligandai import AsyncLigandAI

    async with AsyncLigandAI(api_key="lgai_pro_test", base_url=BASE, max_retries=1) as ac:
        await ac.peptides.generate(gene="GPER1", target_face="ec_tm")
    body = _body(httpx_mock)
    assert body["targetFace"] == "ec_tm"


# ---------------------------------------------------------------------------
# receptor_atlas URL building
# ---------------------------------------------------------------------------


def test_receptor_atlas_hard_data_url(httpx_mock: HTTPXMock, client: LigandAI) -> None:
    """full=False → GET /api/receptor-intelligence/atlas/{gene}."""
    httpx_mock.add_response(
        url=f"{BASE}/api/receptor-intelligence/atlas/GPER1",
        method="GET",
        json={"gene": "GPER1", "signaling_state": "active", "atlas_coverage_count": 9},
    )
    atlas = client.proteins.receptor_atlas("GPER1")
    req = httpx_mock.get_request()
    assert req is not None
    assert req.url.path == "/api/receptor-intelligence/atlas/GPER1"
    assert "/full" not in req.url.path
    assert isinstance(atlas, ReceptorAtlas)
    assert atlas.gene == "GPER1"
    assert atlas.signaling_state == "active"
    assert atlas.atlas_coverage_count == 9


def test_receptor_atlas_full_url(
    httpx_mock: HTTPXMock, superadmin_client: LigandAI
) -> None:
    """full=True → GET /api/receptor-intelligence/atlas/{gene}/full (superadmin)."""
    httpx_mock.add_response(
        url=f"{BASE}/api/receptor-intelligence/atlas/GPER1/full",
        method="GET",
        json={
            "gene": "GPER1",
            "model_predictions": {"available": True, "heads": {"signaling_state": "active"}},
            "intracellular_partners": [{"partner": "GNAS"}],
            "trigger_profile": {"agonist": "E2"},
            "disagreements": [],
            "has_disagreements": False,
        },
    )
    atlas = superadmin_client.proteins.receptor_atlas("GPER1", full=True)
    req = httpx_mock.get_request()
    assert req is not None
    assert req.url.path == "/api/receptor-intelligence/atlas/GPER1/full"
    assert atlas.model_predictions == {
        "available": True,
        "heads": {"signaling_state": "active"},
    }
    assert atlas.intracellular_partners == [{"partner": "GNAS"}]
    assert atlas.trigger_profile == {"agonist": "E2"}
    assert atlas.has_disagreements is False


def test_receptor_atlas_full_sends_bearer_auth(
    httpx_mock: HTTPXMock, superadmin_client: LigandAI
) -> None:
    """The superadmin key reaches the /full endpoint as an Authorization header —
    the server enforces the superadmin gate against it."""
    httpx_mock.add_response(
        url=f"{BASE}/api/receptor-intelligence/atlas/GPER1/full",
        method="GET",
        json={"gene": "GPER1", "model_predictions": {"available": True}},
    )
    superadmin_client.proteins.receptor_atlas("GPER1", full=True)
    req = httpx_mock.get_request()
    assert req is not None
    assert req.headers.get("Authorization") == "Bearer lgai_superadmin_test"


def test_receptor_atlas_preserves_additive_blocks(
    httpx_mock: HTTPXMock, client: LigandAI
) -> None:
    """extra='allow' on ReceptorAtlas preserves server blocks the SDK doesn't type."""
    httpx_mock.add_response(
        url=f"{BASE}/api/receptor-intelligence/atlas/ADRB2",
        method="GET",
        json={"gene": "ADRB2", "some_future_block": {"k": "v"}},
    )
    atlas = client.proteins.receptor_atlas("ADRB2")
    dumped = atlas.model_dump()
    assert dumped.get("some_future_block") == {"k": "v"}


async def test_receptor_atlas_async_full_url(httpx_mock: HTTPXMock) -> None:
    """Async receptor_atlas(full=True) hits the /full path."""
    httpx_mock.add_response(
        url=f"{BASE}/api/receptor-intelligence/atlas/GPER1/full",
        method="GET",
        json={"gene": "GPER1", "model_predictions": {"available": True}},
    )
    from ligandai import AsyncLigandAI

    async with AsyncLigandAI(
        api_key="lgai_superadmin_test", base_url=BASE, max_retries=1
    ) as ac:
        atlas = await ac.proteins.receptor_atlas("GPER1", full=True)
    req = httpx_mock.get_request()
    assert req is not None
    assert req.url.path == "/api/receptor-intelligence/atlas/GPER1/full"
    assert atlas.model_predictions == {"available": True}
