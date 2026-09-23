# Copyright © 2026 Ligandal, Inc. All rights reserved.
"""Hermetic contract tests for the signed H-SWAE SDK capability surface."""

from __future__ import annotations

import copy
import hashlib
import json
import math
from typing import Any

import pytest
from pydantic import ValidationError
from pytest_httpx import HTTPXMock

import ligandai.types as ligandai_types
from ligandai import AsyncLigandAI, LigandAI
from ligandai.types import (
    HSWAE_CAPABILITY_DEFINITION_HASHES,
    HswaeAnnotateDatasetReceipt,
    HswaeCompareCellTypesReceipt,
    HswaeComplexSpecificityReceipt,
    HswaeIsoformExpressionReceipt,
    HswaeResolveCellTypeReceipt,
    HswaeSemanticSearchReceipt,
    HswaeSurfaceMarkersReceipt,
)

BASE = "http://api.ligandai.test"
CAPABILITIES_URL = f"{BASE}/api/transcriptomics/capabilities"
INVOKE_URL = f"{CAPABILITIES_URL}/invoke"
LEGACY_ISOFORM_URL = f"{BASE}/api/transcriptomics/hswae/isoform-expression"

OPERATIONS = (
    "hswae.v1.semantic_search",
    "hswae.v1.surface_markers",
    "hswae.v2.resolve_cell_type",
    "hswae.v2.compare_cell_types",
    "hswae.v2.isoform_expression",
    "hswae.v2.annotate_dataset",
    "predictive_interactomics.complex_specificity",
)


def _schema() -> dict[str, Any]:
    return {
        "type": "object",
        "additionalProperties": False,
        "properties": {
            "query": {"type": "string", "minLength": 1, "maxLength": 2_000},
        },
        "required": ["query"],
    }


def _capability(operation: str) -> dict[str, Any]:
    backend = "hswae_v1" if operation.startswith("hswae.v1") else "hswae_v2"
    complex_operation = operation == "predictive_interactomics.complex_specificity"
    return {
        "operation": operation,
        "title": f"Signed {operation}",
        "description": "A deployment-filtered signed immediate capability.",
        "domain": "predictive_interactomics" if complex_operation else "transcriptomics",
        "effect": "read_local",
        "executionMode": "immediate",
        "requiredEntitlements": [
            "predictive_interactomics.read" if complex_operation else "transcriptomics.read"
        ],
        "requiredApprovalScopes": [],
        "inputSchema": _schema(),
        "outputSchema": _schema(),
        "backendFamily": backend,
    }


def _discovery_payload() -> dict[str, Any]:
    return {
        "contractVersion": "1.0.0",
        "capabilities": [_capability(operation) for operation in OPERATIONS],
    }


def _canonical_json_sha256(value: Any) -> str:
    encoded = json.dumps(
        value,
        allow_nan=False,
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
    ).encode()
    return hashlib.sha256(encoded).hexdigest()


@pytest.fixture
def synthetic_definition_pins(monkeypatch: pytest.MonkeyPatch) -> None:
    """Pin synthetic discovery rows while production pins stay separately asserted."""

    monkeypatch.setattr(
        ligandai_types,
        "HSWAE_CAPABILITY_DEFINITION_HASHES",
        {operation: _canonical_json_sha256(_capability(operation)) for operation in OPERATIONS},
    )


def _semantic_result() -> dict[str, Any]:
    return {
        "query": "liver",
        "matches": [
            {
                "tissue": "Liver",
                "score": 0.92,
                "match_type": "exact",
                "source_entity": "Liver",
                "cell_type": "hepatocyte",
            }
        ],
        "count": 1,
    }


def _surface_result() -> dict[str, Any]:
    return {
        "query": "hepatocyte",
        "matched_tissues": _semantic_result()["matches"],
        "primary_tissue": "Liver",
        "markers": [
            {
                "gene": "ASGR1",
                "targetContext": "hepatocyte",
                "method": "surface_specificity",
                "similarity": 0.87,
                "meanTpm": 12.5,
                "specificityIndex": 7.4,
                "rank": 1,
                "ensemblGene": "ENSG00000141505",
                "uniprotId": "P07307",
                "proteinName": "ASGPR1",
            }
        ],
        "timing_ms": 3.25,
        "method": "semantic_search_markers",
    }


def _resolve_result() -> dict[str, Any]:
    return {
        "query": "hep",
        "canonical": "hepatocyte",
        "candidates": [
            {
                "cell_type": "hepatocyte",
                "confidence": 0.96,
                "top_tissue": "Liver",
                "top_organ_system": "Digestive system",
                "source": "hswae_v2",
                "proxy_for": None,
                "proxy_note": None,
                "alias_query": "hep",
                "alias_marker_genes": ["ALB", "ASGR1"],
                "requires_geo_ingest": [],
                "supportingGenes": [{"gene": "ALB", "csi": 8.1, "rankForGene": 1}],
                "vote_count": 2,
                "marker_score": 0.91,
            }
        ],
        "delivery_context_hint": {
            "default_scope": "systemic",
            "rationale": "The target context is systemically accessible.",
            "alternatives": ["tissue_resident"],
        },
    }


def _compare_result() -> dict[str, Any]:
    return {
        "target_cell_type": "hepatocyte",
        "target_query": "hep",
        "target_proxy_note": None,
        "reference_cell_types": ["cholangiocyte"],
        "differential": [
            {
                "gene_name": "ASGR1",
                "target_cell_type": "hepatocyte",
                "target_csi": 8.0,
                "target_rank_global": 1,
                "target_mean_expression": 12.5,
                "max_reference_csi": 1.2,
                "delta_csi": 6.8,
                "is_surface_annotated": True,
                "top_tissue": "Liver",
                "top_organ_system": "Digestive system",
            }
        ],
        "comparator_scope": "systemic",
        "comparator_scope_source": "request",
        "delivery_context": "systemic",
    }


def _isoform_result() -> dict[str, Any]:
    return {
        "gene": "CD44",
        "isoform_count": 1,
        "isoforms": [
            {
                "transcript_id": "ENST00000263398",
                "transcript_id_versioned": "ENST00000263398.11",
                "transcript_name": "CD44-201",
                "transcript_type": "protein_coding",
                "expression_by_context": [
                    {
                        "tissue": "Liver",
                        "cell_type": "hepatocyte",
                        "context_name": "Liver/hepatocyte",
                        "mean_tpm": 10.0,
                        "median_tpm": 8.0,
                        "max_tpm": 20.0,
                        "prevalence_tpm1": 0.75,
                        "sample_count": 12,
                    }
                ],
                "specificity_by_context": [
                    {
                        "tissue": "Liver",
                        "cell_type": "hepatocyte",
                        "transcript_si": 2.1,
                        "usage_ratio": 0.6,
                        "isi": 1.8,
                        "isoform_usage": 0.55,
                        "target_mean_tpm": 10.0,
                        "gene_mean_tpm": 18.0,
                        "rank_expression": 1,
                        "rank_usage": 2,
                    }
                ],
            }
        ],
        "resolution": "cell_type",
        "note": "Cell-type resolved H-SWAEV2 result.",
    }


def _annotation_result() -> dict[str, Any]:
    return {
        "datasetRef": "dataset_opaque_1",
        "clusterAssignments": [
            {
                "clusterId": "cluster-0",
                "markerCount": 2,
                "candidates": [
                    {
                        "cellType": "hepatocyte",
                        "confidence": 0.94,
                        "supportingGenes": [{"gene": "ALB", "csi": 8.1, "rankForGene": 1}],
                        "voteCount": 2,
                        "markerScore": 0.9,
                        "topTissue": "Liver",
                        "topOrganSystem": "Digestive system",
                    }
                ],
                "topAssignment": "hepatocyte",
                "confidence": 0.94,
            }
        ],
        "method": "marker_overlap_vote",
        "reference": "HSWAE-2 cell_type_global_specificity (HPA v25 surfaceome)",
        "source": "authorized_user_dataset",
        "note": None,
    }


def _complex_result() -> dict[str, Any]:
    return {
        "complexName": "IL2RA/IL2RB",
        "subunits": ["IL2RA", "IL2RB"],
        "contexts": [
            {
                "tissue": "Peripheral blood",
                "cell_type": "activated T cell",
                "cluster": "T-cell cluster 4",
                "subunit_expression": [
                    {"subunit": "IL2RA", "expression": 4.5},
                    {"subunit": "IL2RB", "expression": 3.25},
                ],
                "expressed_subunits": 2,
                "required_subunits": 2,
                "limiting_subunit": "IL2RB",
                "limiting_expression": 3.25,
                "max_subunit_expression": 4.5,
                "geometric_mean_expression": 3.824,
                "balance_score": 0.85,
                "complex_signal": 3.25,
                "heteromer_si": 2.75,
                "heteromer_si_denominator": 1.18,
                "competitor_below_floor": False,
                "competitor_tissue": "Liver",
                "competitor_cell_type": "hepatocyte",
                "competitor_signal": 0.4,
                "rank": 1,
                "source_dataset": "hpa_v25_single_cell_cluster",
            }
        ],
    }


INPUTS: dict[str, dict[str, Any]] = {
    "hswae.v1.semantic_search": {"query": "liver", "k": 5},
    "hswae.v1.surface_markers": {
        "context": "hepatocyte",
        "top_n": 25,
        "exclude_contexts": ["fetal liver"],
    },
    "hswae.v2.resolve_cell_type": {
        "query": "hep",
        "marker_genes": ["ALB", "ASGR1"],
        "top_k": 5,
    },
    "hswae.v2.compare_cell_types": {
        "target_cell_type": "hepatocyte",
        "reference_cell_types": ["cholangiocyte"],
        "comparator_scope": "systemic",
        "delivery_context": "systemic",
        "top_n": 25,
    },
    "hswae.v2.isoform_expression": {
        "gene": "CD44",
        "top_n_isoforms": 10,
        "min_mean_tpm": 0.5,
    },
    "hswae.v2.annotate_dataset": {"dataset_ref": "dataset_opaque_1", "top_n": 3},
    "predictive_interactomics.complex_specificity": {
        "subunits": ["IL2RA", "IL2RB"],
        "complex_name": "IL2RA/IL2RB",
        "min_subunit_tpm": 0.1,
        "limit": 50,
    },
}

RESULTS = {
    "hswae.v1.semantic_search": _semantic_result(),
    "hswae.v1.surface_markers": _surface_result(),
    "hswae.v2.resolve_cell_type": _resolve_result(),
    "hswae.v2.compare_cell_types": _compare_result(),
    "hswae.v2.isoform_expression": _isoform_result(),
    "hswae.v2.annotate_dataset": _annotation_result(),
    "predictive_interactomics.complex_specificity": _complex_result(),
}

RECEIPT_TYPES = {
    "hswae.v1.semantic_search": HswaeSemanticSearchReceipt,
    "hswae.v1.surface_markers": HswaeSurfaceMarkersReceipt,
    "hswae.v2.resolve_cell_type": HswaeResolveCellTypeReceipt,
    "hswae.v2.compare_cell_types": HswaeCompareCellTypesReceipt,
    "hswae.v2.isoform_expression": HswaeIsoformExpressionReceipt,
    "hswae.v2.annotate_dataset": HswaeAnnotateDatasetReceipt,
    "predictive_interactomics.complex_specificity": HswaeComplexSpecificityReceipt,
}


def _receipt(operation: str) -> dict[str, Any]:
    return {
        "receiptVersion": "1",
        "contractVersion": "1.0.0",
        "requestId": "req_public_1",
        "operation": operation,
        "owner": {"userId": "user_public_1", "orgId": None},
        "status": "executed",
        "effect": "read_local",
        "createdAt": "2026-08-19T12:00:00.000Z",
        "result": copy.deepcopy(RESULTS[operation]),
    }


def _request_json(httpx_mock: HTTPXMock) -> dict[str, Any]:
    request = httpx_mock.get_request()
    assert request is not None
    payload: dict[str, Any] = json.loads(request.content)
    return payload


def test_definition_hashes_pin_the_exact_reviewed_registry() -> None:
    assert dict(HSWAE_CAPABILITY_DEFINITION_HASHES) == {
        "hswae.v1.semantic_search": (
            "059b7dabfe64b95df6db302e19c41db4140b21eeb17c1fee1d601942ab1e4530"
        ),
        "hswae.v1.surface_markers": (
            "4f19e07593de43bc4daecf02229c7033e35248cf639b4fb9d96ddc2b6d8dadc9"
        ),
        "hswae.v2.resolve_cell_type": (
            "78ff3122da897fab5d1e49ec2a0f01cd8e90e76e9c0ec8c6ca35d88c03b8ca40"
        ),
        "hswae.v2.compare_cell_types": (
            "cd752bf2c9284510cbabbbcd89f8be4be2cf0817c0f676aa73f12e53f0e90881"
        ),
        "hswae.v2.isoform_expression": (
            "1716acbd629ea944bebd9fc33df97a1f721211e8776bacfec455b38df0164ad2"
        ),
        "hswae.v2.annotate_dataset": (
            "53ba38fff81386bc01c6b98d24e17237c9e7156cf546654efe317acc273b43af"
        ),
        "predictive_interactomics.complex_specificity": (
            "26938db63750694e25c0ba1b6583a3dc90354f24441072e2f5d64ea3a88b0a06"
        ),
    }


def test_capabilities_get_is_closed_and_typed(
    synthetic_definition_pins: None,
    httpx_mock: HTTPXMock,
    client: LigandAI,
) -> None:
    httpx_mock.add_response(method="GET", url=CAPABILITIES_URL, json=_discovery_payload())

    response = client.discovery.capabilities()

    assert response.contract_version == "1.0.0"
    assert [capability.operation for capability in response.capabilities] == list(OPERATIONS)
    assert response.capabilities[0].input_schema.additional_properties is False
    request = httpx_mock.get_request()
    assert request is not None
    assert request.method == "GET"


async def test_capabilities_async_matches_sync_contract(
    synthetic_definition_pins: None,
    httpx_mock: HTTPXMock,
    async_client: AsyncLigandAI,
) -> None:
    httpx_mock.add_response(method="GET", url=CAPABILITIES_URL, json=_discovery_payload())

    response = await async_client.discovery.capabilities()

    assert response.contract_version == "1.0.0"
    assert [capability.operation for capability in response.capabilities] == list(OPERATIONS)


@pytest.mark.parametrize("operation", OPERATIONS)
def test_invoke_capability_sync_exact_body_and_all_result_fields(
    operation: str,
    httpx_mock: HTTPXMock,
    client: LigandAI,
) -> None:
    httpx_mock.add_response(method="POST", url=INVOKE_URL, json=_receipt(operation))

    receipt = client.discovery.invoke_capability(operation, INPUTS[operation])  # type: ignore[call-overload]

    assert isinstance(receipt, RECEIPT_TYPES[operation])
    assert receipt.operation == operation
    assert receipt.model_dump(mode="json", by_alias=True)["result"] == RESULTS[operation]
    assert _request_json(httpx_mock) == {
        "operation": operation,
        "input": INPUTS[operation],
    }


@pytest.mark.parametrize("operation", OPERATIONS)
async def test_invoke_capability_async_exact_body_and_all_result_fields(
    operation: str,
    httpx_mock: HTTPXMock,
    async_client: AsyncLigandAI,
) -> None:
    httpx_mock.add_response(method="POST", url=INVOKE_URL, json=_receipt(operation))

    receipt = await async_client.discovery.invoke_capability(  # type: ignore[call-overload]
        operation,
        INPUTS[operation],
    )

    assert isinstance(receipt, RECEIPT_TYPES[operation])
    assert receipt.operation == operation
    assert receipt.model_dump(mode="json", by_alias=True)["result"] == RESULTS[operation]
    assert _request_json(httpx_mock) == {
        "operation": operation,
        "input": INPUTS[operation],
    }


def test_invoke_capability_does_not_invent_optional_defaults(
    httpx_mock: HTTPXMock,
    client: LigandAI,
) -> None:
    httpx_mock.add_response(
        method="POST",
        url=INVOKE_URL,
        json=_receipt("hswae.v1.semantic_search"),
    )

    client.discovery.invoke_capability("hswae.v1.semantic_search", {"query": "liver"})

    assert _request_json(httpx_mock) == {
        "operation": "hswae.v1.semantic_search",
        "input": {"query": "liver"},
    }


@pytest.mark.parametrize(
    "identity_key",
    [
        "userId",
        "user_id",
        "orgId",
        "org_id",
        "requestId",
        "apiKeyId",
        "tier",
        "entitlements",
        "approval_id",
    ],
)
def test_caller_identity_fields_fail_before_http(
    identity_key: str,
    httpx_mock: HTTPXMock,
    client: LigandAI,
) -> None:
    input_data = {"query": "liver", identity_key: "caller-controlled"}

    with pytest.raises(ValidationError):
        client.discovery.invoke_capability("hswae.v1.semantic_search", input_data)  # type: ignore[call-overload]

    assert httpx_mock.get_requests() == []


@pytest.mark.parametrize(
    ("operation", "input_data"),
    [
        ("hswae.v1.semantic_search", {"query": "liver", "k": True}),
        ("hswae.v1.semantic_search", {"query": "liver", "k": 51}),
        ("hswae.v1.semantic_search", {"query": "x" * 2_001}),
        ("hswae.v1.semantic_search", []),
        ("hswae.v1.surface_markers", {"context": "liver", "exclude_contexts": ()}),
        ("hswae.v1.surface_markers", {"context": "liver", "topN": 10}),
        ("hswae.v2.resolve_cell_type", {"query": "hep", "marker_genes": ["G"] * 501}),
        (
            "hswae.v2.compare_cell_types",
            {"target_cell_type": "hep", "reference_cell_types": ("other",)},
        ),
        ("hswae.v2.isoform_expression", {"gene": "CD44", "min_mean_tpm": math.nan}),
        ("hswae.v2.isoform_expression", {"gene": "CD44", "min_mean_tpm": math.inf}),
        ("hswae.v2.annotate_dataset", {"dataset_ref": "d", "top_n": 11}),
        (
            "predictive_interactomics.complex_specificity",
            {"subunits": ["IL2RA"]},
        ),
        (
            "predictive_interactomics.complex_specificity",
            {"subunits": ["IL2RA", " il2ra "]},
        ),
        (
            "predictive_interactomics.complex_specificity",
            {"subunits": ["IL2RA", "IL2RB"], "min_subunit_tpm": math.inf},
        ),
        (
            "predictive_interactomics.complex_specificity",
            {"subunits": ["IL2RA", "IL2RB"], "limit": 201},
        ),
        (
            "predictive_interactomics.complex_specificity",
            {"subunits": ["IL2RA", "IL2RB"], "min_subunit_tpm": True},
        ),
        (
            "predictive_interactomics.complex_specificity",
            {"subunits": ["IL2RA", "IL2RB"], "complex_name": "bad\x00name"},
        ),
        (
            "predictive_interactomics.complex_specificity",
            {"subunits": ["IL2RA", "IL2RB"], "dataset_ref": "/mnt/private/data.db"},
        ),
        (
            "predictive_interactomics.complex_specificity",
            {"subunits": ("IL2RA", "IL2RB")},
        ),
    ],
)
def test_malformed_nonfinite_and_oversize_inputs_fail_before_http(
    operation: str,
    input_data: Any,
    httpx_mock: HTTPXMock,
    client: LigandAI,
) -> None:
    with pytest.raises(ValidationError):
        client.discovery.invoke_capability(operation, input_data)  # type: ignore[call-overload]

    assert httpx_mock.get_requests() == []


def test_complex_input_is_normalized_before_http(
    httpx_mock: HTTPXMock,
    client: LigandAI,
) -> None:
    httpx_mock.add_response(
        method="POST",
        url=INVOKE_URL,
        json=_receipt("predictive_interactomics.complex_specificity"),
    )

    client.discovery.invoke_capability(
        "predictive_interactomics.complex_specificity",
        {"subunits": [" il2ra ", "Il2Rb"]},
    )

    assert _request_json(httpx_mock) == {
        "operation": "predictive_interactomics.complex_specificity",
        "input": {"subunits": ["IL2RA", "IL2RB"]},
    }


async def test_async_invalid_input_fails_before_http(
    httpx_mock: HTTPXMock,
    async_client: AsyncLigandAI,
) -> None:
    with pytest.raises(ValidationError):
        await async_client.discovery.invoke_capability(
            "hswae.v2.isoform_expression",
            {"gene": "CD44", "min_mean_tpm": math.inf},
        )

    assert httpx_mock.get_requests() == []


@pytest.mark.parametrize(
    "operation",
    [
        "hswae.coverage.assess",
        "geo.discovery.plan",
        "geo.import.execute",
        "hswae.variant.finetune.execute",
        "hswae.variant.register",
    ],
)
def test_unintegrated_and_planned_operations_are_not_exposed(
    operation: str,
    httpx_mock: HTTPXMock,
    client: LigandAI,
) -> None:
    with pytest.raises(ValueError, match="unsupported H-SWAE capability operation"):
        client.discovery.invoke_capability(operation, {})  # type: ignore[call-overload]

    assert httpx_mock.get_requests() == []


@pytest.mark.parametrize(
    "case",
    [
        "top_level_extra",
        "owner_extra",
        "result_extra",
        "nonfinite",
        "oversize",
        "wrong_status",
        "missing_result",
        "operation_mismatch",
    ],
)
def test_malformed_server_receipts_fail_closed(
    case: str,
    httpx_mock: HTTPXMock,
    client: LigandAI,
) -> None:
    payload = _receipt("hswae.v1.semantic_search")
    if case == "top_level_extra":
        payload["internalPath"] = "/srv/private/model.bin"
    elif case == "owner_extra":
        payload["owner"]["apiKeyId"] = "private-key-id"
    elif case == "result_extra":
        payload["result"]["databaseTable"] = "cell_context_expression"
    elif case == "nonfinite":
        payload["result"]["matches"][0]["score"] = math.inf
    elif case == "oversize":
        payload["result"]["matches"] = payload["result"]["matches"] * 51
    elif case == "wrong_status":
        payload["status"] = "planned"
    elif case == "missing_result":
        del payload["result"]
    elif case == "operation_mismatch":
        payload = _receipt("hswae.v1.surface_markers")
    else:  # pragma: no cover - parameter list is exhaustive
        raise AssertionError(case)
    if case == "nonfinite":
        httpx_mock.add_response(
            method="POST",
            url=INVOKE_URL,
            content=json.dumps(payload).encode(),
            headers={"Content-Type": "application/json"},
        )
    else:
        httpx_mock.add_response(method="POST", url=INVOKE_URL, json=payload)

    if case == "operation_mismatch":
        with pytest.raises(ValueError, match="receipt operation mismatch"):
            client.discovery.invoke_capability(
                "hswae.v1.semantic_search",
                {"query": "liver"},
            )
    else:
        with pytest.raises(ValidationError):
            client.discovery.invoke_capability(
                "hswae.v1.semantic_search",
                {"query": "liver"},
            )


@pytest.mark.parametrize(
    "case",
    [
        "private_source",
        "private_tissue",
        "internal_cell_type",
        "uri_cluster",
        "encoded_competitor",
        "context_extra",
        "nonfinite",
        "bool_as_number",
        "subunit_order",
        "count_mismatch",
        "unknown_limiter",
        "oversize_contexts",
    ],
)
def test_malformed_complex_receipts_fail_closed(
    case: str,
    httpx_mock: HTTPXMock,
    client: LigandAI,
) -> None:
    operation = "predictive_interactomics.complex_specificity"
    payload = _receipt(operation)
    context = payload["result"]["contexts"][0]
    if case == "private_source":
        context["source_dataset"] = "/mnt/private/expression.db"
    elif case == "private_tissue":
        context["tissue"] = "/mnt/private/expression.db"
    elif case == "internal_cell_type":
        context["cell_type"] = "sqlite_master"
    elif case == "uri_cluster":
        context["cluster"] = "file:atlas"
    elif case == "encoded_competitor":
        context["competitor_tissue"] = "%2fetc"
    elif case == "context_extra":
        context["internalTable"] = "cell_context_expression"
    elif case == "nonfinite":
        context["heteromer_si"] = math.inf
    elif case == "bool_as_number":
        context["competitor_signal"] = True
    elif case == "subunit_order":
        context["subunit_expression"].reverse()
    elif case == "count_mismatch":
        context["required_subunits"] = 3
    elif case == "unknown_limiter":
        context["limiting_subunit"] = "PRIVATE_GENE"
    elif case == "oversize_contexts":
        payload["result"]["contexts"] = [copy.deepcopy(context) for _ in range(201)]
    else:  # pragma: no cover - parameter list is exhaustive
        raise AssertionError(case)
    if case == "nonfinite":
        httpx_mock.add_response(
            method="POST",
            url=INVOKE_URL,
            content=json.dumps(payload).encode(),
            headers={"Content-Type": "application/json"},
        )
    else:
        httpx_mock.add_response(method="POST", url=INVOKE_URL, json=payload)

    with pytest.raises(ValidationError):
        client.discovery.invoke_capability(
            operation,
            {"subunits": ["IL2RA", "IL2RB"]},
        )


@pytest.mark.parametrize(
    "input_data",
    [
        {"subunits": ["CD3D", "CD3E"]},
        {"subunits": ["IL2RA", "IL2RB"], "complex_name": "Requested complex"},
        {"subunits": ["IL2RA", "IL2RB"], "limit": 1},
    ],
)
def test_complex_receipt_is_bound_to_the_normalized_request(
    input_data: dict[str, Any],
    httpx_mock: HTTPXMock,
    client: LigandAI,
) -> None:
    payload = _receipt("predictive_interactomics.complex_specificity")
    if input_data.get("limit") == 1:
        payload["result"]["contexts"].append(copy.deepcopy(payload["result"]["contexts"][0]))
    httpx_mock.add_response(method="POST", url=INVOKE_URL, json=payload)

    with pytest.raises(ValueError, match="complex result"):
        client.discovery.invoke_capability(
            "predictive_interactomics.complex_specificity",
            input_data,
        )


@pytest.mark.parametrize(
    "case",
    [
        "top_level_extra",
        "capability_extra",
        "planned_operation",
        "wrong_effect",
        "nonfinite_schema",
        "missing_closed_root",
        "open_root",
        "oversize_description",
        "duplicate_operation",
    ],
)
def test_malformed_discovery_payloads_fail_closed(
    case: str,
    synthetic_definition_pins: None,
    httpx_mock: HTTPXMock,
    client: LigandAI,
) -> None:
    payload = _discovery_payload()
    if case == "top_level_extra":
        payload["privateRegistryPath"] = "/srv/private/registry.json"
    elif case == "capability_extra":
        payload["capabilities"][0]["host"] = "127.0.0.1"
    elif case == "planned_operation":
        payload["capabilities"][0]["operation"] = "hswae.coverage.assess"
    elif case == "wrong_effect":
        payload["capabilities"][0]["effect"] = "network_read"
    elif case == "nonfinite_schema":
        payload["capabilities"][0]["inputSchema"]["properties"]["query"]["maximum"] = math.inf
    elif case == "missing_closed_root":
        del payload["capabilities"][0]["inputSchema"]["additionalProperties"]
    elif case == "open_root":
        payload["capabilities"][0]["outputSchema"]["additionalProperties"] = True
    elif case == "oversize_description":
        payload["capabilities"][0]["description"] = "x" * 10_001
    elif case == "duplicate_operation":
        payload["capabilities"][1]["operation"] = payload["capabilities"][0]["operation"]
    else:  # pragma: no cover - parameter list is exhaustive
        raise AssertionError(case)
    if case == "nonfinite_schema":
        httpx_mock.add_response(
            method="GET",
            url=CAPABILITIES_URL,
            content=json.dumps(payload).encode(),
            headers={"Content-Type": "application/json"},
        )
    else:
        httpx_mock.add_response(method="GET", url=CAPABILITIES_URL, json=payload)

    with pytest.raises(ValidationError):
        client.discovery.capabilities()


async def test_async_isoform_expression_is_exact_sync_twin(
    httpx_mock: HTTPXMock,
    client: LigandAI,
    async_client: AsyncLigandAI,
) -> None:
    response = {"gene": "CD44", "isoforms": [], "resolution": "none"}
    httpx_mock.add_response(method="POST", url=LEGACY_ISOFORM_URL, json=response)
    httpx_mock.add_response(method="POST", url=LEGACY_ISOFORM_URL, json=response)

    sync_result = client.discovery.isoform_expression(
        "CD44",
        top_n_isoforms=7,
        min_mean_tpm=0.25,
    )
    async_result = await async_client.discovery.isoform_expression(
        "CD44",
        top_n_isoforms=7,
        min_mean_tpm=0.25,
    )

    assert sync_result == async_result == response
    requests = httpx_mock.get_requests()
    assert len(requests) == 2
    expected_body = {"gene": "CD44", "top_n_isoforms": 7, "min_mean_tpm": 0.25}
    assert json.loads(requests[0].content) == expected_body
    assert json.loads(requests[1].content) == expected_body


async def test_async_isoform_expression_preserves_empty_fallback(
    httpx_mock: HTTPXMock,
    async_client: AsyncLigandAI,
) -> None:
    httpx_mock.add_response(method="POST", url=LEGACY_ISOFORM_URL, json={})

    result = await async_client.discovery.isoform_expression("CD44")

    assert result == {"gene": "CD44", "isoforms": [], "resolution": "none"}
