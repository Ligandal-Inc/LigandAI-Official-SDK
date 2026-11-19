# Copyright © 2026 Ligandal, Inc. All rights reserved.
"""Discovery / transcriptomics — tissue markers, scRNA, GEO, comparisons."""

from __future__ import annotations

import asyncio
import re
import time
from collections.abc import Mapping
from pathlib import Path
from typing import Any, Literal, cast, overload

from pydantic import TypeAdapter

from ligandai._geo import (
    GEO_PATH,
    GEO_TERMINAL_STATUSES,
    GeoImportTimeout,
    geo_accession,
    geo_import_selection,
    geo_job_id,
    geo_poll_parameters,
    geo_search_params,
    parse_geo_inspection,
    parse_geo_job,
    parse_geo_search,
)
from ligandai.errors import (
    LigandAILegalAcceptanceRequired,
    LigandAIUnreadableResponse,
    resolve_mutation_failure,
)
from ligandai.resources._base import AsyncResource, Resource, unwrap_rows
from ligandai.types import (
    BBBReceptor,
    ComparisonResponse,
    CustomDatasetTarget,
    Dataset,
    ExpressionProfile,
    GeoDataset,
    GeoImportJob,
    GeoInspection,
    GeoSearchResponse,
    HswaeAnnotateDatasetInput,
    HswaeAnnotateDatasetReceipt,
    HswaeCapabilityDiscovery,
    HswaeCapabilityOperation,
    HswaeCapabilityReceipt,
    HswaeCompareCellTypesInput,
    HswaeCompareCellTypesReceipt,
    HswaeComplexSpecificityInput,
    HswaeComplexSpecificityReceipt,
    HswaeIsoformExpressionInput,
    HswaeIsoformExpressionReceipt,
    HswaeResolveCellTypeInput,
    HswaeResolveCellTypeReceipt,
    HswaeSemanticSearchInput,
    HswaeSemanticSearchReceipt,
    HswaeSurfaceMarkersInput,
    HswaeSurfaceMarkersReceipt,
    MarkerResponse,
    ReferenceGroup,
    TargetGroup,
    validate_hswae_capability_input,
)

_HSWAE_CAPABILITY_DISCOVERY_PATH = "/api/transcriptomics/capabilities"
_HSWAE_CAPABILITY_INVOKE_PATH = "/api/transcriptomics/capabilities/invoke"
_HSWAE_RECEIPT_ADAPTER: TypeAdapter[HswaeCapabilityReceipt] = TypeAdapter(HswaeCapabilityReceipt)


def _parse_dataset_list(payload: Any) -> list[Dataset]:
    """A missing/mismatched response is not a measurement of zero datasets."""
    if isinstance(payload, dict) and payload.get("success") is False:
        raise LigandAIUnreadableResponse(
            "Dataset listing returned success:false; no dataset count was obtained.",
            cause="backend_negative_answer",
            response=payload,
        )
    # Import-job envelopes are intentionally not accepted as dataset rows.
    rows = unwrap_rows(payload, ("datasets",), what="GET /api/transcriptomics/datasets")
    # The live alias answers ``{"jobs": [...]}``, which ``unwrap_rows`` already refuses on
    # the envelope key alone. That protection was only ever incidental: a job row arriving
    # under a ``datasets`` key still validated, because ``datasetId`` satisfies ``id`` and
    # ``name``/``type`` are now optional, so a GEO import job would surface as a Dataset
    # carrying a bare id. Refuse on the row's own discriminator instead of the envelope key.
    for row in rows:
        if isinstance(row, dict) and "jobId" in row:
            raise LigandAIUnreadableResponse(
                "Dataset listing returned GEO import jobs rather than datasets. "
                "GET /api/transcriptomics/datasets is server-side synthesised from "
                "/api/datasets/geo/jobs; poll those with wait_for_import, not list_datasets.",
                cause="import_jobs_not_datasets",
                response=payload,
            )
    return [Dataset.model_validate(row) for row in rows]


def _dataset_projection_request(
    dataset_id: int, projection_scope: str, group_ids: list[int] | None,
    top_k: int, persist: bool,
) -> dict[str, Any]:
    if type(dataset_id) is not int or dataset_id < 1:
        raise ValueError("dataset_id must be a positive integer")
    if projection_scope not in ("cell_types", "sample_profiles"):
        raise ValueError("projection_scope must be cell_types or sample_profiles")
    if type(top_k) is not int or not 1 <= top_k <= 100 or type(persist) is not bool:
        raise ValueError("top_k must be an integer in 1-100 and persist must be boolean")
    if group_ids is not None and (
        not isinstance(group_ids, list) or not group_ids
        or any(type(value) is not int or value < 1 for value in group_ids)
        or len(set(group_ids)) != len(group_ids)
    ):
        raise ValueError("group_ids must contain unique positive integer dataset group IDs")
    body: dict[str, Any] = {
        "datasetId": dataset_id, "projectionScope": projection_scope,
        "topK": top_k, "persist": persist,
    }
    if group_ids is not None:
        # The server's legacy parameter names the group-table IDs, including
        # sample groups. It is not a biological cell-type annotation.
        body["cellTypeIds"] = list(group_ids)
    return body


def _parse_dataset_projection(payload: Any, dataset_id: int, projection_scope: str) -> dict[str, Any]:
    valid = isinstance(payload, dict) and payload.get("success") is True and payload.get("available") is True
    if valid:
        valid = type(payload.get("datasetId")) is int and payload["datasetId"] == dataset_id
        valid = valid and payload.get("projectionScope") == projection_scope
        groups = payload.get("regionEmbeddings")
        valid = valid and isinstance(groups, dict) and bool(groups)
        if projection_scope == "sample_profiles":
            count = payload.get("sampleCount")
            valid = valid and type(count) is int and count > 0 and payload.get("groupCount") == count
            valid = valid and isinstance(groups, dict) and len(groups) == count
            cell_count = payload.get("cellTypeCount")
            unit = payload.get("expressionUnit")
            # A compliant bulk backend may express "no cell types" either by omitting the
            # key or by sending 0. Those are the same assertion, and rejecting one of them
            # fails a valid response. Only a POSITIVE count means the server answered in
            # cell-type mode, which is the actual confusion this guard exists to catch.
            # ``type(...) is int`` also excludes ``False``, which ``== 0`` would accept.
            cell_count_absent_or_zero = cell_count is None or (type(cell_count) is int and cell_count == 0)
            # A unit label is not case-significant; "TPM" and "tpm" denote the same unit.
            # Every other value, including an absent one, still refuses.
            unit_is_tpm = isinstance(unit, str) and unit.casefold() == "tpm"
            valid = valid and cell_count_absent_or_zero and unit_is_tpm
    if not valid:
        raise LigandAIUnreadableResponse(
            "Dataset projection did not return an available result for the requested dataset and scope. "
            "Bulk sample mode requires a compatible backend; it must not fall back to cell-type interpretation.",
            cause="invalid_projection_receipt", response=payload,
        )
    return cast(dict[str, Any], payload)



# -- Private patient transcriptomics ------------------------------------------
# These endpoints serve encrypted, prepared, single-bulk-sample research
# results for one named patient. They are not a public-atlas surface and the
# validation below is correspondingly unforgiving: a partial or ambiguous
# answer about a person's data must raise, never degrade into an empty list, a
# dropped row or a silently re-POSTed write.
_PATIENT_SLUG_RE = re.compile(r"\A[a-z0-9_-]{1,80}\Z")
_UUID_RE = re.compile(r"\A[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-"
                      r"[0-9a-fA-F]{4}-[0-9a-fA-F]{12}\Z")
_SHA256_RE = re.compile(r"\A[0-9a-f]{64}\Z")


def _patient_segments(org_slug: Any, patient_slug: Any) -> str:
    """Two strict path segments. Slugs are interpolated into the URL path, so
    anything outside the accepted alphabet is refused here rather than encoded
    and sent: a stray slash or dot-segment would change which patient is read."""
    for label, value in (("org_slug", org_slug), ("patient_slug", patient_slug)):
        if not isinstance(value, str) or _PATIENT_SLUG_RE.match(value) is None:
            raise ValueError(
                f"{label} must match [a-z0-9_-]{{1,80}}; got {value!r}"
            )
    return f"{org_slug}/{patient_slug}"


def _patient_uuid(dataset_id: Any) -> str:
    if not isinstance(dataset_id, str) or _UUID_RE.match(dataset_id) is None:
        raise ValueError(f"dataset_id must be a UUID string; got {dataset_id!r}")
    return dataset_id


def _require_positive_int(value: Any, *, what: str) -> int:
    # ``type(...) is int`` rather than isinstance: bool is an int subclass, and
    # ``sampleCount: true`` must not satisfy a count of 1.
    if type(value) is not int or value < 1:
        raise LigandAIUnreadableResponse(
            f"{what} must be a positive integer, got {value!r}",
            cause="invalid_patient_receipt", response=None,
        )
    return value


def _patient_path(org_slug: str, patient_slug: str, tail: str) -> str:
    return f"/api/patient-transcriptomics/{_patient_segments(org_slug, patient_slug)}/{tail}"


def _parse_patient_dataset_list(payload: Any) -> dict[str, Any]:
    """Full dict, including ``unavailable``. A dataset the server could not read
    is a fact about the patient's record, so it is returned, never dropped."""
    if not isinstance(payload, dict) or payload.get("success") is not True:
        raise LigandAIUnreadableResponse(
            "Patient dataset listing did not answer success:true.",
            cause="backend_negative_answer", response=payload,
        )
    for key in ("datasets", "unavailable"):
        if not isinstance(payload.get(key), list):
            raise LigandAIUnreadableResponse(
                f"Patient dataset listing is missing a list-valued '{key}' field; "
                "an absent field is not the same as an empty one.",
                cause="invalid_patient_receipt", response=payload,
            )
    for row in payload["datasets"]:
        if not isinstance(row, dict):
            raise LigandAIUnreadableResponse(
                "Patient dataset row is not an object.",
                cause="invalid_patient_receipt", response=payload,
            )
        # An imported GEO job is a different object with a different lifecycle.
        # Accepting one here would present an import job as a patient result.
        if "jobId" in row or row.get("source") == "durable":
            raise LigandAIUnreadableResponse(
                "Patient dataset listing returned an import job rather than a "
                "prepared patient dataset.",
                cause="import_jobs_not_patient_datasets", response=payload,
            )
        # A malformed id in a RESPONSE is a server problem, not caller input, so
        # it must not surface as the ValueError that _patient_uuid raises for a
        # bad argument. Same shape check, response-appropriate error class.
        rid = row.get("id")
        if not isinstance(rid, str) or _UUID_RE.match(rid) is None:
            raise LigandAIUnreadableResponse(
                "Patient dataset row carries a non-UUID id.",
                cause="invalid_patient_receipt", response=payload,
            )
        if row.get("observationUnit") != "bulk_sample":
            raise LigandAIUnreadableResponse(
                "Patient dataset is not observationUnit bulk_sample.",
                cause="invalid_patient_receipt", response=payload,
            )
        if row.get("visibility") != "private_patient":
            raise LigandAIUnreadableResponse(
                "Patient dataset is not visibility private_patient.",
                cause="invalid_patient_receipt", response=payload,
            )
        _require_positive_int(row.get("geneCount"), what="dataset geneCount")
        if (row.get("assay") != "bulk_rnaseq" or row.get("expressionUnit") != "tpm"
                or type(row.get("sampleCount")) is not int or row["sampleCount"] != 1
                or row.get("projectionScope") != "sample_profiles"
                or row.get("projectionExecution") != "recorded"):
            raise LigandAIUnreadableResponse(
                "Patient dataset units, assay, sample count or recorded scope are incompatible.",
                cause="invalid_patient_receipt", response=payload,
            )
        sha = row.get("contentSha256")
        if not isinstance(sha, str) or _SHA256_RE.match(sha) is None:
            raise LigandAIUnreadableResponse(
                "Patient dataset contentSha256 is not a lowercase sha256 hex digest.",
                cause="invalid_patient_receipt", response=payload,
            )
    for row in payload["unavailable"]:
        if not isinstance(row, dict) or "id" not in row or "error" not in row:
            raise LigandAIUnreadableResponse(
                "Patient dataset 'unavailable' entry lacks an id and error.",
                cause="invalid_patient_receipt", response=payload,
            )
    return payload


def _parse_patient_dataset_read(payload: Any, dataset_id: str,
                               include_expression: bool) -> dict[str, Any]:
    """Validates a recorded result. Nothing here performs inference; the SDK is
    checking that the server is describing the dataset that was asked for."""
    if not isinstance(payload, dict) or payload.get("success") is not True:
        raise LigandAIUnreadableResponse(
            "Patient dataset read did not answer success:true.",
            cause="backend_negative_answer", response=payload,
        )
    ds = payload.get("dataset")
    if not isinstance(ds, dict):
        raise LigandAIUnreadableResponse(
            "Patient dataset read has no dataset object.",
            cause="invalid_patient_receipt", response=payload,
        )
    if ds.get("id") != dataset_id:
        raise LigandAIUnreadableResponse(
            f"Patient dataset read returned id {ds.get('id')!r} for the requested "
            f"{dataset_id!r}; a response for a different dataset is not this one.",
            cause="patient_dataset_identity_mismatch", response=payload,
        )
    _parse_patient_dataset_list({"success": True, "datasets": [ds], "unavailable": []})
    if ds.get("observationUnit") != "bulk_sample":
        raise LigandAIUnreadableResponse(
            "Patient dataset is not observationUnit bulk_sample.",
            cause="invalid_patient_receipt", response=payload,
        )
    if ds.get("sampleCount") is not True and _require_positive_int(
            ds.get("sampleCount"), what="dataset sampleCount") != 1:
        raise LigandAIUnreadableResponse(
            "Patient dataset sampleCount must be exactly 1.",
            cause="invalid_patient_receipt", response=payload,
        )
    if type(ds.get("sampleCount")) is not int or ds.get("sampleCount") != 1:
        raise LigandAIUnreadableResponse(
            "Patient dataset sampleCount must be the integer 1, not a boolean.",
            cause="invalid_patient_receipt", response=payload,
        )
    if ds.get("projectionScope") != "sample_profiles":
        raise LigandAIUnreadableResponse(
            "Patient dataset projectionScope is not sample_profiles.",
            cause="invalid_patient_receipt", response=payload,
        )
    if ds.get("projectionExecution") != "recorded":
        raise LigandAIUnreadableResponse(
            "Patient dataset projectionExecution is not 'recorded'. This endpoint "
            "reads a projection that already happened; it never claims fresh "
            "model inference.",
            cause="invalid_patient_receipt", response=payload,
        )
    gene_count = _require_positive_int(ds.get("geneCount"), what="dataset geneCount")
    proj = payload.get("recordedProjection")
    if not isinstance(proj, dict):
        raise LigandAIUnreadableResponse(
            "Patient dataset read has no recordedProjection object.",
            cause="invalid_patient_receipt", response=payload,
        )
    if proj.get("execution") != "recorded" or proj.get("projectionScope") != "sample_profiles":
        raise LigandAIUnreadableResponse(
            "Projection must be a recorded sample profile.",
            cause="invalid_patient_receipt", response=payload,
        )
    if proj.get("sampleCount") != 1 or type(proj.get("sampleCount")) is not int:
        raise LigandAIUnreadableResponse(
            "recordedProjection.sampleCount must be the integer 1.",
            cause="invalid_patient_receipt", response=payload,
        )
    if include_expression:
        feats = payload.get("features")
        if not isinstance(feats, list):
            raise LigandAIUnreadableResponse(
                "include_expression was requested but the response carries no "
                "features list.", cause="invalid_patient_receipt", response=payload,
            )
        if len(feats) != gene_count:
            raise LigandAIUnreadableResponse(
                f"features holds {len(feats)} rows but the dataset declares "
                f"geneCount {gene_count}; a truncated feature list would silently "
                "shorten the patient's matrix.",
                cause="invalid_patient_receipt", response=payload,
            )
        seen: set[str] = set()
        for f in feats:
            if not isinstance(f, dict):
                raise LigandAIUnreadableResponse(
                    "Expression feature is not an object.",
                    cause="invalid_patient_receipt", response=payload,
                )
            gid = f.get("geneId")
            if not isinstance(gid, str) or not gid:
                raise LigandAIUnreadableResponse(
                    "Expression feature has no geneId.",
                    cause="invalid_patient_receipt", response=payload,
                )
            if gid in seen:
                raise LigandAIUnreadableResponse(
                    f"Duplicate geneId {gid!r} in expression features.",
                    cause="invalid_patient_receipt", response=payload,
                )
            seen.add(gid)
            tpm = f.get("tpm")
            # bool is an int subclass; True must not read as 1 TPM.
            if type(tpm) not in (int, float) or isinstance(tpm, bool) \
                    or tpm != tpm or tpm in (float("inf"), float("-inf")) or tpm < 0:
                raise LigandAIUnreadableResponse(
                    f"Feature {gid!r} has a non-finite, negative, boolean or "
                    "non-numeric tpm.",
                    cause="invalid_patient_receipt", response=payload,
                )
            # Zero tpm and duplicate SYMBOLS are both legitimate and preserved.
    return payload


def _validate_patient_profile(profile: Any) -> dict[str, Any]:
    """Client-side gate for the four fields that define what the bytes ARE.
    Units and assay are never rewritten or inferred: a matrix registered as
    something it is not would be read back as clinical fact."""
    if not isinstance(profile, dict):
        raise ValueError("profile must be an object")
    required = {
        "schema": "patient-prepared-transcriptomics/1",
        "assay": "bulk_rnaseq",
        "observationUnit": "bulk_sample",
        "expressionUnit": "tpm",
    }
    for field, expected in required.items():
        got = profile.get(field)
        if got != expected:
            raise ValueError(
                f"profile.{field} must be exactly {expected!r}, got {got!r}. "
                "This SDK will not relabel units or infer an assay."
            )
    if not isinstance(profile.get("features"), list) or not profile["features"]:
        raise ValueError("profile.features must be a non-empty list")
    if not isinstance(profile.get("sourceUploadIds"), list) or not profile["sourceUploadIds"]:
        raise ValueError("profile.sourceUploadIds must be a non-empty list of UUIDs")
    for sid in profile["sourceUploadIds"]:
        _patient_uuid(sid)
    return profile


def _parse_patient_register(payload: Any) -> dict[str, Any]:
    if not isinstance(payload, dict) or payload.get("success") is not True:
        raise LigandAIUnreadableResponse(
            "Patient dataset registration did not answer success:true. The write "
            "state is unknown; this SDK does not retry a registration, because a "
            "second accepted POST would create a second record.",
            cause="backend_negative_answer", response=payload,
        )
    rid = payload.get("id")
    if not isinstance(rid, str) or _UUID_RE.match(rid) is None:
        raise LigandAIUnreadableResponse(
            "Patient dataset registration returned a non-UUID id. The write may or "
            "may not have landed; this SDK does not guess and does not retry.",
            cause="invalid_patient_receipt", response=payload,
        )
    sha = payload.get("sha256")
    if not isinstance(sha, str) or _SHA256_RE.match(sha) is None:
        raise LigandAIUnreadableResponse(
            "Patient dataset registration returned no sha256 hex digest.",
            cause="invalid_patient_receipt", response=payload,
        )
    if not isinstance(payload.get("reused"), bool):
        raise LigandAIUnreadableResponse(
            "Patient dataset registration 'reused' must be a boolean.",
            cause="invalid_patient_receipt", response=payload,
        )
    if payload.get("visibility") != "private_patient":
        raise LigandAIUnreadableResponse(
            "Patient dataset registration is not visibility private_patient.",
            cause="invalid_patient_receipt", response=payload,
        )
    if payload.get("observationUnit") != "bulk_sample":
        raise LigandAIUnreadableResponse(
            "Patient dataset registration is not observationUnit bulk_sample.",
            cause="invalid_patient_receipt", response=payload,
        )
    if type(payload.get("sampleCount")) is not int or payload.get("sampleCount") != 1:
        raise LigandAIUnreadableResponse(
            "Patient dataset registration sampleCount must be the integer 1.",
            cause="invalid_patient_receipt", response=payload,
        )
    return payload


def _parse_hswae_receipt(
    payload: Any,
    expected_operation: HswaeCapabilityOperation,
    expected_input: Mapping[str, Any],
) -> HswaeCapabilityReceipt:
    receipt = cast(HswaeCapabilityReceipt, _HSWAE_RECEIPT_ADAPTER.validate_python(payload))
    if receipt.operation != expected_operation:
        raise ValueError("H-SWAE capability receipt operation mismatch")
    if isinstance(receipt, HswaeComplexSpecificityReceipt):
        expected_subunits = expected_input.get("subunits")
        if not isinstance(expected_subunits, list) or receipt.result.subunits != expected_subunits:
            raise ValueError("H-SWAE capability complex result does not match the request")
        expected_name = expected_input.get("complex_name")
        if expected_name is None:
            expected_name = "/".join(expected_subunits)
        if receipt.result.complex_name != expected_name:
            raise ValueError("H-SWAE capability complex result does not match the request")
        expected_limit = expected_input.get("limit", 50)
        if not isinstance(expected_limit, int) or len(receipt.result.contexts) > expected_limit:
            raise ValueError("H-SWAE capability complex result exceeds the request limit")
    return receipt


def _check_compare_group_shapes(
    target_group: Any,
    reference_groups: list[Any] | None,
    mode: str,
) -> None:
    """Fail locally, before any request, when the target/reference group
    shapes cannot be accepted by the live ``POST /api/transcriptomics/compare-groups``.

    Live server rules (routes.ts, verified 2026-11-05 against the deployed
    server during Tom's discovery report — his GUI testing hit raw 400s the
    SDK had advertised around):

    - ``mode='focus'``  → ``targetGroup.type`` MUST be ``custom`` (and needs
      ``datasetId`` + ``cellTypes``);
    - ``mode='global'`` / ``'compare'`` → ``targetGroup.type`` MUST be
      ``custom`` or ``geneset``;
    - GTEx tissues (``type='gtex'``) are accepted ONLY as REFERENCE groups in
      ``compare`` mode — never as the target. Earlier docstrings advertised a
      GTEx target ("compare against the named GTEx brain tissues" read like a
      target-slot promise); that was wrong and produced confusing server 400s.
      ``compare_bbb_vs_brain`` is correct: its target is a geneset.
    - ``mode='compare'`` requires at least one reference group.

    Raising here costs nothing and beats an opaque 400 with no field names.
    A 'tissue'/'cell_type' legacy target maps server-side only as its own
    legacy path; treat it as not-accepted in the target slot for these modes.
    [bd-LIGANDAI_ALPHA_V2-tom-D2]
    """
    ttype = getattr(target_group, "type", None)
    if ttype is None and isinstance(target_group, dict):
        ttype = target_group.get("type")
    if mode == "focus":
        if ttype != "custom":
            raise ValueError(
                "compare mode 'focus' requires a custom targetGroup "
                f"(type='custom' with dataset_id + cell_types); got type={ttype!r}. "
                "Live server rejects any other type for focus mode."
            )
    else:  # global / compare
        if ttype not in ("custom", "geneset"):
            raise ValueError(
                f"compare mode '{mode}' requires a custom or geneset targetGroup; "
                f"got type={ttype!r}. GTEx tissues are only supported as REFERENCE "
                "groups in 'compare' mode (set the target to a geneset of your "
                "genes, e.g. TargetGroup(name=..., type='geneset', genes=[...])). "
                "Live server rejects other target types."
            )
    if mode == "compare" and not reference_groups:
        raise ValueError(
            "compare mode 'compare' requires at least one referenceGroup "
            "(gtex tissues are valid references here)."
        )


def _split_shared_differential(markers: list[Any]) -> tuple[list[str], list[str]]:
    """Split comparison markers into SHARED vs DIFFERENTIAL gene lists.
    [bd-LIGANDAI_ALPHA_V2-k5usa]

    - differential: target-enriched (fold_change > 2) or target-exclusive, or
      specificity-ranked (gtex SI) markers with no reference TPM.
    - shared: expressed in target AND a reference (fold_change <= 2 with a
      non-trivial reference TPM).
    """
    shared: list[str] = []
    differential: list[str] = []
    for m in markers:
        gene = getattr(m, "gene", None)
        if not gene:
            continue
        fc = getattr(m, "fold_change", None)
        ref_tpm = getattr(m, "ref_tpm", None)
        if fc is not None:
            if fc > 2:
                differential.append(gene)
            elif ref_tpm is not None and ref_tpm > 1:
                shared.append(gene)
            else:
                differential.append(gene)
        else:
            # SI-method (gtex) markers are specificity-ranked → differential.
            differential.append(gene)
    return shared, differential


#: ``GET /api/transcriptomics/organ-systems`` answers
#: ``{"organSystems": [{"name", "tissues", ...}], "cellSubtypes": {...}, ...}``
#: — measured against blue 2026-10-02: 13 systems, while this module read
#: ``systems`` and reported none. [bd-dre-3imtv.3]
_ORGAN_SYSTEM_KEYS = ("organSystems", "organ_systems", "systems")


def _organ_system_names(rows: list) -> list[str]:
    """System NAMES out of the route's rows.

    The rows are objects (``name``, ``tissues``, ``tissueDisplayNames``,
    ``color``) and this method is typed ``list[str]`` — the name is what
    ``organ_system=`` filters take. [bd-dre-3imtv.3]
    """
    out: list[str] = []
    for row in rows:
        if isinstance(row, str):
            out.append(row)
        elif isinstance(row, dict):
            name = row.get("name", row.get("system", row.get("organSystem")))
            if isinstance(name, str):
                out.append(name)
    return out


class Discovery(Resource):
    """Target-discovery / transcriptomics funnel — the FIRST thing to reach for
    when the user asks "find targets", "which receptors are enriched in tissue /
    cell-type X", or "discover binders for disease Y".

    Backed by ``/api/transcriptomics/*``, ``/api/scrna/*``,
    ``/api/datasets/geo/*`` and ``/api/transport-vasculome/*``. The platform has
    already done the specificity-index (SI) ranking server-side over GTEx and
    its single-cell atlases — so you do NOT hand-stitch GTEx + CellGuide /
    CellxGene yourself. Resolve identifiers, ask for SI-ranked surface
    receptors, then chain the winning gene straight into design.

    Canonical funnel
    ----------------
    1. **Resolve identifiers first** — :meth:`tissues` and :meth:`organ_systems`
       enumerate the exact GTEx tissue / organ-system strings the markers
       endpoints expect (don't guess the spelling).
    2. **Rank surface receptors** — :meth:`tissue_markers` (GTEx bulk) or
       :meth:`cell_type_markers` (single-cell, Academia+) return SI-ranked
       genes; ``receptor_only=True`` (the default) is the cell-surface filter.
    3. **Differentiate / shuttle** — :meth:`compare_targets` surfaces SHARED vs
       DIFFERENTIAL genes across groups; :meth:`transport_vasculome` ranks
       blood-brain-barrier transcytosis shuttles.
    4. **Custom data** — :meth:`upload_dataset` (or :meth:`import_geo`) ingests
       your own counts, then pass ``custom_dataset_targets`` to
       :meth:`tissue_markers` to run the SAME SI ranking on it (routes to the
       ``analyze-fast`` endpoint).
    5. **Design** — take ``markers.top[0].gene`` and hand it to
       ``client.peptides.generate(gene=...)`` → fold → score.

    Every ranking call returns a :class:`~ligandai.types.MarkerResponse`; read
    ``response.top`` (a list of :class:`~ligandai.types.TissueMarker`, each with
    ``gene`` / ``si`` / ``receptor`` / ``rank``).
    """

    def _stamp_organism(
        self, body: dict[str, Any], organism: str | None, species: str | None
    ) -> None:
        """Fail-closed stamp of the effective ``organism``/``species`` on a body.

        Species selection is entitlement-gated: a non-entitled key can never
        force ``mouse`` — it is coerced to ``human`` locally so the SDK's own
        labels match the server. No-op when no parent client is attached.
        """
        if self._client is None:
            return
        self._client._apply_species(body, organism if organism is not None else species)

    def capabilities(self) -> HswaeCapabilityDiscovery:
        """Return the authenticated, deployment-filtered signed H-SWAE registry."""

        payload = self._transport.request("GET", _HSWAE_CAPABILITY_DISCOVERY_PATH)
        return HswaeCapabilityDiscovery.model_validate(payload)

    @overload
    def invoke_capability(
        self,
        operation: Literal["hswae.v1.semantic_search"],
        input: HswaeSemanticSearchInput,
    ) -> HswaeSemanticSearchReceipt: ...

    @overload
    def invoke_capability(
        self,
        operation: Literal["hswae.v1.surface_markers"],
        input: HswaeSurfaceMarkersInput,
    ) -> HswaeSurfaceMarkersReceipt: ...

    @overload
    def invoke_capability(
        self,
        operation: Literal["hswae.v2.resolve_cell_type"],
        input: HswaeResolveCellTypeInput,
    ) -> HswaeResolveCellTypeReceipt: ...

    @overload
    def invoke_capability(
        self,
        operation: Literal["hswae.v2.compare_cell_types"],
        input: HswaeCompareCellTypesInput,
    ) -> HswaeCompareCellTypesReceipt: ...

    @overload
    def invoke_capability(
        self,
        operation: Literal["hswae.v2.isoform_expression"],
        input: HswaeIsoformExpressionInput,
    ) -> HswaeIsoformExpressionReceipt: ...

    @overload
    def invoke_capability(
        self,
        operation: Literal["hswae.v2.annotate_dataset"],
        input: HswaeAnnotateDatasetInput,
    ) -> HswaeAnnotateDatasetReceipt: ...

    @overload
    def invoke_capability(
        self,
        operation: Literal["predictive_interactomics.complex_specificity"],
        input: HswaeComplexSpecificityInput,
    ) -> HswaeComplexSpecificityReceipt: ...

    def invoke_capability(
        self,
        operation: HswaeCapabilityOperation,
        input: Mapping[str, Any],
    ) -> HswaeCapabilityReceipt:
        """Invoke one signed immediate head with an exact closed input object.

        Authentication, tenant, entitlement, and request identity come only
        from the configured SDK transport. Caller input cannot override them.
        """

        clean_input = validate_hswae_capability_input(operation, input)
        payload = self._transport.request(
            "POST",
            _HSWAE_CAPABILITY_INVOKE_PATH,
            json={"operation": operation, "input": clean_input},
        )
        return _parse_hswae_receipt(payload, operation, clean_input)

    def tissue_markers(
        self,
        target_tissues: list[str] | None = None,
        custom_dataset_targets: list[CustomDatasetTarget | dict[str, Any]] | None = None,
        exclude_tissues: list[str] | None = None,
        top_n: int = 2000,
        receptor_only: bool = True,
        min_expression: float | None = None,
        organism: str | None = None,
        species: str | None = None,
    ) -> MarkerResponse:
        """Specificity-index (SI) ranked surface receptors enriched in a tissue
        — the workhorse of the discovery funnel. **Use this first** for "which
        receptors are enriched in <tissue>" / "give me surface targets for
        <organ>".

        Two server paths, picked automatically:

        - **GTEx bulk** (``target_tissues=[...]``) → ``/api/transcriptomics/
          top-markers``. Resolve the exact strings with :meth:`tissues` /
          :meth:`organ_systems` first.
        - **Custom data** (``custom_dataset_targets=[...]``) →
          ``/api/transcriptomics/analyze-fast``, running the SAME SI ranking on
          a dataset you uploaded with :meth:`upload_dataset` (or
          :meth:`import_geo`).

        :param target_tissues: GTEx tissue names to rank within (e.g.
            ``["Kidney - Cortex"]``). Mutually informative with — and may be
            combined against — ``exclude_tissues``.
        :param custom_dataset_targets: rank a user dataset instead of GTEx. Each
            entry is a :class:`~ligandai.types.CustomDatasetTarget` (or a plain
            dict with its aliases) — ``{"datasetId": <id>, "cellTypes": [...]}``;
            ``datasetId`` is the ``id`` returned by :meth:`upload_dataset`.
        :param exclude_tissues: tissues whose expression DEMOTES a gene's SI, so
            you surface targets that are selective for ``target_tissues`` and not
            broadly shared (off-target-prone).
        :param top_n: how many ranked rows to return (default 2000).
        :param receptor_only: **the cell-surface filter** — ``True`` (default)
            keeps only plasma-membrane receptors, the only druggable surface for
            a peptide binder. Set ``False`` only to inspect the full
            (incl. intracellular) ranking.
        :param min_expression: drop genes below this expression floor before
            ranking.
        :param organism: species atlas to rank within — ``"human"`` (default) or
            ``"mouse"``. Falls back to the client ``default_organism``; coerced to
            ``"human"`` when the key is not entitled (fail-closed), so a crafted
            ``organism="mouse"`` never selects the mouse atlas. "Flag mouse for
            mouse targets": the effective species routes the ranking to the
            matching atlas/ortholog namespace.
        :param species: alias for ``organism`` (server-parity). ``organism`` wins.
        :returns: :class:`~ligandai.types.MarkerResponse`; read ``.top`` —
            SI-ranked :class:`~ligandai.types.TissueMarker` rows
            (``.gene`` / ``.si`` / ``.receptor`` / ``.rank``). Feed
            ``resp.top[0].gene`` into ``client.peptides.generate(gene=...)``.

        Don't hand-stitch GTEx + CellGuide yourself — the SI ranking is already
        computed server-side here.
        """
        body: dict[str, object] = {
            "topN": top_n,
            "receptorOnly": receptor_only,
        }
        if target_tissues is not None:
            body["targetTissues"] = target_tissues
        if custom_dataset_targets is not None:
            body["customDatasetTargets"] = [
                t.model_dump(by_alias=True) if isinstance(t, CustomDatasetTarget) else t
                for t in custom_dataset_targets
            ]
        if exclude_tissues is not None:
            body["excludeTissues"] = exclude_tissues
        if min_expression is not None:
            body["minExpression"] = min_expression
        self._stamp_organism(body, organism, species)

        # Server has two endpoints: top-markers (GTEx) and analyze-fast (scRNA + custom).
        # When custom datasets provided, route to analyze-fast.
        path = (
            "/api/transcriptomics/analyze-fast"
            if custom_dataset_targets
            else "/api/transcriptomics/top-markers"
        )
        return MarkerResponse.model_validate(
            self._transport.request("POST", path, json=body) or {"top": []}
        )

    def cell_type_markers(
        self,
        scrna_tissue: str,
        target_cell_types: list[str],
        exclude_tissues: list[str] | None = None,
        top_n: int = 2000,
        receptor_only: bool = True,
        acceptable_offtargets: list[str] | None = None,
        organism: str | None = None,
        species: str | None = None,
    ) -> MarkerResponse:
        """Single-cell resolution of :meth:`tissue_markers` (Academia+ tier).
        SI-ranks surface receptors enriched in specific CELL TYPES within a
        scRNA tissue atlas — use when "enriched in tissue X" is too coarse and
        you need "enriched in proximal-tubule cells, not the whole kidney".

        :param scrna_tissue: the single-cell atlas / tissue to resolve within.
        :param target_cell_types: the cell types to rank markers for (e.g.
            ``["proximal_tubule", "podocyte"]``).
        :param exclude_tissues: tissues whose expression demotes a gene's SI, to
            keep cell-type-selective targets.
        :param top_n: ranked rows to return (default 2000).
        :param receptor_only: cell-surface filter; ``True`` (default) keeps only
            plasma-membrane receptors.
        :param acceptable_offtargets: tissues whose expression is tolerated, so a
            gene is not demoted for being expressed there. The route has always
            accepted this; it was simply never exposed.
        :param organism: accepted for call-shape parity but **not used by this
            route** — its handler reads six named fields and ``organism`` is not
            one of them, so species selection here is a no-op. Use
            :meth:`tissue_markers` when the atlas choice has to take effect.
        :returns: :class:`~ligandai.types.MarkerResponse`; read ``.top``.
        """
        # snake_case, NOT camelCase like tissue_markers above. The two routes
        # disagree and the body is forwarded verbatim, so the casing has to match
        # the one handler each method talks to:
        #   POST /api/transcriptomics/top-markers       destructures targetTissues,
        #                                               excludeTissues, topN, ...
        #   POST /api/transcriptomics/cell-type-markers destructures scrna_tissue,
        #                                               target_cell_types, top_n, ...
        # Sending camelCase here left scrna_tissue undefined, so every call was
        # refused 400 "Missing required parameters: scrna_tissue, target_cell_types
        # (array)" — measured 2026-10-22 against the live route with a cell type and
        # tissue both read back from the server. Verified against the handler in
        # server/routes.ts and the verbatim-forwarding alias in sdk-alias-routes.ts.
        body: dict[str, object] = {
            "scrna_tissue": scrna_tissue,
            "target_cell_types": target_cell_types,
            "top_n": top_n,
            "receptor_only": receptor_only,
        }
        if exclude_tissues is not None:
            body["exclude_tissues"] = exclude_tissues
        if acceptable_offtargets is not None:
            body["acceptable_offtargets"] = acceptable_offtargets
        # This route destructures six named fields and passes only those to its
        # script, so the organism stamp is dropped server-side: organism/species
        # select nothing here, unlike on tissue_markers. Kept for call-shape parity
        # and so a future handler that reads it needs no SDK change.
        self._stamp_organism(body, organism, species)
        return MarkerResponse.model_validate(
            self._transport.request("POST", "/api/scrna/cell-type-markers", json=body) or {"top": []}
        )

    def gene_expression(
        self,
        gene: str,
        *,
        organism: str | None = None,
        species: str | None = None,
    ) -> ExpressionProfile:
        """Tissue-level expression profile for a gene.

        :param organism: species atlas — ``"human"`` (default) or ``"mouse"``.
            Falls back to the client ``default_organism``; coerced to ``"human"``
            when the key is not entitled (fail-closed).
        :param species: alias for ``organism`` (server-parity). ``organism`` wins.
        """
        params: dict[str, Any] = {}
        if self._client is not None:
            self._client._apply_species(
                params, organism if organism is not None else species
            )
        payload = self._transport.request(
            "GET", f"/api/transcriptomics/gene-expression/{gene}", params=params or None
        ) or {"gene": gene}
        payload.setdefault("organism", params.get("organism", "human"))
        return ExpressionProfile.model_validate(payload)

    def isoform_expression(
        self,
        gene: str,
        top_n_isoforms: int = 10,
        min_mean_tpm: float = 0.5,
    ) -> dict[str, Any]:
        """Per-isoform expression for a gene across HSWAE-2 contexts.

        Enterprise / superadmin only — returns 403 with a friendly payload
        otherwise. Cell-type-resolved when cell_isoform_specificity is
        populated; falls back to tissue-resolved with `resolution` flag.

        Returns the raw HSWAE-2 response dict (not a Pydantic model — the
        shape is evolving as the cell_isoform_specificity populator rolls
        out).
        """
        body = {
            "gene": gene,
            "top_n_isoforms": top_n_isoforms,
            "min_mean_tpm": min_mean_tpm,
        }
        return self._transport.request(
            "POST", "/api/transcriptomics/hswae/isoform-expression", json=body
        ) or {"gene": gene, "isoforms": [], "resolution": "none"}

    def get_hswae2_cell_type_markers(
        self,
        cell_type: str,
        top_n: int = 25,
        reference_cell_types: list[str] | None = None,
    ) -> dict[str, Any]:
        """HSWAE-2 surface markers ranked for a cell type (enterprise tier).

        Resolves ``cell_type`` through the HSWAE-2 specialized-alias table
        (e.g. 'HFSC' -> 'basal keratinocytes') and returns the differential
        surface-marker ranking (CSI) against the given reference cell types.
        [bd-LIGANDAI_ALPHA_V2-kg2mi]

        Args:
            cell_type: Canonical HSWAE-2 cell type or a specialized alias.
            top_n: Number of markers to return.
            reference_cell_types: Optional comparator set; empty returns the
                target's own top markers.

        Returns:
            Dict with the differential marker ranking and proxy metadata.
            403 (LigandAIForbidden) if not enterprise tier.
        """
        from urllib.parse import quote

        body = {
            "top_n": top_n,
            "reference_cell_types": reference_cell_types or [],
        }
        return self._transport.request(
            "POST",
            f"/api/transcriptomics/hswae2/cell-type-markers/{quote(cell_type)}",
            json=body,
        ) or {}

    def resolve_hswae2_cell_type(self, term: str) -> dict[str, Any]:
        """Resolve a user-facing cell-type term to a canonical HSWAE-2 cell type.

        Hybrid exact / alias / fuzzy / marker-overlap resolution with proxy
        metadata (enterprise tier). [bd-LIGANDAI_ALPHA_V2-kg2mi]

        Args:
            term: User-facing cell-type term, e.g. 'HFSC' or 'sebocyte'.

        Returns:
            Dict with canonical cell type, confidence, candidates, and any
            proxy note. 403 (LigandAIForbidden) if not enterprise tier.
        """
        from urllib.parse import quote

        return self._transport.request(
            "GET", f"/api/transcriptomics/hswae2/resolve-cell-type/{quote(term)}"
        ) or {}

    def compare_groups(
        self,
        target_group: TargetGroup,
        reference_groups: list[ReferenceGroup] | None = None,
        mode: Literal["focus", "global", "compare"] = "compare",
        receptor_only: bool = False,
        top_n: int = 100,
    ) -> ComparisonResponse:
        """POST ``/api/transcriptomics/compare-groups`` verbatim.

        Group-shape rules the live server enforces (and this call now checks
        locally first — see :func:`_check_compare_group_shapes`):
        ``focus`` needs a ``custom`` target; ``global``/``compare`` need a
        ``custom`` or ``geneset`` target; ``gtex`` tissues are only valid as
        reference groups in ``compare`` mode, and ``compare`` needs >=1 of them.
        [bd-LIGANDAI_ALPHA_V2-tom-D2]
        """
        _check_compare_group_shapes(target_group, reference_groups, mode)
        body: dict[str, object] = {
            "targetGroup": target_group.model_dump(by_alias=True),
            "mode": mode,
            "receptorOnly": receptor_only,
            "topN": top_n,
        }
        if reference_groups is not None:
            body["referenceGroups"] = [g.model_dump(by_alias=True) for g in reference_groups]
        return ComparisonResponse.model_validate(
            self._transport.request("POST", "/api/transcriptomics/compare-groups", json=body)
            or {"targetGroup": target_group.name, "referenceGroups": [], "mode": mode, "results": []}
        )

    def search_geo(self, query: str, *, limit: int = 10) -> list[GeoDataset]:
        """Search public GEO metadata; preserve complete fields on each result.

        Use :meth:`search_geo_response` for envelope warnings and source metadata,
        including when no datasets matched. This call does not import datasets.
        """
        response = self.search_geo_response(query, limit=limit)
        return response.results

    def search_geo_response(self, query: str, *, limit: int = 10) -> GeoSearchResponse:
        """Return the complete public GEO search envelope from the canonical API."""
        payload = self._transport.request(
            "GET", f"{GEO_PATH}/search", params=geo_search_params(query, limit),
        )
        return parse_geo_search(payload)

    def inspect_geo(self, accession: str) -> GeoInspection:
        """Inspect official sample metadata and supported formats without importing.

        Parser readiness is not evidence that expression values were downloaded,
        parsed, normalized or analyzed. Read ``warnings`` and ``metadata_coverage``.
        """
        accession = geo_accession(accession)
        payload = self._transport.request("GET", f"{GEO_PATH}/inspect/{accession}")
        return parse_geo_inspection(payload, accession)

    def import_geo(self, accession: str, *, force: bool = False, selection: dict[str, Any] | None = None) -> GeoImportJob:
        """Explicitly start an owner-scoped import; a queued receipt is not completion.

        ``force=True`` asks the server to re-import an already imported accession.
        ``selection`` preserves exact published file names and an explicit parse plan.
        Archive-relative paths are resolved by the server; no local path or URL is uploaded.
        """
        accession = geo_accession(accession)
        if type(force) is not bool:
            raise ValueError("force must be a boolean")
        selection = geo_import_selection(selection)
        body: dict[str, Any] = {"accession": accession, "force": force}
        if selection is not None:
            body["selection"] = selection
        payload = self._transport.request(
            "POST", f"{GEO_PATH}/import", json=body,
        )
        return parse_geo_job(payload, accession=accession)

    def import_status(self, job_id: str, *, accession: str | None = None) -> GeoImportJob:
        """Fetch the actual import receipt, with optional owner-scoped restart fallback."""
        job_id = geo_job_id(job_id)
        accession = geo_accession(accession) if accession is not None else None
        payload = self._transport.request(
            "GET", f"{GEO_PATH}/status/{job_id}",
            params={"accession": accession} if accession else None,
        )
        return parse_geo_job(payload, job_id=job_id, accession=accession)

    def wait_for_import(
        self, job_id: str, *, accession: str | None = None,
        timeout: float = 300, poll_interval: float = 2,
    ) -> GeoImportJob:
        """Poll until a completed/failed/cancelled receipt or a finite deadline.

        Failed receipts are returned intact. ``TimeoutError`` includes ``last_job``
        when a server receipt was observed. The monotonic deadline caps sleeps and
        each request timeout; an in-flight transport retry can extend wall time.
        No further poll starts after the deadline. This never submits an import.
        """
        job_id = geo_job_id(job_id)
        accession = geo_accession(accession) if accession is not None else None
        geo_poll_parameters(timeout, poll_interval)
        deadline = time.monotonic() + timeout
        last_job: GeoImportJob | None = None
        while True:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise GeoImportTimeout(job_id, last_job)
            payload = self._transport.request(
                "GET", f"{GEO_PATH}/status/{job_id}",
                params={"accession": accession} if accession else None,
                timeout=remaining,
            )
            last_job = parse_geo_job(payload, job_id=job_id, accession=accession)
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise GeoImportTimeout(job_id, last_job)
            if last_job.status in GEO_TERMINAL_STATUSES:
                return last_job
            time.sleep(min(poll_interval, remaining))

    def list_datasets(self) -> list[Dataset]:
        payload = self._transport.request("GET", "/api/transcriptomics/datasets")
        return _parse_dataset_list(payload)

    def project_dataset(
        self, dataset_id: int, *,
        projection_scope: Literal["cell_types", "sample_profiles"] = "cell_types",
        group_ids: list[int] | None = None, top_k: int = 5, persist: bool = False,
    ) -> dict[str, Any]:
        """Project an already prepared account dataset with explicit group semantics.

        ``sample_profiles`` requires a backend supporting documented bulk TPM
        profiles; an older server or an unqualified import is rejected. It does
        not identify individual cancer cells, assign tumor origin or perform
        FASTQ quantification. ``group_ids`` are exact dataset group-table IDs.
        ``persist=False`` leaves the dataset readiness flag unchanged.
        """
        body = _dataset_projection_request(dataset_id, projection_scope, group_ids, top_k, persist)
        payload = self._transport.request("POST", "/api/transcriptomics/hswae-project", json=body)
        return _parse_dataset_projection(payload, dataset_id, projection_scope)

    def upload_dataset(self, file: Path | str, dataset_type: str) -> Dataset:
        """Upload an expression file and return its acceptance receipt.

        Acceptance does not establish completed processing, verified expression
        units or eligibility for H-SWAE/marker analysis. Raw FASTQ requires a
        separate quantification stage. Bulk TPM and single-cell count matrices
        need their respective supported backend preparation contracts.

        :param file: expression matrix (``.h5ad`` / ``.csv`` / ``.tsv``).
        :param dataset_type: the kind of data — ``"bulk"`` for bulk RNA-seq,
            ``"scrna"`` / ``"scRNA-seq"`` for single-cell, ``"microarray"``.
        :returns: a :class:`~ligandai.types.Dataset` with the server's status.
            Use :meth:`list_datasets` to inspect account datasets. GEO import
            polling is a separate job interface and is not an upload-status API.
        """
        path = Path(file)
        with path.open("rb") as f:
            files = {"file": (path.name, f)}
            data = {"datasetType": dataset_type}
            payload = self._transport.request(
                "POST", "/api/transcriptomics/upload", data=data, files=files
            ) or {}
        return Dataset.model_validate(payload)

    def list_patient_datasets(self, org_slug: str, patient_slug: str) -> dict[str, Any]:
        """List prepared private transcriptomics datasets for one patient.

        Returns the server's full body, including the ``unavailable`` list. An
        entry the server could not decrypt or parse is reported, not dropped:
        for a person's record, "this one could not be read" is information.
        Raises rather than returning an empty list when the answer is unreadable.
        """
        path = _patient_path(org_slug, patient_slug, "datasets")
        payload = self._transport.request("GET", path)
        return _parse_patient_dataset_list(payload)

    def patient_dataset(
        self, org_slug: str, patient_slug: str, dataset_id: str, *,
        include_expression: bool = False,
    ) -> dict[str, Any]:
        """Read one prepared private dataset, and optionally its expression features.

        This returns a **recorded** projection. It does not run a model and must
        not be described as fresh inference. ``include_expression=True`` requires
        the response to carry exactly ``geneCount`` features with unique gene ids
        and finite non-negative numeric ``tpm``; duplicate gene symbols and zero
        values are legitimate and are preserved.
        """
        if type(include_expression) is not bool:
            raise ValueError("include_expression must be boolean")
        seg = _patient_path(org_slug, patient_slug, f"datasets/{_patient_uuid(dataset_id)}")
        path = f"{seg}?includeExpression={'true' if include_expression else 'false'}"
        payload = self._transport.request("GET", path)
        return _parse_patient_dataset_read(payload, dataset_id, include_expression)

    def register_patient_dataset(
        self, org_slug: str, patient_slug: str, profile: dict[str, Any],
    ) -> dict[str, Any]:
        """Register a prepared private transcriptomics profile. Superadmin only.

        The four identity fields (``schema``, ``assay``, ``observationUnit``,
        ``expressionUnit``) are checked client-side and never rewritten. A
        registration whose response cannot be read raises and is **not** retried:
        this is a write, and a blind retry can create a second record.
        """
        _validate_patient_profile(profile)
        path = _patient_path(org_slug, patient_slug, "datasets")
        payload = self._transport.request("POST", path, json=profile)
        return _parse_patient_register(payload)

    def delete_dataset(self, dataset_id: str | int) -> bool:
        try:
            self._transport.request("DELETE", f"/api/transcriptomics/datasets/{dataset_id}")
            return True
        except LigandAILegalAcceptanceRequired:
            # ⛔ A legal 403 must not read as "server refused".
            raise
        except Exception as exc:
            # ⛔ `False` here must mean ONLY 'the server was reached and
            # declined'. Every other cause — no connection, bad key, CDN
            # block, 5xx — leaves the outcome UNKNOWN and now raises,
            # because a caller acting on a fabricated 'it did not happen'
            # is worse than one handling an error. [bd-dre-rp11j.3]
            resolve_mutation_failure(exc, operation="discovery.delete_dataset", target=str(dataset_id))
            return False

    def transport_vasculome(
        self,
        modality: Literal["monovalent", "multivalent", "both"],
        min_score: float = 0.0,
        limit: int = 50,
        include_risks: bool = False,
        specificity_weight: float = 0.0,
    ) -> list[BBBReceptor]:
        """Funnel step 3 (BBB shuttle ranking, Enterprise-only): rank
        blood-brain-barrier transcytosis receptors you can hijack to ferry a
        binder/payload into the CNS. **Use this** for "which receptor shuttles
        cross the BBB" / CNS-delivery target selection — it is the discovery
        entry point for brain-penetrant design.

        :param specificity_weight: 0..1 BBB-specificity lever
            [bd-LIGANDAI_ALPHA_V2-k5usa]. ``0`` (default) ranks purely by
            transport suitability (unchanged behaviour). ``> 0`` blends the
            transport score with the GTEx BBB-specificity index and re-ranks, so
            broadly-shared (off-target-prone) shuttles are demoted in favour of
            brain-selective ones. Each returned :class:`~ligandai.types.BBBReceptor`
            then carries ``specificity_index``, ``top_peripheral_tissues``,
            ``broadly_shared`` and ``combined_score``.
        """
        if self._client is not None:
            self._client._require_feature("transport_vasculome")
        body = {
            "modality": modality,
            "minScore": min_score,
            "limit": limit,
            "includeRisks": include_risks,
            "specificityWeight": specificity_weight,
        }
        payload = self._transport.request(
            "POST", "/api/transport-vasculome/query", json=body
        ) or []
        items = (
            payload
            if isinstance(payload, list)
            else payload.get("receptors", payload.get("results", []))
        )
        return [BBBReceptor.model_validate(r) for r in items]

    def compare_targets(
        self,
        groups: list[TargetGroup | ReferenceGroup | dict[str, Any]],
        mode: Literal["focus", "global", "compare"] = "compare",
        receptor_only: bool = False,
        top_n: int = 100,
    ) -> ComparisonResponse:
        """Funnel step 3 (differential ranking): compare 2+ desired target
        groups and surface SHARED vs DIFFERENTIAL genes plus specificity.
        [bd-LIGANDAI_ALPHA_V2-k5usa]

        **Use this** when a single-tissue :meth:`tissue_markers` ranking isn't
        enough and you need targets that are PRESENT in your tissue/cell-type
        but ABSENT (or much lower) in a reference — i.e. selectivity, not just
        abundance.

        ``groups[0]`` is the target; ``groups[1:]`` are references. The live
        server accepts the TARGET as a custom dataset (``type='custom'`` +
        ``dataset_id`` / ``cell_types``) or a gene/receptor set
        (``type='geneset'`` + ``genes``), e.g. a BBB transcytosis shuttle set
        or a pathway — in ``focus`` mode the target must be ``custom``.
        GTEx tissues (``type='gtex'`` + ``tissue``) are valid only as
        REFERENCE groups in ``compare`` mode, never as the target; the SDK now
        raises ``ValueError`` locally with this guidance instead of passing the
        server's bare 400 through. This is what lets you compare
        "BBB-shuttle set vs brain-cortex targets" or "pathway A vs pathway B"
        in ONE call, and lets a user see which BBB shuttles are brain-selective
        vs broadly shared. [bd-LIGANDAI_ALPHA_V2-tom-D2]

        The returned :class:`~ligandai.types.ComparisonResponse` is annotated with
        ``shared_genes`` (expressed in target AND a reference) and
        ``differential_genes`` (target-enriched / target-exclusive).
        """
        if not groups:
            raise ValueError("compare_targets requires at least one group (groups[0]=target)")
        norm = [g if isinstance(g, (TargetGroup, ReferenceGroup)) else TargetGroup.model_validate(g) for g in groups]
        target = TargetGroup.model_validate(norm[0].model_dump(by_alias=True))
        references = [ReferenceGroup.model_validate(g.model_dump(by_alias=True)) for g in norm[1:]]
        resp = self.compare_groups(
            target_group=target,
            reference_groups=references or None,
            mode=mode,
            receptor_only=receptor_only,
            top_n=top_n,
        )
        shared, differential = _split_shared_differential(resp.results)
        resp.shared_genes = shared
        resp.differential_genes = differential
        return resp

    def compare_bbb_vs_brain(
        self,
        bbb_modality: Literal["monovalent", "multivalent", "both"] = "monovalent",
        brain_tissues: list[str] | None = None,
        bbb_limit: int = 25,
        specificity_weight: float = 0.0,
        receptor_only: bool = False,
        top_n: int = 100,
    ) -> ComparisonResponse:
        """Convenience recipe: compare the BBB transcytosis shuttle set against
        brain-parenchyma targets. [bd-LIGANDAI_ALPHA_V2-k5usa]

        Builds a ``geneset`` target group from the top BBB transcytosis receptors
        (via :meth:`transport_vasculome`) and compares it against the named GTEx
        brain tissues — so a user sees which BBB shuttles are brain-selective vs
        broadly shared, and which brain targets are co-expressed at the BBB.
        """
        brain_tissues = brain_tissues or ["brain_cortex"]
        shuttles = self.transport_vasculome(
            modality=bbb_modality,
            limit=bbb_limit,
            specificity_weight=specificity_weight,
        )
        shuttle_genes = [r.gene for r in shuttles if r.gene]
        target = TargetGroup(
            name="BBB transcytosis shuttles",
            type="geneset",
            genes=shuttle_genes,
        )
        references = [
            ReferenceGroup(name=t, type="gtex", tissue=t) for t in brain_tissues
        ]
        return self.compare_targets(
            [target, *references],
            mode="compare",
            receptor_only=receptor_only,
            top_n=top_n,
        )

    def tissues(self) -> list[str]:
        payload = self._transport.request("GET", "/api/transcriptomics/tissues")
        return list(
            unwrap_rows(payload, ("tissues",), what="GET /api/transcriptomics/tissues")
        )

    def organ_systems(self) -> list[str]:
        payload = self._transport.request("GET", "/api/transcriptomics/organ-systems")
        return _organ_system_names(
            unwrap_rows(
                payload,
                _ORGAN_SYSTEM_KEYS,
                what="GET /api/transcriptomics/organ-systems",
            )
        )


class AsyncDiscovery(AsyncResource):
    """Async target-discovery / transcriptomics funnel. Same surface and
    canonical funnel as :class:`Discovery` (resolve identifiers → SI-rank
    surface receptors → differentiate / shuttle → design); see that class for
    the full walkthrough. The platform does the SI ranking server-side — don't
    hand-stitch GTEx + CellGuide.
    """

    def _stamp_organism(
        self, body: dict[str, Any], organism: str | None, species: str | None
    ) -> None:
        """Fail-closed stamp of the effective ``organism``/``species`` on a body.

        See :meth:`Discovery._stamp_organism`. No-op with no parent client.
        """
        if self._client is None:
            return
        self._client._apply_species(body, organism if organism is not None else species)

    async def capabilities(self) -> HswaeCapabilityDiscovery:
        """Async counterpart of :meth:`Discovery.capabilities`."""

        payload = await self._transport.request("GET", _HSWAE_CAPABILITY_DISCOVERY_PATH)
        return HswaeCapabilityDiscovery.model_validate(payload)

    @overload
    async def invoke_capability(
        self,
        operation: Literal["hswae.v1.semantic_search"],
        input: HswaeSemanticSearchInput,
    ) -> HswaeSemanticSearchReceipt: ...

    @overload
    async def invoke_capability(
        self,
        operation: Literal["hswae.v1.surface_markers"],
        input: HswaeSurfaceMarkersInput,
    ) -> HswaeSurfaceMarkersReceipt: ...

    @overload
    async def invoke_capability(
        self,
        operation: Literal["hswae.v2.resolve_cell_type"],
        input: HswaeResolveCellTypeInput,
    ) -> HswaeResolveCellTypeReceipt: ...

    @overload
    async def invoke_capability(
        self,
        operation: Literal["hswae.v2.compare_cell_types"],
        input: HswaeCompareCellTypesInput,
    ) -> HswaeCompareCellTypesReceipt: ...

    @overload
    async def invoke_capability(
        self,
        operation: Literal["hswae.v2.isoform_expression"],
        input: HswaeIsoformExpressionInput,
    ) -> HswaeIsoformExpressionReceipt: ...

    @overload
    async def invoke_capability(
        self,
        operation: Literal["hswae.v2.annotate_dataset"],
        input: HswaeAnnotateDatasetInput,
    ) -> HswaeAnnotateDatasetReceipt: ...

    @overload
    async def invoke_capability(
        self,
        operation: Literal["predictive_interactomics.complex_specificity"],
        input: HswaeComplexSpecificityInput,
    ) -> HswaeComplexSpecificityReceipt: ...

    async def invoke_capability(
        self,
        operation: HswaeCapabilityOperation,
        input: Mapping[str, Any],
    ) -> HswaeCapabilityReceipt:
        """Async counterpart of :meth:`Discovery.invoke_capability`."""

        clean_input = validate_hswae_capability_input(operation, input)
        payload = await self._transport.request(
            "POST",
            _HSWAE_CAPABILITY_INVOKE_PATH,
            json={"operation": operation, "input": clean_input},
        )
        return _parse_hswae_receipt(payload, operation, clean_input)

    async def tissue_markers(
        self,
        target_tissues: list[str] | None = None,
        custom_dataset_targets: list[CustomDatasetTarget | dict[str, Any]] | None = None,
        exclude_tissues: list[str] | None = None,
        top_n: int = 2000,
        receptor_only: bool = True,
        min_expression: float | None = None,
        organism: str | None = None,
        species: str | None = None,
    ) -> MarkerResponse:
        """Async SI-ranked surface receptors enriched in a tissue — the
        workhorse of the funnel; **use first** for target discovery. GTEx bulk
        via ``target_tissues``, or your own data via ``custom_dataset_targets``
        (``[{"datasetId": <id>, "cellTypes": [...]}]``, routes to
        ``analyze-fast``). ``receptor_only=True`` (default) is the cell-surface
        filter. ``organism`` (``"human"`` default / ``"mouse"``) selects the
        atlas, fail-closed to human when not entitled; ``species`` is an alias.
        Read ``.top`` → feed ``.top[0].gene`` to ``peptides.generate``. See
        :meth:`Discovery.tissue_markers`.
        """
        body: dict[str, object] = {
            "topN": top_n,
            "receptorOnly": receptor_only,
        }
        if target_tissues is not None:
            body["targetTissues"] = target_tissues
        if custom_dataset_targets is not None:
            body["customDatasetTargets"] = [
                t.model_dump(by_alias=True) if isinstance(t, CustomDatasetTarget) else t
                for t in custom_dataset_targets
            ]
        if exclude_tissues is not None:
            body["excludeTissues"] = exclude_tissues
        if min_expression is not None:
            body["minExpression"] = min_expression
        self._stamp_organism(body, organism, species)
        path = (
            "/api/transcriptomics/analyze-fast"
            if custom_dataset_targets
            else "/api/transcriptomics/top-markers"
        )
        return MarkerResponse.model_validate(
            await self._transport.request("POST", path, json=body) or {"top": []}
        )

    async def cell_type_markers(
        self,
        scrna_tissue: str,
        target_cell_types: list[str],
        exclude_tissues: list[str] | None = None,
        top_n: int = 2000,
        receptor_only: bool = True,
        acceptable_offtargets: list[str] | None = None,
        organism: str | None = None,
        species: str | None = None,
    ) -> MarkerResponse:
        """Async single-cell resolution of :meth:`tissue_markers` (Academia+):
        SI-ranks surface receptors enriched in specific CELL TYPES within an
        scRNA atlas. ``receptor_only=True`` (default) is the surface filter;
        ``organism`` (``"human"`` default / ``"mouse"``) selects the atlas,
        fail-closed to human when not entitled (``species`` is an alias). Read
        ``.top``. See :meth:`Discovery.cell_type_markers`.
        """
        # snake_case, for the same measured reason as the sync method: this route
        # destructures scrna_tissue/target_cell_types/top_n, while top-markers
        # destructures camelCase. See Discovery.cell_type_markers.
        body: dict[str, object] = {
            "scrna_tissue": scrna_tissue,
            "target_cell_types": target_cell_types,
            "top_n": top_n,
            "receptor_only": receptor_only,
        }
        if exclude_tissues is not None:
            body["exclude_tissues"] = exclude_tissues
        if acceptable_offtargets is not None:
            body["acceptable_offtargets"] = acceptable_offtargets
        self._stamp_organism(body, organism, species)
        return MarkerResponse.model_validate(
            await self._transport.request("POST", "/api/scrna/cell-type-markers", json=body)
            or {"top": []}
        )

    async def gene_expression(
        self,
        gene: str,
        *,
        organism: str | None = None,
        species: str | None = None,
    ) -> ExpressionProfile:
        """Async tissue-level expression profile. ``organism`` (``"human"``
        default / ``"mouse"``) selects the atlas, fail-closed to human when not
        entitled; ``species`` is an alias. See :meth:`Discovery.gene_expression`.
        """
        params: dict[str, Any] = {}
        if self._client is not None:
            self._client._apply_species(
                params, organism if organism is not None else species
            )
        payload = await self._transport.request(
            "GET", f"/api/transcriptomics/gene-expression/{gene}", params=params or None
        ) or {"gene": gene}
        payload.setdefault("organism", params.get("organism", "human"))
        return ExpressionProfile.model_validate(payload)

    async def isoform_expression(
        self,
        gene: str,
        top_n_isoforms: int = 10,
        min_mean_tpm: float = 0.5,
    ) -> dict[str, Any]:
        """Async parity for :meth:`Discovery.isoform_expression`."""

        body = {
            "gene": gene,
            "top_n_isoforms": top_n_isoforms,
            "min_mean_tpm": min_mean_tpm,
        }
        return await self._transport.request(
            "POST",
            "/api/transcriptomics/hswae/isoform-expression",
            json=body,
        ) or {"gene": gene, "isoforms": [], "resolution": "none"}

    async def get_hswae2_cell_type_markers(
        self,
        cell_type: str,
        top_n: int = 25,
        reference_cell_types: list[str] | None = None,
    ) -> dict[str, Any]:
        """HSWAE-2 surface markers ranked for a cell type (enterprise tier).
        [bd-LIGANDAI_ALPHA_V2-kg2mi]
        """
        from urllib.parse import quote

        body = {
            "top_n": top_n,
            "reference_cell_types": reference_cell_types or [],
        }
        return await self._transport.request(
            "POST",
            f"/api/transcriptomics/hswae2/cell-type-markers/{quote(cell_type)}",
            json=body,
        ) or {}

    async def resolve_hswae2_cell_type(self, term: str) -> dict[str, Any]:
        """Resolve a user-facing cell-type term to a canonical HSWAE-2 cell type
        (enterprise tier). [bd-LIGANDAI_ALPHA_V2-kg2mi]
        """
        from urllib.parse import quote

        return await self._transport.request(
            "GET", f"/api/transcriptomics/hswae2/resolve-cell-type/{quote(term)}"
        ) or {}

    async def compare_groups(
        self,
        target_group: TargetGroup,
        reference_groups: list[ReferenceGroup] | None = None,
        mode: Literal["focus", "global", "compare"] = "compare",
        receptor_only: bool = False,
        top_n: int = 100,
    ) -> ComparisonResponse:
        """Async twin of :meth:`Discovery.compare_groups`; same local
        group-shape gate (:func:`_check_compare_group_shapes`).
        [bd-LIGANDAI_ALPHA_V2-tom-D2]
        """
        _check_compare_group_shapes(target_group, reference_groups, mode)
        body: dict[str, object] = {
            "targetGroup": target_group.model_dump(by_alias=True),
            "mode": mode,
            "receptorOnly": receptor_only,
            "topN": top_n,
        }
        if reference_groups is not None:
            body["referenceGroups"] = [g.model_dump(by_alias=True) for g in reference_groups]
        return ComparisonResponse.model_validate(
            await self._transport.request("POST", "/api/transcriptomics/compare-groups", json=body)
            or {"targetGroup": target_group.name, "referenceGroups": [], "mode": mode, "results": []}
        )

    async def search_geo(self, query: str, *, limit: int = 10) -> list[GeoDataset]:
        """Search public GEO metadata; preserve complete fields on each result.

        Use :meth:`search_geo_response` for envelope warnings and source metadata,
        including when no datasets matched. This call does not import datasets.
        """
        response = await self.search_geo_response(query, limit=limit)
        return response.results

    async def search_geo_response(self, query: str, *, limit: int = 10) -> GeoSearchResponse:
        """Return the complete public GEO search envelope from the canonical API."""
        payload = await self._transport.request(
            "GET", f"{GEO_PATH}/search", params=geo_search_params(query, limit),
        )
        return parse_geo_search(payload)

    async def inspect_geo(self, accession: str) -> GeoInspection:
        """Inspect official sample metadata and supported formats without importing.

        Parser readiness is not evidence that expression values were downloaded,
        parsed, normalized or analyzed. Read ``warnings`` and ``metadata_coverage``.
        """
        accession = geo_accession(accession)
        payload = await self._transport.request("GET", f"{GEO_PATH}/inspect/{accession}")
        return parse_geo_inspection(payload, accession)

    async def import_geo(self, accession: str, *, force: bool = False, selection: dict[str, Any] | None = None) -> GeoImportJob:
        """Explicitly start an owner-scoped import; a queued receipt is not completion.

        ``force=True`` asks the server to re-import an already imported accession.
        ``selection`` preserves exact published file names and an explicit parse plan.
        Archive-relative paths are resolved by the server; no local path or URL is uploaded.
        """
        accession = geo_accession(accession)
        if type(force) is not bool:
            raise ValueError("force must be a boolean")
        selection = geo_import_selection(selection)
        body: dict[str, Any] = {"accession": accession, "force": force}
        if selection is not None:
            body["selection"] = selection
        payload = await self._transport.request(
            "POST", f"{GEO_PATH}/import", json=body,
        )
        return parse_geo_job(payload, accession=accession)

    async def import_status(self, job_id: str, *, accession: str | None = None) -> GeoImportJob:
        """Fetch the actual import receipt, with optional owner-scoped restart fallback."""
        job_id = geo_job_id(job_id)
        accession = geo_accession(accession) if accession is not None else None
        payload = await self._transport.request(
            "GET", f"{GEO_PATH}/status/{job_id}",
            params={"accession": accession} if accession else None,
        )
        return parse_geo_job(payload, job_id=job_id, accession=accession)

    async def wait_for_import(
        self, job_id: str, *, accession: str | None = None,
        timeout: float = 300, poll_interval: float = 2,
    ) -> GeoImportJob:
        """Poll until a completed/failed/cancelled receipt or a finite deadline.

        Failed receipts are returned intact. ``TimeoutError`` includes ``last_job``
        when a server receipt was observed. The monotonic deadline caps sleeps and
        each request timeout; an in-flight transport retry can extend wall time.
        No further poll starts after the deadline. This never submits an import.
        """
        job_id = geo_job_id(job_id)
        accession = geo_accession(accession) if accession is not None else None
        geo_poll_parameters(timeout, poll_interval)
        deadline = time.monotonic() + timeout
        last_job: GeoImportJob | None = None
        while True:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise GeoImportTimeout(job_id, last_job)
            payload = await self._transport.request(
                "GET", f"{GEO_PATH}/status/{job_id}",
                params={"accession": accession} if accession else None,
                timeout=remaining,
            )
            last_job = parse_geo_job(payload, job_id=job_id, accession=accession)
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise GeoImportTimeout(job_id, last_job)
            if last_job.status in GEO_TERMINAL_STATUSES:
                return last_job
            await asyncio.sleep(min(poll_interval, remaining))

    async def list_datasets(self) -> list[Dataset]:
        payload = await self._transport.request("GET", "/api/transcriptomics/datasets")
        return _parse_dataset_list(payload)

    async def project_dataset(
        self, dataset_id: int, *,
        projection_scope: Literal["cell_types", "sample_profiles"] = "cell_types",
        group_ids: list[int] | None = None, top_k: int = 5, persist: bool = False,
    ) -> dict[str, Any]:
        """Async equivalent of Discovery.project_dataset with identical validation."""
        body = _dataset_projection_request(dataset_id, projection_scope, group_ids, top_k, persist)
        payload = await self._transport.request("POST", "/api/transcriptomics/hswae-project", json=body)
        return _parse_dataset_projection(payload, dataset_id, projection_scope)

    async def upload_dataset(self, file: Path | str, dataset_type: str) -> Dataset:
        """Async: ingest your own counts so the funnel can SI-rank surface
        receptors on it. ``dataset_type`` is ``"bulk"`` / ``"scrna"`` /
        ``"microarray"``. Pass the returned ``.id`` as ``datasetId`` in
        ``custom_dataset_targets`` to :meth:`tissue_markers`. See
        :meth:`Discovery.upload_dataset` for the quickstart.
        """
        path = Path(file)
        with path.open("rb") as f:
            files = {"file": (path.name, f)}
            data = {"datasetType": dataset_type}
            payload = await self._transport.request(
                "POST", "/api/transcriptomics/upload", data=data, files=files
            ) or {}
        return Dataset.model_validate(payload)

    async def list_patient_datasets(self, org_slug: str, patient_slug: str) -> dict[str, Any]:
        """List prepared private transcriptomics datasets for one patient.

        Returns the server's full body, including the ``unavailable`` list. An
        entry the server could not decrypt or parse is reported, not dropped:
        for a person's record, "this one could not be read" is information.
        Raises rather than returning an empty list when the answer is unreadable.
        """
        path = _patient_path(org_slug, patient_slug, "datasets")
        payload = await self._transport.request("GET", path)
        return _parse_patient_dataset_list(payload)

    async def patient_dataset(
        self, org_slug: str, patient_slug: str, dataset_id: str, *,
        include_expression: bool = False,
    ) -> dict[str, Any]:
        """Read one prepared private dataset, and optionally its expression features.

        This returns a **recorded** projection. It does not run a model and must
        not be described as fresh inference. ``include_expression=True`` requires
        the response to carry exactly ``geneCount`` features with unique gene ids
        and finite non-negative numeric ``tpm``; duplicate gene symbols and zero
        values are legitimate and are preserved.
        """
        if type(include_expression) is not bool:
            raise ValueError("include_expression must be boolean")
        seg = _patient_path(org_slug, patient_slug, f"datasets/{_patient_uuid(dataset_id)}")
        path = f"{seg}?includeExpression={'true' if include_expression else 'false'}"
        payload = await self._transport.request("GET", path)
        return _parse_patient_dataset_read(payload, dataset_id, include_expression)

    async def register_patient_dataset(
        self, org_slug: str, patient_slug: str, profile: dict[str, Any],
    ) -> dict[str, Any]:
        """Register a prepared private transcriptomics profile. Superadmin only.

        The four identity fields (``schema``, ``assay``, ``observationUnit``,
        ``expressionUnit``) are checked client-side and never rewritten. A
        registration whose response cannot be read raises and is **not** retried:
        this is a write, and a blind retry can create a second record.
        """
        _validate_patient_profile(profile)
        path = _patient_path(org_slug, patient_slug, "datasets")
        payload = await self._transport.request("POST", path, json=profile)
        return _parse_patient_register(payload)

    async def delete_dataset(self, dataset_id: str | int) -> bool:
        try:
            await self._transport.request("DELETE", f"/api/transcriptomics/datasets/{dataset_id}")
            return True
        except LigandAILegalAcceptanceRequired:
            # ⛔ A legal 403 must not read as "server refused".
            raise
        except Exception as exc:
            # ⛔ `False` here must mean ONLY 'the server was reached and
            # declined'. Every other cause — no connection, bad key, CDN
            # block, 5xx — leaves the outcome UNKNOWN and now raises,
            # because a caller acting on a fabricated 'it did not happen'
            # is worse than one handling an error. [bd-dre-rp11j.3]
            resolve_mutation_failure(exc, operation="discovery.delete_dataset", target=str(dataset_id))
            return False

    async def transport_vasculome(
        self,
        modality: Literal["monovalent", "multivalent", "both"],
        min_score: float = 0.0,
        limit: int = 50,
        include_risks: bool = False,
        specificity_weight: float = 0.0,
    ) -> list[BBBReceptor]:
        """Enterprise-only. BBB transcytosis receptors.

        :param specificity_weight: 0..1 BBB-specificity lever
            [bd-LIGANDAI_ALPHA_V2-k5usa]. ``0`` (default) ranks by transport
            suitability; ``> 0`` blends in the GTEx BBB-specificity index and
            re-ranks to demote broadly-shared (off-target-prone) shuttles.
        """
        if self._client is not None:
            self._client._require_feature("transport_vasculome")
        body = {
            "modality": modality,
            "minScore": min_score,
            "limit": limit,
            "includeRisks": include_risks,
            "specificityWeight": specificity_weight,
        }
        payload = await self._transport.request(
            "POST", "/api/transport-vasculome/query", json=body
        ) or []
        items = (
            payload
            if isinstance(payload, list)
            else payload.get("receptors", payload.get("results", []))
        )
        return [BBBReceptor.model_validate(r) for r in items]

    async def compare_targets(
        self,
        groups: list[TargetGroup | ReferenceGroup | dict[str, Any]],
        mode: Literal["focus", "global", "compare"] = "compare",
        receptor_only: bool = False,
        top_n: int = 100,
    ) -> ComparisonResponse:
        """Async: compare 2+ target groups → SHARED vs DIFFERENTIAL + specificity.
        See :meth:`Discovery.compare_targets`. [bd-LIGANDAI_ALPHA_V2-k5usa]
        """
        if not groups:
            raise ValueError("compare_targets requires at least one group (groups[0]=target)")
        norm = [g if isinstance(g, (TargetGroup, ReferenceGroup)) else TargetGroup.model_validate(g) for g in groups]
        target = TargetGroup.model_validate(norm[0].model_dump(by_alias=True))
        references = [ReferenceGroup.model_validate(g.model_dump(by_alias=True)) for g in norm[1:]]
        resp = await self.compare_groups(
            target_group=target,
            reference_groups=references or None,
            mode=mode,
            receptor_only=receptor_only,
            top_n=top_n,
        )
        shared, differential = _split_shared_differential(resp.results)
        resp.shared_genes = shared
        resp.differential_genes = differential
        return resp

    async def compare_bbb_vs_brain(
        self,
        bbb_modality: Literal["monovalent", "multivalent", "both"] = "monovalent",
        brain_tissues: list[str] | None = None,
        bbb_limit: int = 25,
        specificity_weight: float = 0.0,
        receptor_only: bool = False,
        top_n: int = 100,
    ) -> ComparisonResponse:
        """Async BBB-vs-brain recipe. See :meth:`Discovery.compare_bbb_vs_brain`.
        [bd-LIGANDAI_ALPHA_V2-k5usa]
        """
        brain_tissues = brain_tissues or ["brain_cortex"]
        shuttles = await self.transport_vasculome(
            modality=bbb_modality,
            limit=bbb_limit,
            specificity_weight=specificity_weight,
        )
        shuttle_genes = [r.gene for r in shuttles if r.gene]
        target = TargetGroup(name="BBB transcytosis shuttles", type="geneset", genes=shuttle_genes)
        references = [ReferenceGroup(name=t, type="gtex", tissue=t) for t in brain_tissues]
        return await self.compare_targets(
            [target, *references], mode="compare", receptor_only=receptor_only, top_n=top_n
        )

    async def tissues(self) -> list[str]:
        payload = await self._transport.request("GET", "/api/transcriptomics/tissues")
        return list(
            unwrap_rows(payload, ("tissues",), what="GET /api/transcriptomics/tissues")
        )

    async def organ_systems(self) -> list[str]:
        payload = await self._transport.request("GET", "/api/transcriptomics/organ-systems")
        return _organ_system_names(
            unwrap_rows(
                payload,
                _ORGAN_SYSTEM_KEYS,
                what="GET /api/transcriptomics/organ-systems",
            )
        )
