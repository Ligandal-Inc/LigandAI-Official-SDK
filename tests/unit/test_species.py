# Copyright © 2026 Ligandal, Inc. All rights reserved.
"""Unit tests for the SDK species / organism selector (bd-dre-7x9bc).

Mirrors the receptordb SDK's species coverage, adapted to the canonical
ligandai tree's resource surfaces:

  * ``normalize_species`` parity with the server's ``normalizeSpecies``
  * client ``default_organism`` threading into requests
  * per-call ``organism`` / ``species`` override (``organism`` wins)
  * ENTITLEMENT FAIL-CLOSED: a non-entitled key never forces ``mouse``
  * entitlement caching + ``check_entitlement=False`` deferral
  * "flag mouse for mouse targets" — mouse routing + effective-species surfacing

HTTP is fully mocked with ``pytest-httpx`` — no network, no real API key.
"""

from __future__ import annotations

import json

import pytest
from pytest_httpx import HTTPXMock

from ligandai import (
    DEFAULT_SPECIES,
    LigandAI,
    SpeciesEntitlement,
    is_mouse,
    normalize_species,
)

BASE = "http://api.ligandai.test"
ENTITLEMENT_URL = f"{BASE}/api/cross-species/entitlement"
TOP_MARKERS_URL = f"{BASE}/api/transcriptomics/top-markers"
RECEPTOR_SEARCH_URL = f"{BASE}/api/receptordb/search"
FOLD_COMPARE_START_URL = f"{BASE}/api/v1/fold-compare/start"


# --------------------------------------------------------------------------- #
# Helpers                                                                       #
# --------------------------------------------------------------------------- #


def _entitlement_body(entitled: bool) -> dict:
    return {
        "success": True,
        "capability": "species_targeting",
        "entitled": entitled,
        "isSuperAdmin": entitled,
        "orgGranted": False,
        "species": ["human", "mouse"] if entitled else ["human"],
        "defaultSpecies": "human",
    }


def _mock_entitlement(
    httpx_mock: HTTPXMock, entitled: bool, *, reusable: bool = True, optional: bool = False
) -> None:
    httpx_mock.add_response(
        url=ENTITLEMENT_URL,
        method="GET",
        json=_entitlement_body(entitled),
        is_reusable=reusable,
        is_optional=optional,
    )


def _mock_top_markers(httpx_mock: HTTPXMock) -> None:
    httpx_mock.add_response(
        url=TOP_MARKERS_URL, method="POST", json={"top": []}, is_reusable=True
    )


def _client(**kwargs) -> LigandAI:
    return LigandAI(
        api_key="lgai_pro_testkey0123456789ABCDEF",
        base_url=BASE,
        max_retries=1,
        **kwargs,
    )


def _last_body(httpx_mock: HTTPXMock, url: str) -> dict:
    reqs = [r for r in httpx_mock.get_requests() if str(r.url).startswith(url)]
    assert reqs, f"no request captured for {url}"
    return json.loads(reqs[-1].content)


# --------------------------------------------------------------------------- #
# Pure normalization (no client / no network)                                  #
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize(
    "value,expected",
    [
        ("mouse", "mouse"),
        ("MOUSE", "mouse"),
        ("Mus_musculus", "mouse"),
        ("mus musculus", "mouse"),
        ("mmu", "mouse"),
        ("mm", "mouse"),
        ("10090", "mouse"),
        ("mgi", "mouse"),
        ("human", "human"),
        ("Homo Sapiens", "human"),
        ("9606", "human"),
        ("", "human"),
        (None, "human"),
        ("banana", "human"),
        (12345, "human"),
    ],
)
def test_normalize_species_matches_server(value, expected) -> None:
    assert normalize_species(value) == expected


def test_default_species_is_human() -> None:
    assert DEFAULT_SPECIES == "human"
    assert normalize_species(None) == "human"


def test_is_mouse() -> None:
    assert is_mouse("mouse") is True
    assert is_mouse("mmu") is True
    assert is_mouse("human") is False
    assert is_mouse(None) is False


# --------------------------------------------------------------------------- #
# default_organism threading                                                    #
# --------------------------------------------------------------------------- #


def test_default_organism_human_is_default(httpx_mock: HTTPXMock) -> None:
    _mock_top_markers(httpx_mock)
    client = _client()
    assert client.default_organism == "human"
    client.discovery.tissue_markers(target_tissues=["Liver"])
    body = _last_body(httpx_mock, TOP_MARKERS_URL)
    assert body["organism"] == "human"
    assert body["species"] == "human"
    client.close()


def test_default_organism_mouse_threads_when_entitled(httpx_mock: HTTPXMock) -> None:
    _mock_entitlement(httpx_mock, entitled=True)
    _mock_top_markers(httpx_mock)
    client = _client(default_organism="mouse")
    client.discovery.tissue_markers(target_tissues=["Liver"])
    body = _last_body(httpx_mock, TOP_MARKERS_URL)
    assert body["organism"] == "mouse"
    assert body["species"] == "mouse"
    client.close()


def test_default_organism_alias_normalized(httpx_mock: HTTPXMock) -> None:
    client = _client(default_organism="mus_musculus")
    assert client.default_organism == "mouse"
    client.close()


# --------------------------------------------------------------------------- #
# per-call override                                                             #
# --------------------------------------------------------------------------- #


def test_per_call_organism_overrides_default(httpx_mock: HTTPXMock) -> None:
    _mock_entitlement(httpx_mock, entitled=True)
    _mock_top_markers(httpx_mock)
    client = _client(default_organism="human")
    client.discovery.tissue_markers(target_tissues=["Liver"], organism="mouse")
    body = _last_body(httpx_mock, TOP_MARKERS_URL)
    assert body["organism"] == "mouse"
    client.close()


def test_species_alias_param_accepted(httpx_mock: HTTPXMock) -> None:
    _mock_entitlement(httpx_mock, entitled=True)
    _mock_top_markers(httpx_mock)
    client = _client()
    client.discovery.tissue_markers(target_tissues=["Liver"], species="mouse")
    body = _last_body(httpx_mock, TOP_MARKERS_URL)
    assert body["organism"] == "mouse"
    client.close()


def test_organism_wins_over_species_alias(httpx_mock: HTTPXMock) -> None:
    _mock_entitlement(httpx_mock, entitled=True)
    _mock_top_markers(httpx_mock)
    client = _client()
    client.discovery.tissue_markers(
        target_tissues=["Liver"], organism="mouse", species="human"
    )
    body = _last_body(httpx_mock, TOP_MARKERS_URL)
    assert body["organism"] == "mouse"
    client.close()


# --------------------------------------------------------------------------- #
# ENTITLEMENT FAIL-CLOSED — the core security invariant                         #
# --------------------------------------------------------------------------- #


def test_not_entitled_coerces_mouse_to_human(httpx_mock: HTTPXMock) -> None:
    _mock_entitlement(httpx_mock, entitled=False)
    _mock_top_markers(httpx_mock)
    client = _client(default_organism="mouse")
    client.discovery.tissue_markers(target_tissues=["Liver"], organism="mouse")
    body = _last_body(httpx_mock, TOP_MARKERS_URL)
    assert body["organism"] == "human", "non-entitled key must NOT force mouse"
    client.close()


def test_entitlement_http_error_fails_closed(httpx_mock: HTTPXMock) -> None:
    # Endpoint 500s (past retries) -> fail closed (not entitled -> human).
    httpx_mock.add_response(
        url=ENTITLEMENT_URL, method="GET", status_code=500, json={"error": "boom"},
        is_reusable=True,
    )
    _mock_top_markers(httpx_mock)
    client = _client(default_organism="mouse")
    client.discovery.tissue_markers(target_tissues=["Liver"], organism="mouse")
    body = _last_body(httpx_mock, TOP_MARKERS_URL)
    assert body["organism"] == "human"
    assert client.entitlement().entitled is False
    client.close()


def test_check_entitlement_false_defers_to_server(httpx_mock: HTTPXMock) -> None:
    # Pre-flight disabled: the client optimistically sends mouse and lets the
    # SERVER be the sole arbiter — no entitlement call is made.
    _mock_entitlement(httpx_mock, entitled=False, optional=True)
    _mock_top_markers(httpx_mock)
    client = _client(default_organism="mouse", check_entitlement=False)
    client.discovery.tissue_markers(target_tissues=["Liver"], organism="mouse")
    body = _last_body(httpx_mock, TOP_MARKERS_URL)
    assert body["organism"] == "mouse"
    ent_calls = [
        r for r in httpx_mock.get_requests() if str(r.url).startswith(ENTITLEMENT_URL)
    ]
    assert not ent_calls, "entitlement must not be pre-flighted when disabled"
    client.close()


def test_entitlement_is_cached(httpx_mock: HTTPXMock) -> None:
    _mock_entitlement(httpx_mock, entitled=True, reusable=True)
    _mock_top_markers(httpx_mock)
    client = _client(default_organism="mouse")
    client.discovery.tissue_markers(target_tissues=["Liver"], organism="mouse")
    client.discovery.tissue_markers(target_tissues=["Kidney"], organism="mouse")
    ent_calls = [
        r for r in httpx_mock.get_requests() if str(r.url).startswith(ENTITLEMENT_URL)
    ]
    assert len(ent_calls) == 1, "entitlement must be fetched once and cached"
    client.close()


def test_entitlement_model_shape(httpx_mock: HTTPXMock) -> None:
    _mock_entitlement(httpx_mock, entitled=True)
    client = _client()
    ent = client.entitlement()
    assert isinstance(ent, SpeciesEntitlement)
    assert ent.entitled is True
    assert ent.capability == "species_targeting"
    assert "mouse" in ent.species
    assert ent.default_species == "human"
    client.close()


# --------------------------------------------------------------------------- #
# receptors browse surface — organism param + legacy-byte preservation          #
# --------------------------------------------------------------------------- #


def test_receptors_search_default_human_omits_organism(httpx_mock: HTTPXMock) -> None:
    # Public browse surface: default-human is byte-identical to the legacy wire —
    # no organism param is added.
    httpx_mock.add_response(
        url=f"{RECEPTOR_SEARCH_URL}?query=EGFR&limit=10", method="GET", json=[]
    )
    client = _client()
    client.receptors.search("EGFR")
    req = [r for r in httpx_mock.get_requests() if str(r.url).startswith(RECEPTOR_SEARCH_URL)][-1]
    assert "organism" not in dict(req.url.params)
    client.close()


def test_receptors_search_mouse_stamps_when_entitled(httpx_mock: HTTPXMock) -> None:
    _mock_entitlement(httpx_mock, entitled=True)
    httpx_mock.add_response(
        url=f"{RECEPTOR_SEARCH_URL}?query=Egfr&limit=10&organism=mouse&species=mouse",
        method="GET",
        json=[],
    )
    client = _client()
    client.receptors.search("Egfr", organism="mouse")
    req = [r for r in httpx_mock.get_requests() if str(r.url).startswith(RECEPTOR_SEARCH_URL)][-1]
    assert dict(req.url.params)["organism"] == "mouse"
    client.close()


def test_receptors_search_mouse_not_entitled_falls_back(httpx_mock: HTTPXMock) -> None:
    _mock_entitlement(httpx_mock, entitled=False)
    httpx_mock.add_response(
        url=f"{RECEPTOR_SEARCH_URL}?query=Egfr&limit=10&organism=human&species=human",
        method="GET",
        json=[],
    )
    client = _client()
    client.receptors.search("Egfr", organism="mouse")
    req = [r for r in httpx_mock.get_requests() if str(r.url).startswith(RECEPTOR_SEARCH_URL)][-1]
    assert dict(req.url.params)["organism"] == "human"
    client.close()


# --------------------------------------------------------------------------- #
# async client entitlement + threading                                          #
# --------------------------------------------------------------------------- #


async def test_async_default_organism_human(httpx_mock: HTTPXMock) -> None:
    from ligandai import AsyncLigandAI

    httpx_mock.add_response(
        url=TOP_MARKERS_URL, method="POST", json={"top": []}, is_reusable=True
    )
    async with AsyncLigandAI(
        api_key="lgai_pro_test", base_url=BASE, max_retries=1
    ) as ac:
        await ac.discovery.tissue_markers(target_tissues=["Liver"])
    body = _last_body(httpx_mock, TOP_MARKERS_URL)
    assert body["organism"] == "human"


async def test_async_mouse_threads_after_await_entitlement(httpx_mock: HTTPXMock) -> None:
    from ligandai import AsyncLigandAI

    _mock_entitlement(httpx_mock, entitled=True)
    httpx_mock.add_response(
        url=TOP_MARKERS_URL, method="POST", json={"top": []}, is_reusable=True
    )
    async with AsyncLigandAI(
        api_key="lgai_pro_test", base_url=BASE, max_retries=1, default_organism="mouse"
    ) as ac:
        # Async client fails closed until the entitlement is awaited once.
        await ac.entitlement()
        await ac.discovery.tissue_markers(target_tissues=["Liver"], organism="mouse")
    body = _last_body(httpx_mock, TOP_MARKERS_URL)
    assert body["organism"] == "mouse"


async def test_async_mouse_fail_closed_without_awaited_entitlement(
    httpx_mock: HTTPXMock,
) -> None:
    from ligandai import AsyncLigandAI

    # No awaited entitlement -> async client has no cached decision -> fail closed.
    httpx_mock.add_response(
        url=TOP_MARKERS_URL, method="POST", json={"top": []}, is_reusable=True
    )
    async with AsyncLigandAI(
        api_key="lgai_pro_test", base_url=BASE, max_retries=1, default_organism="mouse"
    ) as ac:
        await ac.discovery.tissue_markers(target_tissues=["Liver"], organism="mouse")
    body = _last_body(httpx_mock, TOP_MARKERS_URL)
    assert body["organism"] == "human", "async fail-closed without awaited entitlement"
