# Copyright © 2026 Ligandal, Inc. All rights reserved.
"""Unit and OpenAPI 3.1 contract tests for the Disease Atlas SDK surface."""

from __future__ import annotations

import json
import os
import pathlib
import statistics
import time
from typing import Any

import pytest
from pytest_httpx import HTTPXMock

import ligandai
from ligandai import (
    DEFAULT_ATLAS_DATA_VERSION,
    SUPERADMIN_API_KEY_PREFIXES,
    AdminDiseases,
    AsyncAdminDiseases,
    AsyncLigandAI,
    AtlasDiseaseDetail,
    AtlasDiseaseSummary,
    AtlasDiseaseTargetsResponse,
    AtlasDiseaseTissue,
    AtlasDiseaseTissuesResponse,
    AtlasMeta,
    AtlasRankedTarget,
    AtlasSearchResponse,
    DiseaseMap,
    LigandAI,
    LigandAIAmbiguousDisease,
    LigandAIAuthError,
    LigandAIForbidden,
    LigandAIInvalidConfig,
    LigandAINotFoundError,
    LigandAIUnreadableResponse,
    LigandAIValidationError,
    MatrixWeights,
    resolve_atlas_data_version,
)

BASE = "http://atlas.ligandai.test"
OPENAPI_PATH = pathlib.Path(
    "/mnt/ligandal_shared/disease_market_map_20261118/integration/lane_a/openapi.json"
)

CANONICAL_OPENAPI_OPERATIONS: dict[tuple[str, str], set[str]] = {
    ("GET", "/api/atlas/meta"): set(),
    ("GET", "/api/atlas/diseases/search"): {
        "q",
        "organ",
        "tissue",
        "rarity",
        "gene",
        "limit",
        "cursor",
    },
    ("GET", "/api/atlas/diseases/{id}"): {"id"},
    ("GET", "/api/atlas/diseases/{id}/tissues"): {"id", "role", "include_cells"},
    ("GET", "/api/atlas/diseases/{id}/targets"): {
        "id",
        "tissue",
        "cell_type",
        "surface_only",
        "top_k",
        "strict_targets",
    },
    ("GET", "/api/atlas/diseases/{id}/map"): {
        "id",
        "tissue",
        "cell_type",
        "surface_only",
        "top_k",
    },
    ("POST", "/api/atlas/map"): set(),
    ("GET", "/api/admin/atlas/matrix"): {
        "q",
        "organ",
        "modality",
        "rarity",
        "competition_level",
        "designation",
        "ladder_id",
        "exclude_imputed",
        "min_score",
        "sort_by",
        "sort_dir",
        "limit",
        "cursor",
        "weights",
    },
    ("POST", "/api/admin/atlas/matrix/rerank"): set(),
    ("GET", "/api/admin/atlas/diseases/{id}/intelligence"): {"id"},
    ("GET", "/api/admin/atlas/baskets"): set(),
    ("GET", "/api/admin/atlas/baskets/{organ}"): {"organ"},
    ("GET", "/api/admin/atlas/populations"): {
        "q",
        "organ",
        "exclude_imputed",
        "limit",
        "cursor",
    },
    ("GET", "/api/admin/atlas/populations/{id}"): {"id"},
    ("GET", "/api/admin/atlas/ladders"): set(),
    ("GET", "/api/admin/atlas/ladders/{ladderId}"): {"ladderId"},
    ("GET", "/api/admin/atlas/geo/{id}"): {"id"},
    ("GET", "/api/admin/atlas/benchmarks"): set(),
}

FORBIDDEN_COMMERCIAL_FIELDS = {
    "commercial",
    "competition",
    "designations",
    "valuation_inputs",
    "valuationInputs",
    "rnpv_low_usd_m",
    "rnpv_base_usd_m",
    "rnpv_high_usd_m",
    "rnpvLowUsdM",
    "rnpvBaseUsdM",
    "rnpvHighUsdM",
    "us_market_benchmark_usd",
    "usMarketBenchmarkUsd",
    "observed_us_comparator",
    "observedUsComparator",
    "populations",
    "population",
    "basket_contributions",
    "basketContributions",
    "sales_benchmarks",
    "salesBenchmarks",
}


def _sample_external_ids(mondo: str | None = "MONDO:0016545", orpha: str | None = "ORPHA:271861") -> dict[str, Any]:
    return {
        "mondo": mondo,
        "orpha": orpha,
        "omim": ["105210"],
        "hgnc": ["HGNC:12405"],
        "icd10": ["E85.1"],
        "icd11": ["5D56"],
        "medgen": ["C0268392"],
        "recordIds": ["D00142"],
    }


def _sample_summary(
    identity_id: str = "ORPHA:271861",
    name: str = "Hereditary ATTR amyloidosis",
    alias: str | None = "hATTR",
    organ: str = "liver",
    gene: str = "TTR",
) -> dict[str, Any]:
    return {
        "identityId": identity_id,
        "slug": name.lower().replace(" ", "-"),
        "name": name,
        "matchedAlias": alias,
        "matchType": "exact_alias",
        "primaryOrgan": organ,
        "therapeuticPrimaryOrgan": organ,
        "organs": [organ, "heart", "peripheral_nerve"],
        "genes": [gene],
        "rarityClass": "rare_genetic",
        "rarityLabel": "Rare genetic",
        "inheritance": "Autosomal dominant",
        "externalIds": _sample_external_ids(),
        "primaryTissue": {
            "tissueKey": organ,
            "label": organ.capitalize(),
            "gtexSite": organ.capitalize(),
            "uberonId": "UBERON:0002107",
        },
        "topTargetPreview": {
            "gene": "ASGR1",
            "localization": "surface_receptor",
            "confidenceLevel": "high",
        },
    }


def _sample_detail(
    identity_id: str = "ORPHA:271861",
    name: str = "Hereditary ATTR amyloidosis",
    organ: str = "liver",
    gene: str = "TTR",
) -> dict[str, Any]:
    base = _sample_summary(identity_id=identity_id, name=name, organ=organ, gene=gene)
    base.pop("matchedAlias")
    base.pop("matchType")
    base.update(
        {
            "aliases": ["hATTR", "ATTRv amyloidosis"],
            "onset": "Adult",
            "definition": "Systemic amyloidosis caused by pathogenic TTR variants.",
            "allelicConditions": ["Wild-type ATTR cardiomyopathy"],
            "manifestationCategories": ["hepatic", "cardiac", "neurologic"],
            "mutationSummary": {
                "pathogenicVariantCount": 138,
                "exampleProteinVariants": ["p.Val50Met", "p.Val142Ile"],
            },
            "tissuesSummary": {
                "organCount": 3,
                "tissueCount": 3,
                "cellTypeCount": 4,
                "primaryTissues": [organ],
            },
            "targetCount": 2,
            "publicSources": ["Orphanet", "MONDO", "GTEx v8", "HPA v23"],
        }
    )
    return base


def _sample_tissue(tissue_key: str = "liver", label: str = "Liver") -> dict[str, Any]:
    return {
        "tissueKey": tissue_key,
        "label": label,
        "organ": tissue_key,
        "role": "therapeutic_primary",
        "roleDescription": "Primary site of pathogenic protein synthesis and targeted delivery.",
        "gtexSite": label,
        "hpaTissue": label.lower(),
        "uberonId": "UBERON:0002107",
        "crosswalkIds": ["UBERON:0002107"],
        "cells": [
            {
                "cellKey": "hepatocytes",
                "label": "Hepatocytes",
                "clId": "CL:0000182",
                "hpaCellType": "Hepatocytes",
                "targetable": True,
                "roleInDisease": "Synthesizes >95% of circulating TTR.",
                "topExpressingTargets": [
                    {"gene": "ASGR1", "nTPM": 642.4, "specificity": 18.7}
                ],
            }
        ],
        "healthyReferenceDatasets": [
            {
                "accession": "GSE115469",
                "title": "Single-cell RNA-seq of healthy human liver",
                "sampleCount": 5,
                "url": "https://www.ncbi.nlm.nih.gov/geo/query/acc.cgi?acc=GSE115469",
            }
        ],
        "targetCount": 2,
        "evidence": [
            {
                "type": "gtex_expression",
                "summary": "High liver expression in GTEx v8.",
                "source": "GTEx v8",
            }
        ],
    }


def _sample_target(
    gene: str = "ASGR1",
    rank: int = 1,
    disease_id: str = "ORPHA:271861",
    disease_name: str = "Hereditary ATTR amyloidosis",
    tissue: str = "liver",
) -> dict[str, Any]:
    return {
        "rank": rank,
        "gene": gene,
        "hgncId": "HGNC:742",
        "ensemblId": "ENSG00000141505",
        "uniprotId": "P07306",
        "proteinName": "Asialoglycoprotein receptor 1",
        "targetRole": "delivery_receptor",
        "roleLabel": "Hepatocyte internalization receptor",
        "localization": "surface_receptor",
        "isSurfaceExposed": True,
        "isReceptor": True,
        "isSecreted": False,
        "overallScore": 0.94,
        "scoreBreakdown": {
            "geneticCausality": 0.85,
            "tissueExpressionSpecificity": 0.98,
            "surfaceAccessibility": 1.0,
            "druggability": 0.92,
            "ligandaiStructureCoverage": 0.95,
        },
        "expression": {
            "primaryTissue": tissue,
            "gtexMeanTpm": 512.3,
            "hpaTissueNtpm": 498.1,
            "topCellType": "Hepatocytes",
            "topCellClId": "CL:0000182",
            "hpaCellNtpm": 642.4,
            "tissueSpecificityRatio": 18.7,
        },
        "deliveryReceptors": [
            {
                "gene": "ASGR1",
                "label": "ASGPR subunit H1",
                "mechanism": "Clathrin-mediated endocytosis on hepatocytes",
                "cellTypes": ["Hepatocytes"],
            }
        ],
        "structureCoverage": {
            "hasExperimentalPdb": True,
            "representativePdbIds": ["1DV8"],
            "hasAlphaFold": True,
            "inSurfaceomeCollection": True,
        },
        "recommendedApproach": {
            "summary": "Design a hepatocyte-selective peptide conjugate targeting ASGR1.",
            "deliverySummary": "Liver-restricted endocytosis via ASGR1.",
        },
        "confidence": {
            "level": "high",
            "score": 0.94,
            "rationale": "Validated human hepatocyte delivery receptor with experimental PDB coverage.",
        },
        "evidence": [
            {
                "type": "hpa_single_cell",
                "summary": "Hepatocyte-enriched surface expression (642.4 nTPM).",
                "source": "Human Protein Atlas v23",
            }
        ],
        "startCampaign": {
            "gene": gene,
            "targetLabel": f"{gene} ({tissue})",
            "diseaseId": disease_id,
            "diseaseName": disease_name,
            "tissue": tissue,
            "cellType": "Hepatocytes",
            "uniprotId": "P07306",
            "preferredPdbId": "1DV8",
            "localization": "surface_receptor",
            "studioUrl": f"/studio?gene={gene}&disease={disease_id}&tissue={tissue}",
        },
    }


def _sample_map_response(
    query: str = "Fabry kidney disease",
    identity_id: str = "MONDO:0010526",
    disease_name: str = "Fabry disease",
    matched_tissue: str | None = "kidney",
    resolved_via: str = "disease_plus_tissue_phrase",
) -> dict[str, Any]:
    tissue = _sample_tissue(
        tissue_key=matched_tissue or "kidney",
        label=(matched_tissue or "kidney").capitalize(),
    )
    target = _sample_target(
        gene="LRP2",
        rank=1,
        disease_id=identity_id,
        disease_name=disease_name,
        tissue=matched_tissue or "kidney",
    )
    return {
        "dataVersion": "v2",
        "query": query,
        "resolvedVia": resolved_via,
        "matchedTissueFilter": matched_tissue,
        "disease": _sample_detail(
            identity_id=identity_id,
            name=disease_name,
            organ=matched_tissue or "kidney",
            gene="GLA",
        ),
        "tissues": [tissue],
        "cells": tissue["cells"],
        "targets": [target],
        "receptors": [target],
        "evidence": target["evidence"],
        "startCampaign": target["startCampaign"],
        "tookMs": 1.8,
    }


def _sample_admin_matrix_row() -> dict[str, Any]:
    return {
        "identityId": "ORPHA:271861",
        "disease": "Hereditary ATTR amyloidosis",
        "recordIds": ["D00142"],
        "gene": "TTR",
        "organ": "liver",
        "rarity": "rare_genetic",
        "modality": "peptide_siRNA_conjugate",
        "score": 88.4,
        "customScore": 90.1,
        "components": {
            "modality_ease": 92.0,
            "delivery_precedent": 96.0,
            "biology": 95.0,
            "us_market": 84.0,
            "competition_headroom": 72.0,
            "time_cost": 86.0,
            "expansion": 88.0,
            "regulatory": 91.0,
        },
        "competition": {
            "approvedIngredientCombinations": 4,
            "activePhase23Trials": 6,
            "competitionLevel": "moderate",
            "leadingLabelMatches": "vutrisiran; patisiran",
            "leadingTrialAgents": "NTLA-2001",
            "bestInClassGap": "Extrahepatic cardiac + CNS penetration",
        },
        "designations": {
            "orphan": "eligible",
            "fast_track": "eligible",
            "breakthrough": "conditional",
            "rmat": "not_applicable",
            "rare_pediatric_voucher": "ineligible",
            "priority_review": "eligible",
            "eu_prime": "eligible",
        },
        "commercial": {
            "usMarketBenchmarkUsd": 1850000000.0,
            "usMarketBasis": "observed_comparator",
            "comparatorScope": "US net sales",
            "observedUsComparator": "Amvuttra",
            "observedUsComparatorSalesM": 1420.0,
            "observedPeriod": "FY2025",
            "salesSource": "SEC 10-K",
            "yearsToHumanData": 2.1,
            "costToHumanDataUsdM": 14.5,
            "route": "subcutaneous",
        },
        "expansionLadder": {
            "ladderId": "LADDER:ATTR",
            "downstream": "Wild-type ATTR cardiomyopathy",
            "downstreamUsSalesM": 6200.0,
            "ladderMembership": "lead_rare",
        },
        "imputedFields": [],
        "confidence": "high",
        "raw": {"rank": 1},
    }


# ---------------------------------------------------------------------------
# 1. OpenAPI 3.1.0 Contract Parity (against Lane A's openapi.json)
# ---------------------------------------------------------------------------


def test_openapi_contract_parity() -> None:
    if OPENAPI_PATH.exists():
        spec = json.loads(OPENAPI_PATH.read_text(encoding="utf-8"))
        assert spec["openapi"] == "3.1.0"
        live_ops: dict[tuple[str, str], set[str]] = {}
        for path, methods in spec["paths"].items():
            for method, details in methods.items():
                if method.upper() not in {"GET", "POST", "PUT", "PATCH", "DELETE"}:
                    continue
                params = {p["name"] for p in details.get("parameters", [])}
                live_ops[(method.upper(), path)] = params
        assert live_ops == CANONICAL_OPENAPI_OPERATIONS

        schemas = spec["components"]["schemas"]
        # Verify public OpenAPI schemas declare zero commercial fields.
        for public_schema_name in (
            "MetaResponse",
            "DiseaseSummary",
            "SearchResponse",
            "DiseaseDetail",
            "DiseaseTissue",
            "RankedTarget",
            "MapResponse",
        ):
            props = set(schemas[public_schema_name]["properties"].keys())
            leaked = props & FORBIDDEN_COMMERCIAL_FIELDS
            assert not leaked, f"{public_schema_name} leaked {leaked}"

        # Verify SDK Pydantic models accept every required field declared in OpenAPI.
        meta = AtlasMeta.model_validate(
            {
                "dataVersion": "v2",
                "requestedVersion": "v2",
                "schemaVersion": "2026.11.18.1",
                "gatesStatus": "not_required_for_v2",
                "buildHash": "sha256:test",
                "builtAt": "2026-11-18T00:00:00Z",
                "counts": {
                    "diseases": 3214,
                    "records": 4120,
                    "genes": 2890,
                    "organs": 18,
                    "tissues": 54,
                    "cellTypes": 81,
                    "targets": 1940,
                },
                "organs": [
                    {
                        "slug": "liver",
                        "label": "Liver",
                        "diseaseCount": 212,
                        "topTissues": ["liver"],
                    }
                ],
                "featuredExamples": [
                    {
                        "query": "hATTR",
                        "identityId": "ORPHA:271861",
                        "name": "Hereditary ATTR amyloidosis",
                        "primaryTissue": "liver",
                        "topTarget": "ASGR1",
                    }
                ],
            }
        )
        assert set(schemas["MetaResponse"]["required"]).issubset(
            set(meta.model_dump(by_alias=True).keys())
        )
        mapped = DiseaseMap.model_validate(_sample_map_response())
        assert set(schemas["MapResponse"]["required"]).issubset(
            set(mapped.model_dump(by_alias=True).keys())
        )


def test_top_level_exports_present() -> None:
    expected = (
        "DEFAULT_ATLAS_DATA_VERSION",
        "SUPERADMIN_API_KEY_PREFIXES",
        "LigandAIAmbiguousDisease",
        "AdminDiseases",
        "AsyncAdminDiseases",
        "resolve_atlas_data_version",
        "AtlasMeta",
        "AtlasDiseaseSummary",
        "AtlasSearchResponse",
        "AtlasDiseaseDetail",
        "AtlasDiseaseTissue",
        "AtlasDiseaseTissuesResponse",
        "AtlasRankedTarget",
        "AtlasDiseaseTargetsResponse",
        "DiseaseMap",
        "MatrixWeights",
    )
    for symbol in expected:
        assert hasattr(ligandai, symbol), symbol
        assert symbol in ligandai.__all__, symbol


# ---------------------------------------------------------------------------
# 2. Public Biological Resolution (Sync + Async)
# ---------------------------------------------------------------------------


def test_public_disease_atlas_methods_sync(httpx_mock: HTTPXMock) -> None:
    client = LigandAI(api_key="lgai_pro_publictest", base_url=BASE, max_retries=1)

    httpx_mock.add_response(
        url=f"{BASE}/api/atlas/meta",
        json={
            "dataVersion": "v2",
            "requestedVersion": "v2",
            "schemaVersion": "2026.11.18.1",
            "gatesStatus": "not_required_for_v2",
            "buildHash": "sha256:v2build",
            "builtAt": "2026-11-18T12:00:00Z",
            "counts": {
                "diseases": 3214,
                "records": 4120,
                "genes": 2890,
                "organs": 18,
                "tissues": 54,
                "cellTypes": 81,
                "targets": 1940,
            },
            "organs": [],
            "featuredExamples": [],
        },
    )
    meta = client.diseases.meta()
    assert meta.data_version == "v2"
    assert meta.counts.diseases == 3214

    # Clinical abbreviations: hATTR, CF, HoFH
    for abbr, identity_id, disease_name, organ, gene in (
        ("hATTR", "ORPHA:271861", "Hereditary ATTR amyloidosis", "liver", "TTR"),
        ("CF", "MONDO:0009061", "Cystic fibrosis", "lung", "CFTR"),
        ("HoFH", "MONDO:0018328", "Homozygous familial hypercholesterolemia", "liver", "LDLR"),
    ):
        httpx_mock.add_response(
            url=f"{BASE}/api/atlas/diseases/search?q={abbr}&limit=25",
            json={
                "dataVersion": "v2",
                "query": abbr,
                "totalEstimate": 1,
                "nextCursor": None,
                "tookMs": 0.9,
                "items": [
                    _sample_summary(
                        identity_id=identity_id,
                        name=disease_name,
                        alias=abbr,
                        organ=organ,
                        gene=gene,
                    )
                ],
            },
        )
        hits = client.diseases.search(abbr)
        assert len(hits) == 1
        assert hits[0].identity_id == identity_id
        assert hits[0].matched_alias == abbr
        assert hits[0].gene == gene

    # Paginated search envelope
    httpx_mock.add_response(
        url=f"{BASE}/api/atlas/diseases/search?q=amyloid&organ=liver&limit=10",
        json={
            "dataVersion": "v2",
            "query": "amyloid",
            "totalEstimate": 4,
            "nextCursor": "cur_2",
            "tookMs": 1.1,
            "items": [_sample_summary()],
        },
    )
    page = client.diseases.search_page("amyloid", organ="liver", limit=10)
    assert isinstance(page, AtlasSearchResponse)
    assert page.total_estimate == 4
    assert page.next_cursor == "cur_2"

    # Detail
    httpx_mock.add_response(
        url=f"{BASE}/api/atlas/diseases/ORPHA%3A271861",
        json={"dataVersion": "v2", "tookMs": 0.6, "disease": _sample_detail()},
    )
    detail = client.diseases.get("ORPHA:271861")
    assert isinstance(detail, AtlasDiseaseDetail)
    assert detail.identity_id == "ORPHA:271861"
    assert "TTR" in detail.genes

    # Tissues + envelope
    httpx_mock.add_response(
        url=f"{BASE}/api/atlas/diseases/ORPHA%3A271861/tissues?role=therapeutic&include_cells=true",
        json={
            "dataVersion": "v2",
            "identityId": "ORPHA:271861",
            "diseaseName": "Hereditary ATTR amyloidosis",
            "matchedTissueHint": "liver",
            "tookMs": 0.8,
            "tissues": [_sample_tissue()],
        },
    )
    tissues = client.diseases.tissues("ORPHA:271861", role="therapeutic", include_cells=True)
    assert len(tissues) == 1
    assert isinstance(tissues[0], AtlasDiseaseTissue)
    assert tissues[0].cells[0].cl_id == "CL:0000182"

    httpx_mock.add_response(
        url=f"{BASE}/api/atlas/diseases/ORPHA%3A271861/tissues",
        json={
            "dataVersion": "v2",
            "identityId": "ORPHA:271861",
            "diseaseName": "Hereditary ATTR amyloidosis",
            "matchedTissueHint": None,
            "tookMs": 0.5,
            "tissues": [_sample_tissue()],
        },
    )
    tissues_env = client.diseases.tissues_response("ORPHA:271861")
    assert isinstance(tissues_env, AtlasDiseaseTissuesResponse)
    assert tissues_env.identity_id == "ORPHA:271861"

    # Targets + honest no_targets envelope
    httpx_mock.add_response(
        url=f"{BASE}/api/atlas/diseases/ORPHA%3A271861/targets?tissue=liver&surface_only=true&top_k=5",
        json={
            "dataVersion": "v2",
            "identityId": "ORPHA:271861",
            "diseaseName": "Hereditary ATTR amyloidosis",
            "filters": {"tissue": "liver", "cellType": None, "surfaceOnly": True, "topK": 5},
            "status": "ok",
            "emptyReason": None,
            "tookMs": 1.2,
            "targets": [_sample_target()],
        },
    )
    targets = client.diseases.targets("ORPHA:271861", tissue="liver", surface_only=True, top_k=5)
    assert len(targets) == 1
    assert isinstance(targets[0], AtlasRankedTarget)
    assert targets[0].gene == "ASGR1"
    assert targets[0].start_campaign.preferred_pdb_id == "1DV8"

    httpx_mock.add_response(
        url=f"{BASE}/api/atlas/diseases/ORPHA%3A271861/targets?tissue=retina&top_k=25",
        json={
            "dataVersion": "v2",
            "identityId": "ORPHA:271861",
            "diseaseName": "Hereditary ATTR amyloidosis",
            "filters": {"tissue": "retina", "cellType": None, "surfaceOnly": False, "topK": 25},
            "status": "no_targets",
            "emptyReason": "No surface receptors met the expression threshold in retina.",
            "tookMs": 0.7,
            "targets": [],
        },
    )
    empty_env = client.diseases.targets_response("ORPHA:271861", tissue="retina")
    assert isinstance(empty_env, AtlasDiseaseTargetsResponse)
    assert empty_env.status == "no_targets"
    assert empty_env.targets == []
    assert empty_env.empty_reason is not None

    # One-call map() via POST (compound phrase "Fabry kidney disease") and GET
    httpx_mock.add_response(
        url=f"{BASE}/api/atlas/map",
        method="POST",
        json=_sample_map_response("Fabry kidney disease"),
    )
    mapped = client.map("Fabry kidney disease", surface_only=True, top_k=10)
    assert isinstance(mapped, DiseaseMap)
    assert mapped.resolved_via == "disease_plus_tissue_phrase"
    assert mapped.matched_tissue_filter == "kidney"
    assert len(mapped.tissues) == 1
    assert len(mapped.cells) == 1
    assert len(mapped.receptors) == 1
    assert len(mapped.evidence) == 1
    assert mapped.start_campaign is not None
    assert mapped.start_campaign.gene == "LRP2"

    httpx_mock.add_response(
        url=f"{BASE}/api/atlas/diseases/ORPHA%3A271861/map?tissue=liver&top_k=5",
        method="GET",
        json=_sample_map_response(
            query="ORPHA:271861",
            identity_id="ORPHA:271861",
            disease_name="Hereditary ATTR amyloidosis",
            matched_tissue="liver",
            resolved_via="exact_id",
        ),
    )
    mapped_get = client.map_disease("ORPHA:271861", tissue="liver", top_k=5, use_get=True)
    assert mapped_get.disease.identity_id == "ORPHA:271861"


@pytest.mark.asyncio
async def test_public_and_admin_methods_async(httpx_mock: HTTPXMock) -> None:
    httpx_mock.add_response(
        url=f"{BASE}/api/atlas/map",
        method="POST",
        json=_sample_map_response("hATTR", "ORPHA:271861", "Hereditary ATTR amyloidosis", "liver", "alias"),
    )
    httpx_mock.add_response(
        url=f"{BASE}/api/admin/atlas/baskets",
        json={
            "dataVersion": "v2",
            "baskets": [
                {
                    "organ": "liver",
                    "leadDisease": "Hereditary ATTR amyloidosis",
                    "uniqueIndications": 14,
                    "sharedRouteAdditions": 9,
                    "allocatedMarketProxyUsdM": 9200.0,
                    "allocatedPeakSalesUsdM": 3100.0,
                    "discountedSharedCostSavingsUsdM": 185.0,
                    "rnpvLowUsdM": 420.0,
                    "rnpvBaseUsdM": 890.0,
                    "rnpvHighUsdM": 1540.0,
                    "raw": {},
                }
            ],
            "totals": {
                "rnpvLowUsdM": 420.0,
                "rnpvBaseUsdM": 890.0,
                "rnpvHighUsdM": 1540.0,
                "discountedSharedCostSavingsUsdM": 185.0,
            },
        },
    )

    async with AsyncLigandAI(api_key="lgai_superadmin_asynctest", base_url=BASE, max_retries=1) as client:
        assert client.tier == "superadmin"
        mapped = await client.map("hATTR")
        assert mapped.disease.identity_id == "ORPHA:271861"
        baskets = await client.diseases.baskets()
        assert len(baskets.baskets) == 1
        assert baskets.baskets[0].rnpv_base_usd_m == 890.0


# ---------------------------------------------------------------------------
# 3. Superadmin Role Gating & Full Intelligence Surface
# ---------------------------------------------------------------------------


def test_superadmin_methods_reject_missing_and_non_superadmin_keys_before_http(
    httpx_mock: HTTPXMock,
) -> None:
    anon = LigandAI(base_url=BASE, max_retries=1)
    with pytest.raises(LigandAIAuthError) as auth_exc:
        anon.diseases.matrix()
    assert auth_exc.value.status_code == 401
    assert auth_exc.value.code == "superadmin_api_key_required"
    assert httpx_mock.get_requests() == []

    for prefix in ("lgai_free_", "lgai_basic_", "lgai_edu_", "lgai_pro_", "lgai_ent_"):
        customer = LigandAI(api_key=f"{prefix}customer123", base_url=BASE, max_retries=1)
        for call in (
            lambda c=customer: c.diseases.matrix(),
            lambda c=customer: c.diseases.baskets(),
            lambda c=customer: c.diseases.populations(),
            lambda c=customer: c.diseases.intelligence("ORPHA:271861"),
            lambda c=customer: c.diseases.ladders(),
            lambda c=customer: c.diseases.geo("ORPHA:271861"),
            lambda c=customer: c.diseases.benchmarks(),
        ):
            with pytest.raises(LigandAIForbidden) as forb_exc:
                call()
            assert forb_exc.value.status_code == 403
            assert forb_exc.value.code == "forbidden_role"
            assert forb_exc.value.reason == "forbidden_role"
        assert httpx_mock.get_requests() == []


def test_superadmin_methods_succeed_for_both_superadmin_key_prefixes(
    httpx_mock: HTTPXMock,
) -> None:
    for prefix in SUPERADMIN_API_KEY_PREFIXES:
        sa = LigandAI(api_key=f"{prefix}key999", base_url=BASE, max_retries=1)
        assert sa.tier == "superadmin"
        assert isinstance(sa.diseases.admin, AdminDiseases)

        httpx_mock.add_response(
            url=f"{BASE}/api/admin/atlas/matrix?organ=liver&sort_by=score&sort_dir=desc&limit=20",
            json={
                "dataVersion": "v2",
                "totalCount": 1,
                "nextCursor": None,
                "activeWeights": None,
                "tookMs": 1.3,
                "items": [_sample_admin_matrix_row()],
            },
        )
        mat = sa.diseases.matrix(organ="liver", limit=20)
        assert mat.total_count == 1
        assert mat.items[0].commercial.observed_us_comparator == "Amvuttra"

        httpx_mock.add_response(
            url=f"{BASE}/api/admin/atlas/matrix/rerank",
            method="POST",
            json={
                "dataVersion": "v2",
                "totalCount": 1,
                "nextCursor": None,
                "activeWeights": {
                    "modality_ease": 0.25,
                    "delivery_precedent": 0.20,
                    "biology": 0.20,
                    "us_market": 0.10,
                    "competition_headroom": 0.10,
                    "time_cost": 0.05,
                    "expansion": 0.05,
                    "regulatory": 0.05,
                },
                "tookMs": 1.7,
                "rows": [_sample_admin_matrix_row()],
            },
        )
        reranked = sa.diseases.admin.matrix(
            organ="liver",
            weights=MatrixWeights(modality_ease=0.25, delivery_precedent=0.20, biology=0.20),
        )
        assert len(reranked.items) == 1
        assert reranked.active_weights is not None

        httpx_mock.add_response(
            url=f"{BASE}/api/admin/atlas/baskets/liver",
            json={
                "dataVersion": "v2",
                "basket": {
                    "organ": "liver",
                    "leadDisease": "Hereditary ATTR amyloidosis",
                    "uniqueIndications": 14,
                    "sharedRouteAdditions": 9,
                    "allocatedMarketProxyUsdM": 9200.0,
                    "allocatedPeakSalesUsdM": 3100.0,
                    "discountedSharedCostSavingsUsdM": 185.0,
                    "rnpvLowUsdM": 420.0,
                    "rnpvBaseUsdM": 890.0,
                    "rnpvHighUsdM": 1540.0,
                    "raw": {},
                },
                "contributions": [{"identityId": "ORPHA:271861", "rnpvBaseUsdM": 310.0}],
                "cashFlows": [{"year": 2028, "netCashFlowUsdM": -12.5}],
            },
        )
        basket_detail = sa.diseases.baskets("liver")
        assert basket_detail.basket.organ == "liver"
        assert len(basket_detail.contributions) == 1

        httpx_mock.add_response(
            url=f"{BASE}/api/admin/atlas/populations?organ=liver&exclude_imputed=true&limit=25",
            json={
                "dataVersion": "v2",
                "totalCount": 1,
                "nextCursor": None,
                "populations": [{"identityId": "ORPHA:271861", "usPrevalence": 6800}],
            },
        )
        pops = sa.diseases.populations(organ="liver", exclude_imputed=True, limit=25)
        assert pops.total_count == 1
        assert len(pops.items) == 1

        httpx_mock.add_response(
            url=f"{BASE}/api/admin/atlas/populations/ORPHA%3A271861",
            json={
                "dataVersion": "v2",
                "identityId": "ORPHA:271861",
                "population": {"usPrevalence": 6800, "eu5Prevalence": 8100},
            },
        )
        pop_one = sa.diseases.populations("ORPHA:271861")
        assert pop_one.identity_id == "ORPHA:271861"
        assert pop_one.population["usPrevalence"] == 6800

        httpx_mock.add_response(
            url=f"{BASE}/api/admin/atlas/diseases/ORPHA%3A271861/intelligence",
            json={
                "dataVersion": "v2",
                "identityId": "ORPHA:271861",
                "disease": _sample_detail(),
                "matrix": _sample_admin_matrix_row(),
                "matrixByRecord": [],
                "valuationInputs": {"basePriceUsd": 460000},
                "populations": {"usPrevalence": 6800},
                "basketContributions": [],
                "basketCashFlows": [],
                "designations": {"flags": {"orphan": "eligible"}, "criteria": []},
                "ladder": None,
                "diseaseGeoDatasets": [],
                "geoCoverage": [],
            },
        )
        intel = sa.diseases.intelligence("ORPHA:271861")
        assert intel.identity_id == "ORPHA:271861"
        assert intel.valuation_inputs == {"basePriceUsd": 460000}

        httpx_mock.add_response(
            url=f"{BASE}/api/admin/atlas/ladders",
            json={
                "dataVersion": "v2",
                "ladders": [
                    {
                        "ladderId": "LADDER:ATTR",
                        "organ": "liver",
                        "summary": {"lead": "hATTR"},
                        "members": [{"identityId": "ORPHA:271861"}],
                    }
                ],
            },
        )
        ladders = sa.diseases.ladders()
        assert len(ladders.ladders) == 1

        httpx_mock.add_response(
            url=f"{BASE}/api/admin/atlas/geo/ORPHA%3A271861",
            json={
                "dataVersion": "v2",
                "identityId": "ORPHA:271861",
                "diseaseTissueDatasets": [{"accession": "GSE99999"}],
                "geoCoverage": [],
                "healthyReferenceDatasets": [],
            },
        )
        geo = sa.diseases.geo("ORPHA:271861")
        assert len(geo.disease_tissue_datasets) == 1

        httpx_mock.add_response(
            url=f"{BASE}/api/admin/atlas/benchmarks",
            json={
                "dataVersion": "v2",
                "salesBenchmarks": [{"drug": "Amvuttra", "usSalesM": 1420.0}],
                "designationCriteria": [{"designation": "orphan"}],
            },
        )
        bench = sa.diseases.benchmarks()
        assert len(bench.sales_benchmarks) == 1


# ---------------------------------------------------------------------------
# 4. Strict Public Non-Leakage Guarantee
# ---------------------------------------------------------------------------


def test_public_models_strip_any_injected_commercial_fields() -> None:
    for model_cls in (
        AtlasMeta,
        AtlasDiseaseSummary,
        AtlasSearchResponse,
        AtlasDiseaseDetail,
        AtlasDiseaseTissue,
        AtlasRankedTarget,
        DiseaseMap,
    ):
        leaked = set(model_cls.model_fields.keys()) & FORBIDDEN_COMMERCIAL_FIELDS
        assert not leaked, f"{model_cls.__name__} declared forbidden fields: {leaked}"

    contaminated = _sample_map_response()
    contaminated["commercial"] = {"usMarketBenchmarkUsd": 1850000000.0}
    contaminated["rnpvBaseUsdM"] = 890.0
    contaminated["populations"] = {"usPrevalence": 6800}
    contaminated["disease"]["designations"] = {"orphan": "eligible"}
    contaminated["targets"][0]["competition"] = {"competitionLevel": "high"}

    parsed = DiseaseMap.model_validate(contaminated)
    dumped = json.dumps(parsed.model_dump(by_alias=True))
    for secret_token in (
        "usMarketBenchmarkUsd",
        "rnpvBaseUsdM",
        "usPrevalence",
        "designations",
        "competitionLevel",
    ):
        assert secret_token not in dumped


# ---------------------------------------------------------------------------
# 5. Error Mapping & v2 -> v3 Data-Version Switch
# ---------------------------------------------------------------------------


def test_atlas_error_envelopes_map_to_typed_exceptions(httpx_mock: HTTPXMock) -> None:
    client = LigandAI(api_key="lgai_pro_errtest", base_url=BASE, max_retries=1)

    httpx_mock.add_response(
        url=f"{BASE}/api/atlas/diseases/NO_SUCH_DISEASE",
        status_code=404,
        json={
            "dataVersion": "v2",
            "error": {
                "code": "unknown_disease",
                "message": "No disease matched 'NO_SUCH_DISEASE'.",
                "query": "NO_SUCH_DISEASE",
            },
        },
    )
    with pytest.raises(LigandAINotFoundError) as nf:
        client.diseases.get("NO_SUCH_DISEASE")
    assert nf.value.code == "unknown_disease"

    httpx_mock.add_response(
        url=f"{BASE}/api/atlas/map",
        method="POST",
        status_code=409,
        json={
            "dataVersion": "v2",
            "error": {
                "code": "ambiguous_disease",
                "message": "Query 'amyloidosis' matched multiple distinct diseases.",
                "query": "amyloidosis",
                "candidates": [
                    {
                        "identityId": "ORPHA:271861",
                        "name": "Hereditary ATTR amyloidosis",
                        "primaryOrgan": "liver",
                        "genes": ["TTR"],
                        "externalIds": {"mondo": "MONDO:0016545", "orpha": "ORPHA:271861", "omim": ["105210"]},
                    },
                    {
                        "identityId": "ORPHA:85443",
                        "name": "AL amyloidosis",
                        "primaryOrgan": "kidney",
                        "genes": ["IGLC2"],
                        "externalIds": {"mondo": "MONDO:0009053", "orpha": "ORPHA:85443", "omim": []},
                    },
                ],
            },
        },
    )
    with pytest.raises(LigandAIAmbiguousDisease) as amb:
        client.map("amyloidosis")
    assert amb.value.status_code == 409
    assert amb.value.code == "ambiguous_disease"
    assert amb.value.query == "amyloidosis"
    assert len(amb.value.candidates) == 2

    httpx_mock.add_response(
        url=f"{BASE}/api/atlas/diseases/ORPHA%3A271861/targets?tissue=retina&top_k=25&strict_targets=true",
        status_code=422,
        json={
            "dataVersion": "v2",
            "error": {
                "code": "no_targets",
                "message": "No targets matched in retina.",
                "emptyReason": "Zero targets above threshold.",
            },
        },
    )
    with pytest.raises(LigandAIValidationError) as val_err:
        client.diseases.targets("ORPHA:271861", tissue="retina", strict_targets=True)
    assert val_err.value.code == "no_targets"

    httpx_mock.add_response(
        url=f"{BASE}/api/atlas/meta",
        status_code=204,
    )
    with pytest.raises(LigandAIUnreadableResponse):
        client.diseases.meta()


def test_data_version_switch_and_v3_gates_fallback(
    tmp_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch, httpx_mock: HTTPXMock
) -> None:
    monkeypatch.delenv("ATLAS_DATA_VERSION", raising=False)
    monkeypatch.delenv("LIGANDAI_ATLAS_DATA_VERSION", raising=False)
    monkeypatch.delenv("ATLAS_GATES_PATH", raising=False)

    assert DEFAULT_ATLAS_DATA_VERSION == "v3"
    assert resolve_atlas_data_version() == "v3"
    assert resolve_atlas_data_version("v2") == "v2"

    failing_gates = tmp_path / "gates_fail.json"
    failing_gates.write_text(
        json.dumps({"gates": {"schema": "pass", "orphan_ prevalence": "fail"}}),
        encoding="utf-8",
    )
    passing_gates = tmp_path / "gates_pass.json"
    passing_gates.write_text(
        json.dumps({"passed": True, "gates": {"schema": "pass", "crosswalk": "pass"}}),
        encoding="utf-8",
    )

    # Missing or failing gates fall back safely to v2; passing gates activate v3.
    assert resolve_atlas_data_version("v3", gates_path=tmp_path / "missing.json") == "v2"
    assert resolve_atlas_data_version("v3", gates_path=failing_gates) == "v2"
    assert resolve_atlas_data_version("v3", gates_path=passing_gates) == "v3"

    # Addendum 7 invariants: low numeric US prevalence or unpriced baskets stay on v2.
    pre_add7_dir = tmp_path / "pre_add7"
    pre_add7_dir.mkdir()
    pre_add7_gates = pre_add7_dir / "gates.json"
    pre_add7_gates.write_text(
        json.dumps(
            {
                "overall": "pass",
                "gates": {
                    "US point prevalence concentration": {
                        "status": "pass",
                        "observed": {"numeric_identities": 144, "total_identities": 5473},
                    },
                    "Basket replay and drivers": {
                        "status": "pass",
                        "observed": {"unpriced": ["Liver"]},
                    },
                },
            }
        ),
        encoding="utf-8",
    )
    assert resolve_atlas_data_version("v3", gates_path=pre_add7_gates) == "v2"

    # Freshness invariant: if an add7-*.json artifact is newer than gates.json, stay on v2.
    post_add7_dir = tmp_path / "post_add7"
    post_add7_dir.mkdir()
    post_add7_gates = post_add7_dir / "gates.json"
    post_add7_gates.write_text(
        json.dumps(
            {
                "overall": "pass",
                "gates": {
                    "US point prevalence concentration": {
                        "status": "pass",
                        "observed": {"numeric_identities": 5471, "total_identities": 5473},
                    },
                    "Basket replay and drivers": {
                        "status": "pass",
                        "observed": {"unpriced": []},
                    },
                },
            }
        ),
        encoding="utf-8",
    )
    add7_marker = post_add7_dir / "add7-4-pricing.json"
    add7_marker.write_text(json.dumps({"ok": True}), encoding="utf-8")
    os.utime(post_add7_gates, (1_700_000_000, 1_700_000_000))
    os.utime(add7_marker, (1_700_000_100, 1_700_000_100))
    assert resolve_atlas_data_version("v3", gates_path=post_add7_gates) == "v2"
    os.utime(post_add7_gates, (1_700_000_200, 1_700_000_200))
    assert resolve_atlas_data_version("v3", gates_path=post_add7_gates) == "v3"

    with pytest.raises(LigandAIInvalidConfig):
        resolve_atlas_data_version("v99")

    # Header forwarding when ATLAS_DATA_VERSION=v3 is set
    monkeypatch.setenv("ATLAS_DATA_VERSION", "v3")
    client = LigandAI(api_key="lgai_pro_vertest", base_url=BASE, max_retries=1)
    httpx_mock.add_response(
        url=f"{BASE}/api/atlas/meta",
        json={
            "dataVersion": "v3",
            "requestedVersion": "v3",
            "schemaVersion": "2026.11.18.1",
            "gatesStatus": "passed",
            "buildHash": "sha256:v3build",
            "builtAt": "2026-11-18T15:00:00Z",
            "counts": {
                "diseases": 3214,
                "records": 4120,
                "genes": 2890,
                "organs": 18,
                "tissues": 54,
                "cellTypes": 81,
                "targets": 1940,
            },
            "organs": [],
            "featuredExamples": [],
        },
    )
    meta = client.diseases.meta()
    assert meta.data_version == "v3"
    req = httpx_mock.get_request()
    assert req is not None
    assert req.headers.get("X-Atlas-Data-Version") == "v3"


# ---------------------------------------------------------------------------
# 6. Client-Side Deserialization / Serialization Latency Benchmark
# ---------------------------------------------------------------------------


def test_disease_map_client_latency_budget() -> None:
    payload = _sample_map_response()
    timings_ms: list[float] = []
    for _ in range(1000):
        t0 = time.perf_counter()
        model = DiseaseMap.model_validate(payload)
        _ = model.model_dump(by_alias=True)
        timings_ms.append((time.perf_counter() - t0) * 1000.0)
    timings_ms.sort()
    p50 = statistics.median(timings_ms)
    p95 = timings_ms[int(0.95 * len(timings_ms))]
    assert p50 < 5.0, f"DiseaseMap p50 validation+dump too slow: {p50:.3f} ms"
    assert p95 < 15.0, f"DiseaseMap p95 validation+dump too slow: {p95:.3f} ms"
