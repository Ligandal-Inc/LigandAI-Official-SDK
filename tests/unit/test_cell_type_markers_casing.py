# Copyright © 2026 Ligandal, Inc. All rights reserved.
"""cell_type_markers spoke camelCase to a snake_case route, so every call 400'd.

The two marker routes disagree and the body is forwarded verbatim:

    POST /api/transcriptomics/top-markers        destructures targetTissues, topN, ...
    POST /api/transcriptomics/cell-type-markers  destructures scrna_tissue, top_n, ...

So ``scrna_tissue`` arrived undefined and the handler answered
``400 Missing required parameters: scrna_tissue, target_cell_types (array)``.
Measured live 2026-10-22 with a cell type and tissue both read back from the server.

With the request fixed the response then failed its own model, because these rows
carry no ``gene`` and no ``gene_name`` -- the symbol is ``Primary_gene`` in a
UniProt-style row. Both halves are pinned here, each in the casing the route it
talks to actually uses, so a future "tidy the casing" pass cannot silently
reintroduce either.
"""

from __future__ import annotations

import json as _json

import pytest
from pytest_httpx import HTTPXMock

from ligandai import LigandAI
from ligandai.types import MarkerResponse

BASE = "http://api.ligandai.test"

# One real row, keys exactly as the live route returned them (values trimmed).
LIVE_ROW = {
    "Primary_gene": "ASGR1",
    "Ensembl_gene": "ENSG00000134089",
    "UniProt_accession": "P07306",
    "Protein_name": "Asialoglycoprotein receptor 1",
    "Specificity_index": 4.421822303205998,
    "SI_within_tissue": 2.1,
    "SI_vs_parent": 3.0,
    "Transmembrane": True,
    "AlphaFoldDB": "P07306",
    "DrugBank": "",
    "PDB": "1DV8",
    "Max": 9.1,
    "Mean": 4.8,
    "method": "scrna_si",
    "parent_tissue": "liver",
    "tissue_expression": {},
    "cell_type_breakdown": {},
}


@pytest.fixture
def client() -> LigandAI:
    return LigandAI(api_key="lgai_test", base_url=BASE, max_retries=1)


def test_cell_type_markers_sends_snake_case(client: LigandAI, httpx_mock: HTTPXMock) -> None:
    httpx_mock.add_response(
        url=f"{BASE}/api/scrna/cell-type-markers",
        json={"markers": [LIVE_ROW], "count": 1},
    )
    client.discovery.cell_type_markers(
        scrna_tissue="liver",
        target_cell_types=["hepatocytes"],
        exclude_tissues=["whole_blood"],
        acceptable_offtargets=["spleen"],
        top_n=5,
    )
    body = _json.loads(httpx_mock.get_requests()[0].content)
    assert body["scrna_tissue"] == "liver"
    assert body["target_cell_types"] == ["hepatocytes"]
    assert body["exclude_tissues"] == ["whole_blood"]
    assert body["acceptable_offtargets"] == ["spleen"]
    assert body["top_n"] == 5 and body["receptor_only"] is True
    for camel in ("scrnaTissue", "targetCellTypes", "topN", "receptorOnly", "excludeTissues"):
        assert camel not in body, f"{camel} is the other route's spelling; this one 400s on it"


def test_tissue_markers_keeps_camel_case(client: LigandAI, httpx_mock: HTTPXMock) -> None:
    """The sibling route really does want camelCase -- fixing one must not 'fix' the other."""
    httpx_mock.add_response(
        url=f"{BASE}/api/transcriptomics/top-markers",
        json={"markers": [LIVE_ROW], "count": 1},
    )
    client.discovery.tissue_markers(target_tissues=["liver"], exclude_tissues=["spleen"], top_n=5)
    body = _json.loads(httpx_mock.get_requests()[0].content)
    assert body["targetTissues"] == ["liver"]
    assert body["excludeTissues"] == ["spleen"]
    assert body["topN"] == 5 and body["receptorOnly"] is True
    assert "scrna_tissue" not in body and "top_n" not in body


def test_a_uniprot_style_row_yields_a_gene_and_an_si() -> None:
    """Before this, a real response raised ``top.N.gene Field required`` and the
    caller got an exception instead of its ranked markers."""
    response = MarkerResponse.model_validate({"markers": [LIVE_ROW], "count": 1})
    assert len(response.top) == 1
    assert response.top[0].gene == "ASGR1"
    assert response.top[0].si == pytest.approx(4.421822303205998)
    assert response.total == 1


def test_gene_name_still_wins_when_present() -> None:
    """The pre-existing normalization is not displaced by the new keys."""
    row = {**LIVE_ROW, "gene_name": "FROM_GENE_NAME"}
    assert MarkerResponse.model_validate({"markers": [row]}).top[0].gene == "FROM_GENE_NAME"


def test_an_explicit_gene_is_never_overwritten() -> None:
    row = {**LIVE_ROW, "gene": "ALREADY_SET"}
    assert MarkerResponse.model_validate({"top": [row]}).top[0].gene == "ALREADY_SET"


def test_an_explicit_si_is_never_overwritten() -> None:
    row = {**LIVE_ROW, "gene": "G", "si": 1.5}
    assert MarkerResponse.model_validate({"top": [row]}).top[0].si == pytest.approx(1.5)


def test_an_ensembl_id_is_the_last_resort_not_the_first() -> None:
    """An Ensembl id is an identifier, not the symbol the other fields are keyed by."""
    row = {k: v for k, v in LIVE_ROW.items() if k != "Primary_gene"}
    assert MarkerResponse.model_validate({"markers": [row]}).top[0].gene == "ENSG00000134089"


def test_a_row_with_no_recognizable_gene_key_still_raises() -> None:
    """The normalizer must not invent a gene; a genuinely unknown shape has to surface."""
    row = {"Specificity_index": 1.0, "Protein_name": "something"}
    with pytest.raises(Exception):
        MarkerResponse.model_validate({"markers": [row]})


def test_an_empty_body_is_still_an_empty_ranking() -> None:
    assert MarkerResponse.model_validate({}).top == []
