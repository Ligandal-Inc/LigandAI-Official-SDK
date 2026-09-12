# Copyright © 2026 Ligandal, Inc. All rights reserved.
"""Unit tests for the de novo peptide / binder flag on fold entities.

bd-dre-yt8kx (incident: a customer's Brd4ET binder folds were REFUSED because
the designed chain carried no peptide flag, so the MSA prefetch guard and the
Modal worker both classified a de novo design as a receptor and went looking for
an alignment that cannot exist).

The contract under test — ANY ONE caller-side signal must reach the wire as ALL
of ``isPeptide`` + ``is_peptide`` + ``use_msa: false``, because the four
platform consumers do not read the same keys:

  * ``msa_prefetch_guard._is_flagged_peptide``  — all four spellings
  * ``modal_workers/boltz2.py``                 — all four spellings
  * ``modal_workers/boltz2_batch.py``           — ``isPeptide`` / ``use_msa`` ONLY
  * ``modal_workers/boltz2_fda_v2.py``          — ``isPeptide`` / ``use_msa`` ONLY

Covers:
  - dict chains flagged by ``is_peptide`` / ``isPeptide`` / ``use_msa=False`` /
    ``role`` each normalise to the full wire triple
  - typed ``Sequence`` objects carry the flag (they used to DROP it)
  - ``Sequence.msa=False`` — a field that existed for releases and was never
    read — is now honored as ``use_msa=False``
  - the receptor chain is untouched and keeps its MSA
  - a ligand entity is never labelled a peptide
  - ``client.fold(receptor, binder)`` auto-flags chain B
"""

from __future__ import annotations

import pytest
from pytest_httpx import HTTPXMock

from ligandai import LigandAI
from ligandai.resources.peptides import is_peptide_chain
from ligandai.types import Sequence

BASE = "http://api.ligandai.test"

RECEPTOR = "MSAESGPGTRLRNLPVMGDGLETSQMSTTQAQAQPQPANAASTNPPPPETSNPNKPKRQTNQLQYLLRVVLKTLWKHQFAWPFQQPVD"
BINDER = "WEALYKKALEWLRKAFEEALKWLEEA"


@pytest.fixture
def pro_client() -> LigandAI:
    return LigandAI(api_key="lgai_pro_test", base_url=BASE, max_retries=1)


def _submit(httpx_mock: HTTPXMock, client: LigandAI, sequences: list) -> dict:
    """Submit a fold and return the request body that went out."""
    httpx_mock.add_response(
        url=f"{BASE}/api/folding/predict",
        json={"jobId": "fold_test_1", "status": "queued"},
    )
    client.peptides.fold(sequences=sequences, target_gene="BRD4")
    req = httpx_mock.get_requests()[-1]
    import json as _json

    return _json.loads(req.content)


def _chain(body: dict, chain_id: str) -> dict:
    matches = [e for e in body["entities"] if e["chainId"] == chain_id]
    assert len(matches) == 1, f"chain {chain_id} not found in {body['entities']}"
    return matches[0]


# ─── is_peptide_chain: the SDK mirror of the server-side guard ───────────────


@pytest.mark.parametrize(
    "entity",
    [
        {"isPeptide": True},
        {"is_peptide": True},
        {"use_msa": False},
        {"role": "peptide"},
        {"role": "binder"},
        {"role": "ligand_peptide"},
        {"role": "BINDER"},          # case-insensitive
        {"role": "  peptide  "},     # whitespace-tolerant
        {"entity_role": "binder"},
        {"chain_role": "peptide"},
    ],
)
def test_is_peptide_chain_accepts_every_spelling(entity: dict) -> None:
    assert is_peptide_chain(entity) is True


@pytest.mark.parametrize(
    "entity",
    [
        {},
        {"isPeptide": False},
        {"is_peptide": False},
        {"use_msa": True},
        {"role": "receptor"},
        {"role": "target"},
        {"geneName": "BRD4"},
    ],
)
def test_is_peptide_chain_rejects_a_receptor(entity: dict) -> None:
    assert is_peptide_chain(entity) is False


# ─── dict chains: every spelling reaches the wire as the full triple ─────────


@pytest.mark.parametrize(
    "flag",
    [
        {"is_peptide": True},
        {"isPeptide": True},
        {"use_msa": False},
        {"role": "peptide"},
        {"role": "binder"},
    ],
)
def test_any_flag_normalises_to_the_full_wire_triple(
    httpx_mock: HTTPXMock, pro_client: LigandAI, flag: dict
) -> None:
    body = _submit(
        httpx_mock,
        pro_client,
        [
            {"chainId": "A", "sequence": RECEPTOR, "geneName": "BRD4"},
            {"chainId": "B", "sequence": BINDER, **flag},
        ],
    )
    binder = _chain(body, "B")
    # boltz2_batch.py + boltz2_fda_v2.py read ONLY these two keys.
    assert binder["isPeptide"] is True
    assert binder["use_msa"] is False
    # boltz2.py + the prefetch guard also accept the snake_case spelling.
    assert binder["is_peptide"] is True


def test_receptor_chain_keeps_its_msa(
    httpx_mock: HTTPXMock, pro_client: LigandAI
) -> None:
    body = _submit(
        httpx_mock,
        pro_client,
        [
            {"chainId": "A", "sequence": RECEPTOR, "geneName": "BRD4"},
            {"chainId": "B", "sequence": BINDER, "is_peptide": True},
        ],
    )
    receptor = _chain(body, "A")
    assert receptor.get("isPeptide") is None
    assert receptor.get("is_peptide") is None
    assert receptor.get("use_msa") is not False
    assert receptor["geneName"] == "BRD4"


def test_explicit_receptor_opt_in_travels(
    httpx_mock: HTTPXMock, pro_client: LigandAI
) -> None:
    body = _submit(
        httpx_mock,
        pro_client,
        [
            {"chainId": "A", "sequence": RECEPTOR, "use_msa": True},
            {"chainId": "B", "sequence": BINDER, "is_peptide": True},
        ],
    )
    assert _chain(body, "A")["use_msa"] is True


def test_role_is_preserved_alongside_the_derived_flags(
    httpx_mock: HTTPXMock, pro_client: LigandAI
) -> None:
    body = _submit(
        httpx_mock,
        pro_client,
        [
            {"chainId": "A", "sequence": RECEPTOR},
            {"chainId": "B", "sequence": BINDER, "role": "binder"},
        ],
    )
    binder = _chain(body, "B")
    assert binder["role"] == "binder"
    assert binder["isPeptide"] is True


def test_ligand_entity_is_never_flagged_a_peptide(
    httpx_mock: HTTPXMock, pro_client: LigandAI
) -> None:
    body = _submit(
        httpx_mock,
        pro_client,
        [
            {"chainId": "A", "sequence": RECEPTOR},
            {"chainId": "B", "sequence": BINDER, "is_peptide": True},
            {"chainId": "X", "sequence": "ZN", "type": "ligand"},
        ],
    )
    ligand = _chain(body, "X")
    assert ligand["type"] == "ligand"
    assert ligand.get("isPeptide") is None
    assert ligand.get("use_msa") is None


# ─── typed Sequence objects used to DROP the flag entirely ──────────────────


def test_typed_sequence_carries_the_peptide_flag(
    httpx_mock: HTTPXMock, pro_client: LigandAI
) -> None:
    body = _submit(
        httpx_mock,
        pro_client,
        [
            Sequence(sequence=RECEPTOR, target_chain="A", target_gene="BRD4"),
            Sequence(sequence=BINDER, target_chain="B", is_peptide=True),
        ],
    )
    binder = _chain(body, "B")
    assert binder["isPeptide"] is True
    assert binder["is_peptide"] is True
    assert binder["use_msa"] is False
    assert _chain(body, "A").get("isPeptide") is None


def test_typed_sequence_role_and_use_msa_also_work(
    httpx_mock: HTTPXMock, pro_client: LigandAI
) -> None:
    body = _submit(
        httpx_mock,
        pro_client,
        [
            Sequence(sequence=RECEPTOR, target_chain="A"),
            Sequence(sequence=BINDER, target_chain="B", role="binder"),
            Sequence(sequence=BINDER, target_chain="C", use_msa=False),
        ],
    )
    for cid in ("B", "C"):
        assert _chain(body, cid)["isPeptide"] is True
        assert _chain(body, cid)["use_msa"] is False


def test_sequence_msa_false_is_no_longer_a_silent_no_op(
    httpx_mock: HTTPXMock, pro_client: LigandAI
) -> None:
    """``Sequence.msa`` shipped for releases and was NEVER read — a caller who
    wrote ``msa=False`` got a chain folded as a receptor. It is now honored as
    the deprecated alias of ``use_msa``."""
    body = _submit(
        httpx_mock,
        pro_client,
        [
            Sequence(sequence=RECEPTOR, target_chain="A"),
            Sequence(sequence=BINDER, target_chain="B", msa=False),
        ],
    )
    binder = _chain(body, "B")
    assert binder["use_msa"] is False
    assert binder["isPeptide"] is True


def test_explicit_use_msa_beats_the_deprecated_msa_alias(
    httpx_mock: HTTPXMock, pro_client: LigandAI
) -> None:
    body = _submit(
        httpx_mock,
        pro_client,
        [
            Sequence(sequence=RECEPTOR, target_chain="A", use_msa=True, msa=False),
            Sequence(sequence=BINDER, target_chain="B", is_peptide=True),
        ],
    )
    # use_msa=True wins over msa=False, so chain A stays a receptor.
    assert _chain(body, "A")["use_msa"] is True
    assert _chain(body, "A").get("isPeptide") is None


def test_sequence_msa_disabled_property() -> None:
    assert Sequence(sequence=BINDER, is_peptide=True).msa_disabled is True
    assert Sequence(sequence=BINDER, use_msa=False).msa_disabled is True
    assert Sequence(sequence=BINDER, msa=False).msa_disabled is True
    assert Sequence(sequence=BINDER, role="binder").msa_disabled is True
    assert Sequence(sequence=RECEPTOR).msa_disabled is False
    assert Sequence(sequence=RECEPTOR, use_msa=True).msa_disabled is False


def test_sequence_accepts_camelcase_aliases() -> None:
    s = Sequence(sequence=BINDER, isPeptide=True, useMsa=False)
    assert s.is_peptide is True
    assert s.use_msa is False


# ─── client.fold(receptor, binder) convenience ──────────────────────────────


def test_client_fold_two_positional_flags_chain_b(
    httpx_mock: HTTPXMock, pro_client: LigandAI
) -> None:
    httpx_mock.add_response(
        url=f"{BASE}/api/folding/predict",
        json={"jobId": "fold_test_2", "status": "queued"},
    )
    pro_client.fold(RECEPTOR, BINDER)
    import json as _json

    body = _json.loads(httpx_mock.get_requests()[-1].content)
    assert _chain(body, "B")["isPeptide"] is True
    assert _chain(body, "B")["use_msa"] is False
    assert _chain(body, "A").get("isPeptide") is None


# ─── cofold: an explicit flag beats chain position ──────────────────────────


def test_cofold_flag_beats_position(pro_client: LigandAI) -> None:
    from ligandai.resources.peptides import _cofold_chains

    # Binder listed FIRST. Without the flag, position would call it the receptor.
    chains = _cofold_chains(
        [
            {"id": "A", "sequence": BINDER, "is_peptide": True},
            {"id": "B", "sequence": RECEPTOR},
        ]
    )
    assert chains[0]["kind"] == "peptide"
    assert chains[1]["kind"] == "receptor"


def test_cofold_position_default_still_applies(pro_client: LigandAI) -> None:
    from ligandai.resources.peptides import _cofold_chains

    chains = _cofold_chains([{"sequence": RECEPTOR}, {"sequence": BINDER}])
    assert chains[0]["kind"] == "receptor"
    assert chains[1]["kind"] == "peptide"
