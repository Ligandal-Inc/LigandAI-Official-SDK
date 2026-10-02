# Copyright © 2026 Ligandal, Inc. All rights reserved.
"""Wrapped-list envelope reads, against the bodies the server really sends.

Every fixture in ``tests/fixtures/wrapped_list_envelopes_20261002.json`` was
captured on 2026-10-02 from production blue with the agent key: keys verbatim,
rows trimmed, sequences/ids/free text redacted. ``_meta.live_row_counts`` holds
how many rows each untrimmed body carried — the number the SDK was answering
``0`` to.

⛔ THE DEFECT. Every list-returning method here was written as
``payload.get("<guessed key>", [])``. Where the guess was wrong the method
returned ``[]`` — a measurement of zero indistinguishable from "the server
holds nothing". Measured on 0.16.1 against that server:

======================================  ========  =======================
method                                  returned  server was holding
======================================  ========  =======================
``receptors.search("EGFR")``            0         38 (``complexes``)
``diseases.search("cancer")``           0         47 (``diseases``)
``memory.search(...)`` / ``.list()``    0         5 each (``memories``)
``discovery.organ_systems()``           0         13 (``organSystems``)
``peptides.search_by_pocket("EGFR")``   0         100 (``results``)
``peptides.get_elite(gene=...)``        raised    232 sessions (``sessions``)
``receptors.oligomeric_states()``       255 dicts 255 names, typed ``List[str]``
``diseases.gene_mutations("EGFR")``     raised    100 ClinVar rows
======================================  ========  =======================

The npm SDK shipped the same fix for the same route in 32f3afc
(bd-dre-3imtv.1). [bd-dre-3imtv.3]
"""

from __future__ import annotations

import json
import pathlib
from typing import Any

import pytest

from ligandai.errors import LigandAIUnreadableResponse
from ligandai.resources._base import unwrap_rows
from ligandai.resources.diseases import AsyncDiseases, Diseases
from ligandai.resources.discovery import AsyncDiscovery, Discovery
from ligandai.resources.memory import AsyncMemory, Memory
from ligandai.resources.peptides import AsyncPeptides, Peptides
from ligandai.resources.receptors import AsyncReceptors, Receptors
from ligandai.types import Disease, MemoryItem, Mutation

FIXTURE = (
    pathlib.Path(__file__).resolve().parent.parent
    / "fixtures"
    / "wrapped_list_envelopes_20261002.json"
)
LIVE: dict[str, Any] = json.loads(FIXTURE.read_text())
COUNTS: dict[str, int] = LIVE["_meta"]["live_row_counts"]


class StubTransport:
    """Answers each request with the next queued body, recording the calls."""

    def __init__(self, *bodies: Any) -> None:
        self._bodies = list(bodies)
        self.calls: list[tuple[str, str]] = []

    def request(self, method: str, path: str, **kwargs: Any) -> Any:
        self.calls.append((method, path))
        return self._bodies.pop(0)


class AsyncStubTransport(StubTransport):
    async def request(self, method: str, path: str, **kwargs: Any) -> Any:  # type: ignore[override]
        self.calls.append((method, path))
        return self._bodies.pop(0)


# ─────────────────────────────────────────────────────────────────────────────
# unwrap_rows: the shared contract. An unrecognised body RAISES — it is never
# reported as zero rows, which is the whole point of the helper.
# ─────────────────────────────────────────────────────────────────────────────


def test_unwrap_rows_prefers_the_live_key_then_the_legacy_ones() -> None:
    assert unwrap_rows({"complexes": [1, 2]}, ("complexes", "results"), what="x") == [1, 2]
    # A server still on the old envelope keeps working.
    assert unwrap_rows({"results": [3]}, ("complexes", "results"), what="x") == [3]
    # First key holding a LIST wins; a non-list under an earlier key is skipped.
    assert unwrap_rows({"complexes": None, "results": [4]}, ("complexes", "results"), what="x") == [
        4
    ]
    # A bare list body is still a list.
    assert unwrap_rows([5], ("complexes",), what="x") == [5]
    # An honest empty list stays empty — this must NOT raise.
    assert unwrap_rows({"complexes": []}, ("complexes", "results"), what="x") == []


@pytest.mark.parametrize(
    "payload",
    [
        {"query": "EGFR", "total": 38},  # envelope the SDK does not know
        {},
        None,
        "not json at all",
        42,
    ],
)
def test_unwrap_rows_refuses_to_report_zero_for_a_body_it_cannot_read(payload: Any) -> None:
    with pytest.raises(LigandAIUnreadableResponse) as exc:
        unwrap_rows(payload, ("complexes", "results"), what="GET /api/receptordb/search")
    assert exc.value.cause in {"unrecognised_envelope", "empty_body"}
    # The message must say what was attempted and must not read like "no hits".
    assert "/api/receptordb/search" in str(exc.value)
    assert "zero rows" in str(exc.value)


def test_unwrap_rows_error_carries_the_body_for_diagnosis() -> None:
    body = {"query": "EGFR", "total": 38}
    with pytest.raises(LigandAIUnreadableResponse) as exc:
        unwrap_rows(body, ("complexes",), what="GET /api/receptordb/search")
    assert exc.value.response == body
    assert "query, total" in str(exc.value)


# ─────────────────────────────────────────────────────────────────────────────
# receptors — the reported bug: EGFR, 38 complexes, answered as zero.
# ─────────────────────────────────────────────────────────────────────────────


def test_receptors_search_reads_the_complexes_envelope() -> None:
    body = LIVE["receptordb_search_EGFR"]
    assert "results" not in body  # the key 0.16.1 was reading
    assert body["total"] == COUNTS["receptordb_search_EGFR"] == 38

    t = StubTransport(body)
    out = Receptors(t).search("EGFR", limit=100)  # type: ignore[arg-type]

    assert len(out) == len(body["complexes"])
    assert out[0].complex_id == body["complexes"][0]["complexId"]
    assert t.calls == [("GET", "/api/receptordb/search")]


@pytest.mark.asyncio
async def test_async_receptors_search_reads_the_complexes_envelope() -> None:
    body = LIVE["receptordb_search_EGFR"]
    out = await AsyncReceptors(AsyncStubTransport(body)).search("EGFR")  # type: ignore[arg-type]
    assert len(out) == len(body["complexes"])


def test_receptors_search_raises_on_an_unknown_envelope_instead_of_zero_hits() -> None:
    # Exactly the live body minus its rows key: a server shape we do not know.
    body = {k: v for k, v in LIVE["receptordb_search_EGFR"].items() if k != "complexes"}
    with pytest.raises(LigandAIUnreadableResponse):
        Receptors(StubTransport(body)).search("EGFR")  # type: ignore[arg-type]


def test_receptors_search_still_reads_a_legacy_results_envelope() -> None:
    rows = LIVE["receptordb_search_EGFR"]["complexes"]
    out = Receptors(StubTransport({"results": rows})).search("EGFR")  # type: ignore[arg-type]
    assert len(out) == len(rows)


def test_receptors_by_gene_reads_complexes_and_genes_reads_genes() -> None:
    by_gene = LIVE["receptordb_by_gene_EGFR"]
    out = Receptors(StubTransport(by_gene)).by_gene("EGFR")  # type: ignore[arg-type]
    assert len(out) == len(by_gene["complexes"])

    genes = LIVE["receptordb_genes"]
    assert Receptors(StubTransport(genes)).genes() == genes["genes"]  # type: ignore[arg-type]


def test_oligomeric_states_returns_names_not_rows() -> None:
    body = LIVE["receptordb_oligomeric_states"]
    assert isinstance(body, list) and isinstance(body[0], dict)  # bare list of {state, count}

    out = Receptors(StubTransport(body)).oligomeric_states()  # type: ignore[arg-type]

    assert out == [row["state"] for row in body]
    assert all(isinstance(s, str) for s in out)  # the declared type, finally true
    assert "homodimer" in out  # what `search(oligomeric_state=...)` takes


@pytest.mark.asyncio
async def test_async_oligomeric_states_returns_names() -> None:
    body = LIVE["receptordb_oligomeric_states"]
    out = await AsyncReceptors(AsyncStubTransport(body)).oligomeric_states()  # type: ignore[arg-type]
    assert out == [row["state"] for row in body]


# ─────────────────────────────────────────────────────────────────────────────
# diseases — envelope key AND a row shape the model rejected.
# ─────────────────────────────────────────────────────────────────────────────


def test_diseases_search_reads_the_diseases_envelope() -> None:
    body = LIVE["disease_search_cancer"]
    assert "results" not in body
    assert COUNTS["disease_search_cancer"] == 47

    out = Diseases(StubTransport(body)).search("cancer")  # type: ignore[arg-type]

    assert len(out) == len(body["diseases"])
    # `name` is the server's `condition`; the row has no `name` at all.
    assert "name" not in body["diseases"][0]
    assert out[0].name == body["diseases"][0]["condition"]
    assert out[0].gene == body["diseases"][0]["gene"]


@pytest.mark.asyncio
async def test_async_diseases_search_reads_the_diseases_envelope() -> None:
    body = LIVE["disease_search_cancer"]
    out = await AsyncDiseases(AsyncStubTransport(body)).search("cancer")  # type: ignore[arg-type]
    assert len(out) == len(body["diseases"])


def test_disease_row_validates_without_a_name_field() -> None:
    row = LIVE["disease_search_cancer"]["diseases"][0]
    d = Disease.model_validate(row)
    assert d.name and d.id
    # Unmodelled columns stay reachable rather than being dropped.
    assert d.model_extra is not None and "inheritance" in d.model_extra


def test_gene_mutations_validates_real_clinvar_rows() -> None:
    rows = LIVE["disease_gene_mutations_EGFR"]
    assert "variant" not in rows[0]  # the field Mutation used to REQUIRE
    assert COUNTS["disease_gene_mutations_EGFR"] == 100

    out = Diseases(StubTransport(rows)).gene_mutations("EGFR")  # type: ignore[arg-type]

    assert len(out) == len(rows)
    assert out[0].gene == "EGFR"
    assert out[0].variant is None
    assert out[0].model_extra is not None
    assert out[0].model_extra["hgvs_genomic"] == rows[0]["hgvs_genomic"]


def test_mutation_consequence_reads_molecular_consequence() -> None:
    m = Mutation.model_validate({"gene": "EGFR", "molecular_consequence": "missense_variant"})
    assert m.consequence == "missense_variant"


# ─────────────────────────────────────────────────────────────────────────────
# memory — both routes answer {"memories": [...]}, read as results/items.
# ─────────────────────────────────────────────────────────────────────────────


def test_memory_search_and_list_read_the_memories_envelope() -> None:
    search_body = LIVE["episodic_memory_search"]
    list_body = LIVE["episodic_memory_list"]
    assert "results" not in search_body and "items" not in list_body
    assert COUNTS["episodic_memory_search"] == COUNTS["episodic_memory_list"] == 5

    m = Memory(StubTransport(search_body, list_body))  # type: ignore[arg-type]
    found = m.search("EGFR", limit=5)
    listed = m.list(limit=5)

    assert len(found) == len(search_body["memories"])
    assert len(listed) == len(list_body["memories"])
    # The rows key it `type`, not `memoryType`.
    assert "memoryType" not in search_body["memories"][0]
    assert found[0].memory_type == search_body["memories"][0]["type"]


@pytest.mark.asyncio
async def test_async_memory_search_reads_the_memories_envelope() -> None:
    body = LIVE["episodic_memory_search"]
    out = await AsyncMemory(AsyncStubTransport(body)).search("EGFR")  # type: ignore[arg-type]
    assert len(out) == len(body["memories"])


def test_memory_item_accepts_both_type_and_memory_type() -> None:
    assert MemoryItem.model_validate({"id": 1, "content": "c", "type": "fold_result"}).memory_type == (
        "fold_result"
    )
    assert MemoryItem.model_validate(
        {"id": 1, "content": "c", "memoryType": "decision"}
    ).memory_type == "decision"
    # save() still sends `memoryType` on the way out.
    dumped = MemoryItem.model_validate({"id": 1, "content": "c", "type": "x"}).model_dump(
        by_alias=True
    )
    assert dumped["memoryType"] == "x"


# ─────────────────────────────────────────────────────────────────────────────
# discovery — organSystems, read as `systems`, and the rows are objects.
# ─────────────────────────────────────────────────────────────────────────────


def test_organ_systems_reads_organsystems_and_returns_names() -> None:
    body = LIVE["transcriptomics_organ_systems"]
    assert "systems" not in body
    assert COUNTS["transcriptomics_organ_systems"] == 13

    out = Discovery(StubTransport(body)).organ_systems()  # type: ignore[arg-type]

    assert out == [s["name"] for s in body["organSystems"]]
    assert all(isinstance(s, str) for s in out)


@pytest.mark.asyncio
async def test_async_organ_systems_returns_names() -> None:
    body = LIVE["transcriptomics_organ_systems"]
    out = await AsyncDiscovery(AsyncStubTransport(body)).organ_systems()  # type: ignore[arg-type]
    assert out == [s["name"] for s in body["organSystems"]]


def test_tissues_reads_the_tissues_envelope() -> None:
    body = LIVE["transcriptomics_tissues"]
    assert Discovery(StubTransport(body)).tissues() == body["tissues"]  # type: ignore[arg-type]


# ─────────────────────────────────────────────────────────────────────────────
# peptides — prior designs for a pocket, and a session's elite hits.
# ─────────────────────────────────────────────────────────────────────────────


def test_search_by_pocket_reads_the_results_envelope() -> None:
    body = LIVE["ptf_peptides_by_pocket_EGFR"]
    assert "peptides" not in body  # the key 0.16.1 was reading
    assert COUNTS["ptf_peptides_by_pocket_EGFR"] == 100

    out = Peptides(StubTransport(body)).search_by_pocket("EGFR")  # type: ignore[arg-type]

    assert len(out) == len(body["results"])
    assert out[0].gene == "EGFR"


@pytest.mark.asyncio
async def test_async_search_by_pocket_reads_the_results_envelope() -> None:
    body = LIVE["ptf_peptides_by_pocket_EGFR"]
    out = await AsyncPeptides(AsyncStubTransport(body)).search_by_pocket("EGFR")  # type: ignore[arg-type]
    assert len(out) == len(body["results"])


def test_get_elite_resolves_the_session_from_the_sessions_rows() -> None:
    sessions = LIVE["ptf_sessions_by_gene_EGFR"]
    elite = LIVE["ptf_parallel_elite"]
    assert "id" not in sessions  # what 0.16.1 read, hence "No active session" for every gene
    assert COUNTS["ptf_sessions_by_gene_EGFR"] == 232

    t = StubTransport(sessions, elite)
    out = Peptides(t).get_elite(gene="EGFR")  # type: ignore[arg-type]

    sid = sessions["sessions"][0]["sessionId"]
    assert t.calls == [
        ("GET", "/api/ptf/sessions/by-gene/EGFR"),
        ("GET", f"/api/ptf/parallel/{sid}/elite"),
    ]
    assert out == []  # this session's threshold found no elites — an honest zero


def test_get_elite_reads_the_elitehits_envelope() -> None:
    row = dict(LIVE["ptf_peptides_by_pocket_EGFR"]["results"][0])
    body = {**LIVE["ptf_parallel_elite"], "eliteHits": [row], "totalEliteHits": 1}
    out = Peptides(StubTransport(body)).get_elite(session_id="s1")  # type: ignore[arg-type]
    assert len(out) == 1
    assert out[0].gene == row["gene"]


def test_get_elite_raises_on_an_unknown_elite_envelope() -> None:
    body = {k: v for k, v in LIVE["ptf_parallel_elite"].items() if k != "eliteHits"}
    with pytest.raises(LigandAIUnreadableResponse):
        Peptides(StubTransport(body)).get_elite(session_id="s1")  # type: ignore[arg-type]
