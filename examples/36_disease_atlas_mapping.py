#!/usr/bin/env python3
# Copyright © 2026 Ligandal, Inc. All rights reserved.
"""Runnable Disease Atlas walkthrough: search -> tissues -> ranked receptors -> map.

Runs against the live LigandAI API when ``LIGANDAI_API_KEY`` is set, or against a
deterministic in-memory transport when run offline so CI and local checkouts can
execute it directly without network credentials.
"""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path

import httpx

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from ligandai import LigandAI, LigandAIForbidden


def _build_offline_transport() -> httpx.MockTransport:
    def handler(request: httpx.Request) -> httpx.Response:
        path = request.url.path
        if path == "/api/atlas/diseases/search":
            return httpx.Response(
                200,
                json={
                    "dataVersion": "v2",
                    "query": request.url.params.get("q", ""),
                    "totalEstimate": 1,
                    "nextCursor": None,
                    "tookMs": 0.9,
                    "items": [
                        {
                            "identityId": "ORPHA:271861",
                            "slug": "hereditary-attr-amyloidosis",
                            "name": "Hereditary ATTR amyloidosis",
                            "matchedAlias": "hATTR",
                            "matchType": "exact_alias",
                            "primaryOrgan": "liver",
                            "therapeuticPrimaryOrgan": "liver",
                            "organs": ["liver", "heart", "peripheral_nerve"],
                            "genes": ["TTR"],
                            "rarityClass": "rare_genetic",
                            "rarityLabel": "Rare genetic",
                            "inheritance": "Autosomal dominant",
                            "externalIds": {
                                "mondo": "MONDO:0016545",
                                "orpha": "ORPHA:271861",
                                "omim": ["105210"],
                                "hgnc": ["HGNC:12405"],
                                "icd10": ["E85.1"],
                                "icd11": ["5D56"],
                                "medgen": ["C0268392"],
                                "recordIds": ["D00142"],
                            },
                            "primaryTissue": {
                                "tissueKey": "liver",
                                "label": "Liver",
                                "gtexSite": "Liver",
                                "uberonId": "UBERON:0002107",
                            },
                            "topTargetPreview": {
                                "gene": "ASGR1",
                                "localization": "surface_receptor",
                                "confidenceLevel": "high",
                            },
                        }
                    ],
                },
            )
        if path == "/api/atlas/map":
            body = json.loads(request.content.decode("utf-8"))
            return httpx.Response(
                200,
                json={
                    "dataVersion": "v2",
                    "query": body["disease"],
                    "resolvedVia": "disease_plus_tissue_phrase",
                    "matchedTissueFilter": "kidney",
                    "disease": {
                        "identityId": "MONDO:0010526",
                        "slug": "fabry-disease",
                        "name": "Fabry disease",
                        "aliases": ["Anderson-Fabry disease"],
                        "primaryOrgan": "kidney",
                        "therapeuticPrimaryOrgan": "kidney",
                        "organs": ["kidney", "heart"],
                        "genes": ["GLA"],
                        "rarityClass": "rare_genetic",
                        "rarityLabel": "Rare genetic",
                        "inheritance": "X-linked",
                        "onset": "Childhood",
                        "definition": "Lysosomal glycosphingolipid storage disorder.",
                        "allelicConditions": [],
                        "manifestationCategories": ["renal", "cardiac"],
                        "mutationSummary": {
                            "pathogenicVariantCount": 910,
                            "exampleProteinVariants": ["p.Asn215Ser"],
                        },
                        "externalIds": {
                            "mondo": "MONDO:0010526",
                            "orpha": "ORPHA:324",
                            "omim": ["301500"],
                            "hgnc": ["HGNC:4296"],
                            "icd10": ["E75.21"],
                            "icd11": ["5C56.01"],
                            "medgen": ["C0002986"],
                            "recordIds": ["D00088"],
                        },
                        "tissuesSummary": {
                            "organCount": 2,
                            "tissueCount": 2,
                            "cellTypeCount": 3,
                            "primaryTissues": ["kidney"],
                        },
                        "targetCount": 1,
                        "publicSources": ["MONDO", "Orphanet", "GTEx v8", "HPA v23"],
                        "primaryTissue": {
                            "tissueKey": "kidney",
                            "label": "Kidney",
                            "gtexSite": "Kidney - Cortex",
                            "uberonId": "UBERON:0002113",
                        },
                        "topTargetPreview": {
                            "gene": "LRP2",
                            "localization": "surface_receptor",
                            "confidenceLevel": "high",
                        },
                    },
                    "tissues": [
                        {
                            "tissueKey": "kidney",
                            "label": "Kidney",
                            "organ": "kidney",
                            "role": "therapeutic_primary",
                            "roleDescription": "Primary renal clearance and podocyte/tubular involvement.",
                            "gtexSite": "Kidney - Cortex",
                            "hpaTissue": "kidney",
                            "uberonId": "UBERON:0002113",
                            "crosswalkIds": ["UBERON:0002113"],
                            "cells": [
                                {
                                    "cellKey": "proximal_tubular_cells",
                                    "label": "Proximal tubular cells",
                                    "clId": "CL:0002306",
                                    "hpaCellType": "Proximal tubular cells",
                                    "targetable": True,
                                    "roleInDisease": "Renal uptake and glycosphingolipid clearance.",
                                    "topExpressingTargets": [
                                        {"gene": "LRP2", "nTPM": 518.2, "specificity": 14.1}
                                    ],
                                }
                            ],
                            "healthyReferenceDatasets": [],
                            "targetCount": 1,
                            "evidence": [
                                {
                                    "type": "hpa_single_cell",
                                    "summary": "Enriched on proximal tubular epithelium.",
                                    "source": "HPA v23",
                                }
                            ],
                        }
                    ],
                    "cells": [
                        {
                            "cellKey": "proximal_tubular_cells",
                            "label": "Proximal tubular cells",
                            "clId": "CL:0002306",
                            "hpaCellType": "Proximal tubular cells",
                            "targetable": True,
                            "roleInDisease": "Renal uptake and glycosphingolipid clearance.",
                            "topExpressingTargets": [
                                {"gene": "LRP2", "nTPM": 518.2, "specificity": 14.1}
                            ],
                        }
                    ],
                    "targets": [
                        {
                            "rank": 1,
                            "gene": "LRP2",
                            "hgncId": "HGNC:6694",
                            "ensemblId": "ENSG00000081479",
                            "uniprotId": "P98164",
                            "proteinName": "Low-density lipoprotein receptor-related protein 2 (Megalin)",
                            "targetRole": "delivery_receptor",
                            "roleLabel": "Renal proximal tubule receptor",
                            "localization": "surface_receptor",
                            "isSurfaceExposed": True,
                            "isReceptor": True,
                            "isSecreted": False,
                            "overallScore": 0.91,
                            "scoreBreakdown": {
                                "geneticCausality": 0.78,
                                "tissueExpressionSpecificity": 0.95,
                                "surfaceAccessibility": 1.0,
                                "druggability": 0.89,
                                "ligandaiStructureCoverage": 0.93,
                            },
                            "expression": {
                                "primaryTissue": "kidney",
                                "gtexMeanTpm": 184.6,
                                "hpaTissueNtpm": 212.0,
                                "topCellType": "Proximal tubular cells",
                                "topCellClId": "CL:0002306",
                                "hpaCellNtpm": 518.2,
                                "tissueSpecificityRatio": 14.1,
                            },
                            "deliveryReceptors": [],
                            "structureCoverage": {
                                "hasExperimentalPdb": True,
                                "representativePdbIds": ["8J3Y"],
                                "hasAlphaFold": True,
                                "inSurfaceomeCollection": True,
                            },
                            "recommendedApproach": {
                                "summary": "Target LRP2 for renal proximal-tubule delivery.",
                                "deliverySummary": "Endocytic uptake on renal epithelium.",
                            },
                            "confidence": {
                                "level": "high",
                                "score": 0.91,
                                "rationale": "Strong human kidney single-cell specificity and experimental structure.",
                            },
                            "evidence": [
                                {
                                    "type": "hpa_single_cell",
                                    "summary": "Proximal tubular cells 518.2 nTPM.",
                                    "source": "HPA v23",
                                }
                            ],
                            "startCampaign": {
                                "gene": "LRP2",
                                "targetLabel": "LRP2 (kidney)",
                                "diseaseId": "MONDO:0010526",
                                "diseaseName": "Fabry disease",
                                "tissue": "kidney",
                                "cellType": "Proximal tubular cells",
                                "uniprotId": "P98164",
                                "preferredPdbId": "8J3Y",
                                "localization": "surface_receptor",
                                "studioUrl": "/studio?gene=LRP2&disease=MONDO:0010526&tissue=kidney",
                            },
                        }
                    ],
                    "receptors": [],
                    "evidence": [
                        {
                            "type": "hpa_single_cell",
                            "summary": "Proximal tubular cells 518.2 nTPM.",
                            "source": "HPA v23",
                        }
                    ],
                    "startCampaign": {
                        "gene": "LRP2",
                        "targetLabel": "LRP2 (kidney)",
                        "diseaseId": "MONDO:0010526",
                        "diseaseName": "Fabry disease",
                        "tissue": "kidney",
                        "cellType": "Proximal tubular cells",
                        "uniprotId": "P98164",
                        "preferredPdbId": "8J3Y",
                        "localization": "surface_receptor",
                        "studioUrl": "/studio?gene=LRP2&disease=MONDO:0010526&tissue=kidney",
                    },
                    "tookMs": 1.4,
                },
            )
        if path.endswith("/targets-graph"):
            return httpx.Response(
                200,
                json={
                    "dataVersion": "v4",
                    "surface": "ligandai.com Studio dropdown",
                    "query": "Fabry disease",
                    "disease": {
                        "identityId": "MONDO:0010526",
                        "name": "Fabry disease",
                        "derivedCoarseSystem": "Kidney",
                    },
                    "tissues": [
                        {
                            "fineBasketId": "tissue:kidney_cortex",
                            "label": "Kidney cortex",
                            "level": "tissue",
                            "derivedCoarseSystem": "Kidney",
                            "role": "therapeutic_primary",
                            "evidence": "GTEx v8 Kidney-Cortex + HPA v24 renal tubule enrichment",
                            "citation": "GTEx v8 / HPA v24",
                        }
                    ],
                    "cell_types": [
                        {
                            "cellKey": "proximal_tubular_cells",
                            "label": "Renal proximal tubular epithelial cells",
                            "clId": "CL:0002306",
                            "hpaCellType": "proximal tubular cells",
                            "tissueFineBasketId": "tissue:kidney_cortex",
                            "evidence": "Endocytic megalin expression on proximal tubule brush border",
                            "citation": "Human Protein Atlas v24 (CL:0002306)",
                        }
                    ],
                    "markers": [
                        {
                            "marker": "LRP2",
                            "proteinName": "Low-density lipoprotein receptor-related protein 2 (Megalin)",
                            "uniprotId": "P98164",
                            "isReceptor": True,
                            "isSurfaceExposed": True,
                            "targetRole": "delivery_receptor",
                            "is_platform_marker": True,
                            "selectivity": 0.94,
                            "n_diseases": 68,
                            "patient_pop": 215000,
                            "price_per_drug": 380000.0,
                            "score": 0.892,
                            "evidence": "Renal cortex endocytic uptake receptor enriched versus systemic GTEx tissues",
                            "citations": ["HPA v24 / GTEx v8 Kidney-Cortex & PDB:8F7P"],
                        }
                    ],
                    "edges": [],
                    "tookMs": 1.1,
                },
            )
        if path == "/api/atlas/markers/ranking":
            return httpx.Response(
                200,
                json={
                    "dataVersion": "v4",
                    "surface": "ligandai.com Studio dropdown",
                    "markers": [
                        {
                            "marker": "ASGR1",
                            "isReceptor": True,
                            "isSurfaceExposed": True,
                            "is_platform_marker": True,
                            "selectivity": 0.97,
                            "n_diseases": 94,
                            "patient_pop": 480000,
                            "price_per_drug": 459986.0,
                            "score": 0.941,
                            "evidence": "Hepatocyte-selective lectin uptake receptor",
                            "citations": ["HPA v24 / GTEx v8 Liver"],
                        },
                        {
                            "marker": "LRP2",
                            "isReceptor": True,
                            "isSurfaceExposed": True,
                            "is_platform_marker": True,
                            "selectivity": 0.94,
                            "n_diseases": 68,
                            "patient_pop": 215000,
                            "price_per_drug": 380000.0,
                            "score": 0.892,
                            "evidence": "Proximal tubular uptake receptor",
                            "citations": ["HPA v24 / GTEx v8 Kidney-Cortex"],
                        },
                    ],
                    "per_organ_top": [
                        {
                            "fine_basket_id": "tissue:liver_parenchyma",
                            "fine_tissue_label": "Liver parenchyma",
                            "level": "tissue",
                            "derived_coarse_system_rollup": "Liver",
                            "top_markers": [
                                {
                                    "marker": "ASGR1",
                                    "score": 0.941,
                                    "selectivity": 0.97,
                                    "n_diseases": 94,
                                    "patient_pop": 480000,
                                    "price_per_drug": 459986.0,
                                }
                            ],
                        }
                    ],
                    "platform_markers": [],
                    "tookMs": 1.5,
                },
            )
        return httpx.Response(404, json={"dataVersion": "v2", "error": {"code": "unknown_resource", "message": "Not mocked"}})

    return httpx.MockTransport(handler)


def main() -> None:
    api_key = os.environ.get("LIGANDAI_API_KEY")
    if api_key:
        client = LigandAI(api_key=api_key)
    else:
        client = LigandAI(api_key="lgai_pro_offline_demo", base_url="http://atlas.local")
        client._transport._client = httpx.Client(transport=_build_offline_transport())

    hits = client.diseases.search("hATTR")
    print(f"Search 'hATTR' -> {hits[0].identity_id}: {hits[0].name} (organ={hits[0].primary_organ})")

    mapped = client.map("Fabry kidney disease", surface_only=True, top_k=5)
    top_receptor = mapped.receptors[0] if mapped.receptors else mapped.targets[0]
    print(
        f"One-call map('Fabry kidney disease') -> disease={mapped.disease.name}, "
        f"tissue={mapped.matched_tissue_filter}, top_receptor={top_receptor.gene} "
        f"(score={top_receptor.overall_score})"
    )

    graph = client.diseases.map_disease_to_targets("Fabry disease", tissue="kidney", top_k=5)
    top_marker = graph.markers[0]
    print(
        f"Graph map_disease_to_targets('Fabry disease') -> tissues={len(graph.tissues)}, "
        f"cell_types={len(graph.cell_types)}, top_marker={top_marker.marker} "
        f"(score={top_marker.score}, n_diseases={top_marker.n_diseases})"
    )

    ranking = client.markers.ranking(platform_only=True, top_k=5, seed=42)
    print(
        f"Marker ranking (platform_only=True, seed=42) -> top={ranking.markers[0].marker} "
        f"(score={ranking.markers[0].score}, digest={ranking.reproducibility['deterministic_digest'][:12]})"
    )

    # Non-superadmin keys are blocked client-side before any superadmin request leaves the process
    if not str(client.api_key or "").startswith(("lgai_sa_", "lgai_superadmin_")):
        try:
            client.diseases.matrix()
        except LigandAIForbidden as exc:
            print(f"Superadmin role check verified client-side: {exc.code} (HTTP {exc.status_code})")


if __name__ == "__main__":
    main()
