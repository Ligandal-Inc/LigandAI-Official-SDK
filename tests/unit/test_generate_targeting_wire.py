# Copyright © 2025 Ligandal, Inc. All rights reserved.
"""Wire-format + validation-gate tests for the x4vt8 targeting hardening.

[bd-LIGANDAI_ALPHA_V2-x4vt8]

Covers ``Peptides.generate()``'s new ``avoid_shell_angstrom`` /
``hotspot_residues`` fields, the fixed ``avoidRegions`` wire shape (canonical
``start``/``end`` alongside the legacy ``startResidue``/``endResidue``, no
more silent 0-default), and that client-side validation raises BEFORE any
HTTP call reaches the mock (proven by asserting no request was made).

Follows the conventions in ``tests/unit/test_generate_flags.py``.
"""

from __future__ import annotations

import json

import pytest
from pytest_httpx import HTTPXMock

from ligandai import LigandAI
from ligandai.errors import LigandAIValidationError
from ligandai.types import ResidueRange

BASE = "http://api.ligandai.test"
GEN_URL = f"{BASE}/api/ptf/parallel/generate"

_QUEUED = {"sessionId": "sid_test", "status": "queued"}


@pytest.fixture
def client() -> LigandAI:
    return LigandAI(api_key="lgai_pro_test", base_url=BASE, max_retries=1)


def _body(httpx_mock: HTTPXMock) -> dict:
    req = httpx_mock.get_request()
    assert req is not None
    return json.loads(req.content)


# ---------------------------------------------------------------------------
# avoid_shell_angstrom on the wire
# ---------------------------------------------------------------------------


def test_avoid_shell_angstrom_emitted_camel_and_snake(httpx_mock: HTTPXMock, client: LigandAI) -> None:
    httpx_mock.add_response(url=GEN_URL, method="POST", json=_QUEUED)
    client.peptides.generate(gene="EGFR", avoid_shell_angstrom=12.5)
    body = _body(httpx_mock)
    assert body["avoidShellAngstrom"] == 12.5
    assert body["avoid_shell_angstrom"] == 12.5


def test_avoid_shell_angstrom_zero_disables_but_is_sent(httpx_mock: HTTPXMock, client: LigandAI) -> None:
    """0.0 is falsy but meaningful — MUST still appear on the wire."""
    httpx_mock.add_response(url=GEN_URL, method="POST", json=_QUEUED)
    with pytest.warns(match=r"disables the packing shell"):
        client.peptides.generate(gene="EGFR", avoid_shell_angstrom=0)
    body = _body(httpx_mock)
    assert body["avoidShellAngstrom"] == 0.0


def test_avoid_shell_angstrom_omitted_when_not_passed(httpx_mock: HTTPXMock, client: LigandAI) -> None:
    httpx_mock.add_response(url=GEN_URL, method="POST", json=_QUEUED)
    client.peptides.generate(gene="EGFR")
    body = _body(httpx_mock)
    assert "avoidShellAngstrom" not in body
    assert "avoid_shell_angstrom" not in body


def test_avoid_shell_angstrom_out_of_range_raises_before_http_call(
    httpx_mock: HTTPXMock, client: LigandAI
) -> None:
    with pytest.raises(LigandAIValidationError, match=r"between 0 and 50"):
        client.peptides.generate(gene="EGFR", avoid_shell_angstrom=100)
    assert len(httpx_mock.get_requests()) == 0


# ---------------------------------------------------------------------------
# hotspot_residues on the wire
# ---------------------------------------------------------------------------


def test_hotspot_residues_emitted_camel_and_snake(httpx_mock: HTTPXMock, client: LigandAI) -> None:
    httpx_mock.add_response(url=GEN_URL, method="POST", json=_QUEUED)
    client.peptides.generate(gene="EGFR", hotspot_residues=[792, 855])
    body = _body(httpx_mock)
    assert body["hotspotResidues"] == [792, 855]
    assert body["hotspot_residues"] == [792, 855]


def test_hotspot_residues_dict_shape_normalized_to_flat_ints(
    httpx_mock: HTTPXMock, client: LigandAI
) -> None:
    httpx_mock.add_response(url=GEN_URL, method="POST", json=_QUEUED)
    client.peptides.generate(
        gene="EGFR", hotspot_residues=[{"chain": "A", "residue": 792}, 855]
    )
    body = _body(httpx_mock)
    assert body["hotspotResidues"] == [792, 855]


def test_hotspot_residues_index_zero_raises_before_http_call(
    httpx_mock: HTTPXMock, client: LigandAI
) -> None:
    with pytest.raises(LigandAIValidationError, match=r"start at 1; got start=0"):
        client.peptides.generate(gene="EGFR", hotspot_residues=[45, 0])
    assert len(httpx_mock.get_requests()) == 0


# ---------------------------------------------------------------------------
# avoidRegions wire shape — canonical start/end alongside legacy keys, no
# silent 0-default landmine
# ---------------------------------------------------------------------------


def test_avoid_regions_wire_shape_has_canonical_and_legacy_keys(
    httpx_mock: HTTPXMock, client: LigandAI
) -> None:
    httpx_mock.add_response(url=GEN_URL, method="POST", json=_QUEUED)
    client.peptides.generate(
        gene="EGFR",
        avoid_residues=[ResidueRange(chain="A", start=36, end=46)],
    )
    body = _body(httpx_mock)
    assert body["avoidRegions"] == [
        {"chain": "A", "start": 36, "end": 46, "startResidue": 36, "endResidue": 46}
    ]


def test_avoid_residues_start_zero_raises_before_http_call(
    httpx_mock: HTTPXMock, client: LigandAI
) -> None:
    """The exact array-index mistake bd-x4vt8 exists to kill — must never
    reach the server, and must never silently become avoidRegions=[{...:0}]."""
    with pytest.raises(LigandAIValidationError, match=r"array index"):
        client.peptides.generate(
            gene="EGFR", avoid_residues=[{"chain": "A", "start": 0, "end": 10}]
        )
    assert len(httpx_mock.get_requests()) == 0


def test_target_residues_start_zero_raises_before_http_call(
    httpx_mock: HTTPXMock, client: LigandAI
) -> None:
    with pytest.raises(LigandAIValidationError, match=r"array index"):
        client.peptides.generate(
            gene="EGFR", target_residues=[{"chain": "A", "start": 0, "end": 5}]
        )
    assert len(httpx_mock.get_requests()) == 0


def test_target_residues_label_preserved_through_validation(
    httpx_mock: HTTPXMock, client: LigandAI
) -> None:
    """Validation must not strip per-region metadata (e.g. label) that the
    existing serialization path (and its test coverage) depends on."""
    httpx_mock.add_response(url=GEN_URL, method="POST", json=_QUEUED)
    client.peptides.generate(
        gene="EGFR",
        target_residues=[ResidueRange(chain="A", start=34, end=36, label="pocket A")],
        targeting_strategy="pocket_targeted",
    )
    body = _body(httpx_mock)
    assert body["targets"][0]["targetResidues"] == [
        {"chain": "A", "start": 34, "end": 36, "label": "pocket A"}
    ]


def test_region_span_over_limit_raises_before_http_call(
    httpx_mock: HTTPXMock, client: LigandAI
) -> None:
    with pytest.raises(LigandAIValidationError, match=r"sanity limit"):
        client.peptides.generate(
            gene="EGFR", avoid_residues=[{"chain": "A", "start": 1, "end": 6000}]
        )
    assert len(httpx_mock.get_requests()) == 0


def test_chain_malformed_raises_before_http_call(httpx_mock: HTTPXMock, client: LigandAI) -> None:
    with pytest.raises(LigandAIValidationError, match=r"chain must be"):
        client.peptides.generate(
            gene="EGFR", target_residues=[{"chain": "ABC", "start": 1, "end": 5}]
        )
    assert len(httpx_mock.get_requests()) == 0
