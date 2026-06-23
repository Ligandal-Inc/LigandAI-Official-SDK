# Copyright © 2026 Ligandal, Inc. All rights reserved.
"""Comparative-transcriptomics SDK surface [bd-LIGANDAI_ALPHA_V2-k5usa].

Covers:
- geneset / gtex / custom group types on TargetGroup / ReferenceGroup
- compare_groups marker normalization (server ``markers`` w/ ``gene_name`` → ``results``)
- compare_targets shared/differential split across groups
- transport_vasculome specificity output (specificity_weight forwarded;
  specificity overlay fields parsed; raw transport_receptors row coercion)
- compare_bbb_vs_brain recipe (geneset BBB target vs gtex brain reference)
"""

from __future__ import annotations

import json as _json

import pytest
from pytest_httpx import HTTPXMock

from ligandai import LigandAI
from ligandai.types import (
    BBBReceptor,
    ComparisonResponse,
    ReferenceGroup,
    TargetGroup,
)

BASE = "http://api.ligandai.test"


@pytest.fixture
def ent_client() -> LigandAI:
    # enterprise key so transport_vasculome's client-side feature gate passes.
    return LigandAI(api_key="lgai_ent_test", base_url=BASE, max_retries=1)


@pytest.fixture
def pro_client() -> LigandAI:
    return LigandAI(api_key="lgai_pro_test", base_url=BASE, max_retries=1)


# -- Group model types -----------------------------------------------------


def test_target_group_geneset_serialization() -> None:
    g = TargetGroup(name="BBB shuttles", type="geneset", genes=["TFRC", "LRP1", "FCGRT"])
    dumped = g.model_dump(by_alias=True)
    assert dumped["type"] == "geneset"
    assert dumped["genes"] == ["TFRC", "LRP1", "FCGRT"]


def test_target_group_gtex_serialization() -> None:
    g = TargetGroup(name="cortex", type="gtex", tissue="brain_cortex")
    assert g.model_dump(by_alias=True)["tissue"] == "brain_cortex"


def test_target_group_custom_aliases() -> None:
    g = TargetGroup.model_validate(
        {"name": "myset", "type": "custom", "datasetId": 42, "cellTypes": ["neuron"]}
    )
    assert g.dataset_id == 42
    assert g.cell_types == ["neuron"]
    assert g.model_dump(by_alias=True)["datasetId"] == 42


def test_reference_group_geneset() -> None:
    g = ReferenceGroup(name="pathwayB", type="geneset", genes=["AKT1", "MTOR"])
    assert g.type == "geneset"
    assert g.genes == ["AKT1", "MTOR"]


# -- compare_groups marker normalization -----------------------------------


def test_compare_groups_normalizes_markers(httpx_mock: HTTPXMock, pro_client: LigandAI) -> None:
    httpx_mock.add_response(
        url=f"{BASE}/api/transcriptomics/compare-groups",
        method="POST",
        json={
            "success": True,
            "mode": "compare",
            "method": "fold_change",
            "markers": [
                {"gene_name": "TFRC", "fold_change": 5.0, "target_tpm": 100, "ref_tpm": 20},
                {"gene_name": "ACTB", "fold_change": 1.1, "target_tpm": 50, "ref_tpm": 45},
            ],
            "metadata": {"target_genes": 2},
        },
    )
    target = TargetGroup(name="t", type="geneset", genes=["TFRC", "ACTB"])
    ref = ReferenceGroup(name="brain", type="gtex", tissue="brain_cortex")
    resp = pro_client.discovery.compare_groups(target_group=target, reference_groups=[ref])
    assert isinstance(resp, ComparisonResponse)
    assert resp.method == "fold_change"
    assert len(resp.results) == 2
    assert resp.results[0].gene == "TFRC"
    assert resp.results[0].fold_change == 5.0
    assert resp.results[0].ref_tpm == 20


# -- compare_targets shared/differential -----------------------------------


def test_compare_targets_shared_differential(httpx_mock: HTTPXMock, pro_client: LigandAI) -> None:
    httpx_mock.add_response(
        url=f"{BASE}/api/transcriptomics/compare-groups",
        method="POST",
        json={
            "success": True,
            "mode": "compare",
            "method": "fold_change",
            "markers": [
                # target-enriched → differential
                {"gene_name": "TFRC", "fold_change": 8.0, "target_tpm": 80, "ref_tpm": 1},
                # co-expressed → shared
                {"gene_name": "ACTB", "fold_change": 1.0, "target_tpm": 50, "ref_tpm": 50},
                # target-only (no ref) → differential
                {"gene_name": "GFAP", "fold_change": 100.0, "target_tpm": 100, "ref_tpm": 0},
            ],
        },
    )
    target = TargetGroup(name="BBB", type="geneset", genes=["TFRC", "ACTB", "GFAP"])
    ref = ReferenceGroup(name="cortex", type="gtex", tissue="brain_cortex")
    resp = pro_client.discovery.compare_targets([target, ref])
    assert "ACTB" in (resp.shared_genes or [])
    assert "TFRC" in (resp.differential_genes or [])
    assert "GFAP" in (resp.differential_genes or [])
    assert "ACTB" not in (resp.differential_genes or [])


def test_compare_targets_forwards_geneset_and_custom(httpx_mock: HTTPXMock, pro_client: LigandAI) -> None:
    httpx_mock.add_response(
        url=f"{BASE}/api/transcriptomics/compare-groups",
        method="POST",
        json={"success": True, "mode": "compare", "markers": []},
    )
    target = TargetGroup(name="pathwayA", type="geneset", genes=["AKT1", "MTOR"])
    custom_ref = ReferenceGroup.model_validate(
        {"name": "myUpload", "type": "custom", "datasetId": 7, "cellTypes": ["astrocyte"]}
    )
    gtex_ref = ReferenceGroup(name="cortex", type="gtex", tissue="brain_cortex")
    pro_client.discovery.compare_targets([target, custom_ref, gtex_ref])
    req = httpx_mock.get_request()
    body = _json.loads(req.read())
    assert body["targetGroup"]["type"] == "geneset"
    assert body["targetGroup"]["genes"] == ["AKT1", "MTOR"]
    assert len(body["referenceGroups"]) == 2
    types = {g["type"] for g in body["referenceGroups"]}
    assert types == {"custom", "gtex"}
    # custom ref carries dataset id + cell types through aliases
    custom = next(g for g in body["referenceGroups"] if g["type"] == "custom")
    assert custom["datasetId"] == 7
    assert custom["cellTypes"] == ["astrocyte"]


def test_compare_targets_requires_groups(pro_client: LigandAI) -> None:
    with pytest.raises(ValueError):
        pro_client.discovery.compare_targets([])


# -- transport_vasculome specificity output --------------------------------


def test_transport_vasculome_forwards_specificity_weight(httpx_mock: HTTPXMock, ent_client: LigandAI) -> None:
    httpx_mock.add_response(
        url=f"{BASE}/api/transport-vasculome/query",
        method="POST",
        json={"receptors": [{"uniprot_gene": "TFRC", "monovalent_score": 0.9}]},
    )
    ent_client.discovery.transport_vasculome(modality="monovalent", specificity_weight=0.5)
    req = httpx_mock.get_request()
    body = _json.loads(req.read())
    assert body["specificityWeight"] == 0.5


def test_transport_vasculome_parses_specificity_overlay(httpx_mock: HTTPXMock, ent_client: LigandAI) -> None:
    httpx_mock.add_response(
        url=f"{BASE}/api/transport-vasculome/query",
        method="POST",
        json={
            "receptors": [
                {
                    "uniprot_gene": "SLC2A1",
                    "monovalent_score": 0.7,
                    "specificity_index": 0.12,
                    "enrichment": 1.05,
                    "gtex_max_tpm": 331.98,
                    "top_gtex_tissues": [["lung", 331.98], ["adipose_subcutaneous", 319.8]],
                    "broadly_shared": True,
                    "broadly_shared_reason": "low CNS specificity (specificity_index=0.120)",
                    "combined_score": 0.41,
                },
                # canonical shuttle with NO expression row (null-safe path)
                {"uniprot_gene": "TFRC", "monovalent_score": 0.95, "broadly_shared": False},
            ]
        },
    )
    res = ent_client.discovery.transport_vasculome(modality="monovalent", specificity_weight=0.5)
    assert len(res) == 2
    slc = next(r for r in res if r.gene == "SLC2A1")
    assert slc.specificity_index == 0.12
    assert slc.broadly_shared is True
    assert slc.combined_score == 0.41
    assert slc.top_peripheral_tissues == [["lung", 331.98], ["adipose_subcutaneous", 319.8]]
    tfrc = next(r for r in res if r.gene == "TFRC")
    assert tfrc.broadly_shared is False
    assert tfrc.specificity_index is None  # null-safe


def test_bbb_receptor_coerces_raw_row() -> None:
    r = BBBReceptor.model_validate(
        {"uniprot_gene": "LRP1", "multivalent_score": 0.6, "top_gtex_tissues": [["liver", 5.0]]}
    )
    assert r.gene == "LRP1"
    assert r.score == 0.6
    assert r.top_peripheral_tissues == [["liver", 5.0]]


def test_bbb_receptor_still_accepts_alias_shape() -> None:
    # The SDK-alias {gene, score} shape must keep working.
    r = BBBReceptor.model_validate({"gene": "TFRC", "score": 0.92})
    assert r.gene == "TFRC"
    assert r.score == 0.92


# -- compare_bbb_vs_brain recipe -------------------------------------------


def test_compare_bbb_vs_brain_recipe(httpx_mock: HTTPXMock, ent_client: LigandAI) -> None:
    # 1) transport_vasculome → BBB shuttle genes
    httpx_mock.add_response(
        url=f"{BASE}/api/transport-vasculome/query",
        method="POST",
        json={
            "receptors": [
                {"uniprot_gene": "TFRC", "monovalent_score": 0.95, "specificity_index": 0.9},
                {"uniprot_gene": "SLC2A1", "monovalent_score": 0.7, "specificity_index": 0.1},
            ]
        },
    )
    # 2) compare-groups with the geneset target vs gtex brain reference
    httpx_mock.add_response(
        url=f"{BASE}/api/transcriptomics/compare-groups",
        method="POST",
        json={
            "success": True,
            "mode": "compare",
            "method": "fold_change",
            "markers": [
                {"gene_name": "TFRC", "fold_change": 6.0, "target_tpm": 60, "ref_tpm": 5},
                {"gene_name": "SLC2A1", "fold_change": 1.2, "target_tpm": 30, "ref_tpm": 28},
            ],
        },
    )
    resp = ent_client.discovery.compare_bbb_vs_brain(
        bbb_modality="monovalent", brain_tissues=["brain_cortex"], bbb_limit=10
    )
    # The compare-groups request must carry a geneset target built from shuttles
    compare_req = httpx_mock.get_requests()[-1]
    body = _json.loads(compare_req.read())
    assert body["targetGroup"]["type"] == "geneset"
    assert set(body["targetGroup"]["genes"]) == {"TFRC", "SLC2A1"}
    assert body["referenceGroups"][0]["type"] == "gtex"
    assert body["referenceGroups"][0]["tissue"] == "brain_cortex"
    # shared/differential populated
    assert "TFRC" in (resp.differential_genes or [])
    assert "SLC2A1" in (resp.shared_genes or [])
