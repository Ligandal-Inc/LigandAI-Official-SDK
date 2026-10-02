# Copyright © 2026 Ligandal, Inc. All rights reserved.
"""``proteins.info`` / ``receptor_topology`` and the ``synthesis`` resource against blue.

* ``proteins.info`` required ``gene`` where the route sends ``geneName``;
  ``receptor_topology`` left its typed fields ``None`` beside a populated
  ``chain_topologies``;
* ``synthesis`` called routes the server does not have, and validated the
  platform's ``coming_soon`` stub into models that need real fields (or read it
  as "no orders").

Bodies are trimmed copies of 2026-10-02 reads from ``http://127.0.0.1:5050``.
[bd-dre-3imtv.4]
"""

from __future__ import annotations

import re
from typing import Any

import pytest
from pytest_httpx import HTTPXMock

from ligandai import AsyncLigandAI, LigandAI
from ligandai.errors import LigandAINotFoundError, LigandAIUnreadableResponse
from ligandai.resources.synthesis import LigandAISynthesisUnavailable
from ligandai.types import ProteinInfo, ReceptorTopology

BASE = "http://api.ligandai.test"

PROTEIN_INFO = {
    "geneName": "EGFR",
    "uniprotId": "P00533",
    "source": "receptor_database",
    "proteinName": "Epidermal growth factor receptor (EC 2.7.10.1)",
    "tmHelices": 1,
}
TOPOLOGY = {
    "success": True,
    "gene": "EGFR",
    "uniprotId": "P00533",
    "has_topology": True,
    "chain_topologies": {
        "A": {
            "extracellular": [[25, 645]],
            "transmembrane": [[646, 668]],
            "cytoplasmic": [[669, 1210]],
            "signal_peptide": [],
        }
    },
}
# server/sdk-alias-routes.ts `synthesisStub`: the ONE body all seven
# /api/synthesis-checkout/* routes answer today.
STUB = {
    "status": "coming_soon",
    "message": "API synthesis requests coming soon. You can export your results and place orders on the Ligandai.com platform.",
    "platform_url": "https://ligandai.com/synthesis",
    "pricing_url": "https://ligandai.com/pricing",
    "tier": "superadmin",
    "can_synthesize_via_ui": True,
}


def _mk(key: str = "lgai_pro_testkey0123456789ABCDEF") -> LigandAI:
    return LigandAI(api_key=key, base_url=BASE, max_retries=1)


# ------------------------------------------------ proteins: info and topology


def test_protein_info_reads_genename(httpx_mock: HTTPXMock) -> None:
    httpx_mock.add_response(
        url=f"{BASE}/api/protein-info/EGFR?include_sequence=true&include_ptms=true&include_domains=true",
        json=PROTEIN_INFO,
    )
    info = _mk().proteins.info("EGFR")
    assert info.gene == "EGFR"
    assert info.uniprot_id == "P00533"
    assert info.protein_name and info.protein_name.startswith("Epidermal growth factor receptor")
    assert info.source == "receptor_database"


def test_protein_info_still_accepts_the_old_gene_key() -> None:
    assert ProteinInfo.model_validate({"gene": "TP53"}).gene == "TP53"


def test_receptor_topology_derives_flat_fields_from_chain_topologies() -> None:
    t = ReceptorTopology.model_validate(TOPOLOGY)
    assert [(r.chain, r.start, r.end) for r in t.extracellular or []] == [("A", 25, 645)]
    assert [(r.chain, r.start, r.end) for r in t.tm_regions or []] == [("A", 646, 668)]
    assert [(r.chain, r.start, r.end) for r in t.intracellular or []] == [("A", 669, 1210)]
    assert t.signal_peptide is None  # the server listed none: measured, not unknown
    assert t.model_extra and "chain_topologies" in t.model_extra  # still readable verbatim


def test_receptor_topology_keeps_unknown_as_none_when_server_has_no_topology() -> None:
    t = ReceptorTopology.model_validate(
        {"success": True, "gene": "XYZ1", "has_topology": False, "chain_topologies": {"A": {}}}
    )
    assert t.extracellular is None and t.tm_regions is None


def test_receptor_topology_flat_shape_is_untouched() -> None:
    t = ReceptorTopology.model_validate(
        {"gene": "G", "extracellular": [{"chain": "A", "start": 1, "end": 9}]}
    )
    assert t.extracellular and t.extracellular[0].end == 9


# ---------------------------------------------------------------- synthesis


@pytest.mark.parametrize(
    "call",
    [
        lambda c: c.synthesis.get_cart(),
        lambda c: c.synthesis.list_orders(),
        lambda c: c.synthesis.get_order("o1"),
        lambda c: c.synthesis.estimate([{"sequence": "YHWYGYTPQNVI"}]),
        lambda c: c.synthesis.recommend([{"sequence": "YHWYGYTPQNVI"}]),
        lambda c: c.synthesis.add_to_cart("s1", "EGFR", peptide_sequences=["YHWYGYTPQNVI"]),
    ],
)
def test_coming_soon_stub_raises_instead_of_validating_or_reading_as_empty(
    httpx_mock: HTTPXMock, call: Any
) -> None:
    httpx_mock.add_response(url=re.compile(rf"{BASE}/api/synthesis-checkout/.*"), json=STUB)
    with pytest.raises(LigandAISynthesisUnavailable) as ei:
        call(_mk())
    assert ei.value.platform_url == "https://ligandai.com/synthesis"
    assert ei.value.can_synthesize_via_ui is True
    assert "coming soon" in str(ei.value)


def test_synthesis_options_still_returns_the_stub_readably(httpx_mock: HTTPXMock) -> None:
    httpx_mock.add_response(
        url=re.compile(rf"{BASE}/api/synthesis-checkout/options.*"), json=STUB
    )
    assert _mk().synthesis.options().status == "coming_soon"


def test_linker_options_use_the_route_the_server_has(httpx_mock: HTTPXMock) -> None:
    httpx_mock.add_response(
        url=f"{BASE}/api/synthesis/linker-options",
        json={"linkers": [{
            "id": "n_biotin_peg4", "position": "n_terminal", "type": "biotin_peg4",
            "description": "N-terminal Biotin-PEG4 (standard)", "format": "Biotin-PEG4-{sequence}",
            "recommendedFor": ["Most peptides"], "length_angstroms": 20, "cost_addon": 5000,
            "flexibility": "flexible",
        }]},
    )
    (link,) = _mk().synthesis.linker_options()
    assert link.id == "n_biotin_peg4"
    assert link.length_angstroms == 20.0


def test_linker_options_refuses_an_unrecognised_body(httpx_mock: HTTPXMock) -> None:
    httpx_mock.add_response(url=f"{BASE}/api/synthesis/linker-options", json={"options": []})
    with pytest.raises(LigandAIUnreadableResponse):
        _mk().synthesis.linker_options()


def test_adaptyv_search_targets_use_the_route_and_params_the_server_has(
    httpx_mock: HTTPXMock,
) -> None:
    httpx_mock.add_response(
        url=f"{BASE}/api/adaptyv/targets?search=EGFR&limit=2",
        json={"items": [{"id": "019a03da", "name": "Cynomolgus EGFR Protein", "vendor_name": "Sino",
                         "catalog_number": "90285-C08H"}],
              "total": 158, "count": 1, "offset": 0},
    )
    (t,) = _mk().synthesis.adaptyv_search_targets("EGFR", limit=2)
    assert t.id == "019a03da"
    assert t.name == "Cynomolgus EGFR Protein"


def test_adaptyv_search_targets_refuses_an_unrecognised_body(httpx_mock: HTTPXMock) -> None:
    httpx_mock.add_response(url=f"{BASE}/api/adaptyv/targets?search=EGFR&limit=10", json={"hits": []})
    with pytest.raises(LigandAIUnreadableResponse):
        _mk().synthesis.adaptyv_search_targets("EGFR")


@pytest.mark.parametrize(
    "call,path",
    [
        (lambda c: c.synthesis.recommend_linker("YHWYGYTPQNVI"), "/api/synthesis-checkout/recommend-linker"),
        (lambda c: c.synthesis.binding_orientation("YHWYGYTPQNVI", "job1"), "/api/synthesis-checkout/binding-orientation"),
        (lambda c: c.synthesis.generation_mask_guidance("n_biotin_peg4"), "/api/synthesis-checkout/mask-guidance"),
    ],
)
def test_a_missing_route_is_not_reported_as_a_missing_record(
    httpx_mock: HTTPXMock, call: Any, path: str
) -> None:
    httpx_mock.add_response(
        url=f"{BASE}{path}", method="POST", status_code=404,
        json={"error": "Not found", "path": path},
    )
    with pytest.raises(LigandAISynthesisUnavailable) as ei:
        call(_mk())
    assert "no such endpoint" in str(ei.value)


def test_a_handler_404_naming_a_record_stays_a_not_found(httpx_mock: HTTPXMock) -> None:
    path = "/api/synthesis-checkout/recommend-linker"
    httpx_mock.add_response(
        url=f"{BASE}{path}", method="POST", status_code=404, json={"error": "fold job not found"}
    )
    with pytest.raises(LigandAINotFoundError):
        _mk().synthesis.recommend_linker("YHWYGYTPQNVI", pdb_job_id="nope")


async def test_async_synthesis_stub_and_routes(
    httpx_mock: HTTPXMock, async_client: AsyncLigandAI
) -> None:
    httpx_mock.add_response(url=f"{BASE}/api/synthesis-checkout/cart", json=STUB)
    httpx_mock.add_response(url=f"{BASE}/api/synthesis/linker-options", json={"linkers": []})
    httpx_mock.add_response(
        url=f"{BASE}/api/adaptyv/targets?search=EGFR&limit=10", json={"items": []}
    )
    with pytest.raises(LigandAISynthesisUnavailable):
        await async_client.synthesis.get_cart()
    assert await async_client.synthesis.linker_options() == []
    assert await async_client.synthesis.adaptyv_search_targets("EGFR") == []
