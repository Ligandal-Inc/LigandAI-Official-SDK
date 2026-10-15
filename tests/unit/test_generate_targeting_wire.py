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
    with pytest.raises(LigandAIValidationError, match=r"start at 1; got 0"):
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


# ---------------------------------------------------------------------------
# [bd-dre-i3h4y] A FLAT LIST OF RESNUMS MUST REACH THE WIRE AS RANGES.
#
# `target_residues=[140, ..., 259]` is the documented pocket-targeting call and
# `validate_region_list()` has accepted it since x4vt8 — but the body builder
# kept the ORIGINAL shape, so bare ints went out as ints and the server's
# shape-coercion step (liftSdkResidueField, residue-targeting-validation.ts)
# dropped every non-object entry. Measured consequence on a live campaign: all
# 105 runs passed 120-206 residues, `ptf_generation_runs.target_residues`
# persisted NULL on every one, and generation ran on the whole-chain
# geometric-pocket fallback while the call reported success.
# ---------------------------------------------------------------------------


def test_flat_resnum_list_serializes_as_ranges_not_bare_ints(
    httpx_mock: HTTPXMock, client: LigandAI
) -> None:
    httpx_mock.add_response(url=GEN_URL, method="POST", json=_QUEUED)
    client.peptides.generate(gene="CD34", target_residues=list(range(140, 260)))
    tr = _body(httpx_mock)["targets"][0]["targetResidues"]
    assert tr == [{"start": 140, "end": 259}], tr
    # Not a single bare int may survive — that is what the server dropped.
    assert all(isinstance(r, dict) for r in tr)


def test_non_contiguous_resnums_stay_separate_ranges(
    httpx_mock: HTTPXMock, client: LigandAI
) -> None:
    httpx_mock.add_response(url=GEN_URL, method="POST", json=_QUEUED)
    client.peptides.generate(gene="CD34", target_residues=[140, 141, 145])
    tr = _body(httpx_mock)["targets"][0]["targetResidues"]
    assert tr == [{"start": 140, "end": 141}, {"start": 145, "end": 145}], tr


def test_flat_resnum_list_sets_pocket_targeted_strategy(
    httpx_mock: HTTPXMock, client: LigandAI
) -> None:
    httpx_mock.add_response(url=GEN_URL, method="POST", json=_QUEUED)
    client.peptides.generate(gene="CD34", target_residues=[140, 141])
    assert _body(httpx_mock)["targets"][0]["targetingStrategy"] == "pocket_targeted"


def test_explicit_ranges_keep_their_chain_and_label(
    httpx_mock: HTTPXMock, client: LigandAI
) -> None:
    """A hand-written region is passed through UNCHANGED — collapsing applies
    only to bare resnums, so per-region chain/label survive exactly as before."""
    httpx_mock.add_response(url=GEN_URL, method="POST", json=_QUEUED)
    client.peptides.generate(
        gene="CD34",
        target_residues=[{"chain": "A", "start": 140, "end": 150, "label": "site1"}],
    )
    tr = _body(httpx_mock)["targets"][0]["targetResidues"]
    assert tr == [{"chain": "A", "start": 140, "end": 150, "label": "site1"}], tr


def test_mixed_scalar_and_range_input_both_survive(
    httpx_mock: HTTPXMock, client: LigandAI
) -> None:
    httpx_mock.add_response(url=GEN_URL, method="POST", json=_QUEUED)
    client.peptides.generate(
        gene="CD34", target_residues=[{"chain": "A", "start": 10, "end": 12}, 140, 141]
    )
    tr = _body(httpx_mock)["targets"][0]["targetResidues"]
    assert {"chain": "A", "start": 10, "end": 12} in tr
    assert {"start": 140, "end": 141} in tr
    assert len(tr) == 2, tr


def test_avoid_residues_flat_list_also_serializes_as_ranges(
    httpx_mock: HTTPXMock, client: LigandAI
) -> None:
    httpx_mock.add_response(url=GEN_URL, method="POST", json=_QUEUED)
    client.peptides.generate(gene="CD34", avoid_residues=[36, 37, 38])
    body = _body(httpx_mock)
    avoid = body["avoidRegions"]
    assert len(avoid) == 1, avoid
    assert (avoid[0]["start"], avoid[0]["end"]) == (36, 38), avoid
    # chain is null, never guessed as "A" — a fabricated chain short-circuits
    # FastAPI's chain-agnostic resnum fallback and can exclude the wrong residue.
    assert avoid[0]["chain"] is None, avoid


def test_flat_resnum_list_also_reaches_top_level_target_regions(
    httpx_mock: HTTPXMock, client: LigandAI
) -> None:
    """The internal key Express destructures must carry the pocket too, so the
    transport does not depend on a single server-side lift. [bd-dre-i3h4y]"""
    httpx_mock.add_response(url=GEN_URL, method="POST", json=_QUEUED)
    client.peptides.generate(gene="CD34", target_residues=list(range(140, 260)))
    tr = _body(httpx_mock)["targetRegions"]
    assert tr == [
        {"chain": None, "start": 140, "end": 259, "startResidue": 140, "endResidue": 259}
    ], tr


def test_target_regions_absent_when_no_pocket_requested(
    httpx_mock: HTTPXMock, client: LigandAI
) -> None:
    """A caller who never asked for a pocket sends a body byte-identical to before."""
    httpx_mock.add_response(url=GEN_URL, method="POST", json=_QUEUED)
    client.peptides.generate(gene="CD34")
    assert "targetRegions" not in _body(httpx_mock)
