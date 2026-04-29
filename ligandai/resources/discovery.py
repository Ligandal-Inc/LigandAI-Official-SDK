# Copyright © 2025 Ligandal, Inc. All rights reserved.
"""Discovery / transcriptomics — tissue markers, scRNA, GEO, comparisons."""

from __future__ import annotations

from pathlib import Path
from typing import Any, Literal

from ligandai.resources._base import AsyncResource, Resource
from ligandai.types import (
    BBBReceptor,
    ComparisonResponse,
    CustomDatasetTarget,
    Dataset,
    ExpressionProfile,
    GeoDataset,
    GeoImportJob,
    MarkerResponse,
    ReferenceGroup,
    TargetGroup,
)


class Discovery(Resource):
    """``/api/transcriptomics/*``, ``/api/scrna/*``, ``/api/geo-import/*``,
    ``/api/transport-vasculome/*``."""

    def tissue_markers(
        self,
        target_tissues: list[str] | None = None,
        custom_dataset_targets: list[CustomDatasetTarget | dict[str, Any]] | None = None,
        exclude_tissues: list[str] | None = None,
        top_n: int = 2000,
        receptor_only: bool = True,
        min_expression: float | None = None,
    ) -> MarkerResponse:
        """SI-ranked tissue markers via GTEx (target_tissues) or scRNA (custom_dataset_targets)."""
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
    ) -> MarkerResponse:
        body: dict[str, object] = {
            "scrnaTissue": scrna_tissue,
            "targetCellTypes": target_cell_types,
            "topN": top_n,
            "receptorOnly": receptor_only,
        }
        if exclude_tissues is not None:
            body["excludeTissues"] = exclude_tissues
        return MarkerResponse.model_validate(
            self._transport.request("POST", "/api/scrna/cell-type-markers", json=body) or {"top": []}
        )

    def gene_expression(self, gene: str) -> ExpressionProfile:
        return ExpressionProfile.model_validate(
            self._transport.request("GET", f"/api/transcriptomics/gene-expression/{gene}") or {"gene": gene}
        )

    def compare_groups(
        self,
        target_group: TargetGroup,
        reference_groups: list[ReferenceGroup] | None = None,
        mode: Literal["focus", "global", "compare"] = "compare",
        receptor_only: bool = False,
        top_n: int = 100,
    ) -> ComparisonResponse:
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

    def search_geo(self, query: str) -> list[GeoDataset]:
        payload = self._transport.request(
            "GET", "/api/geo-import/search", params={"query": query}
        ) or []
        items = payload if isinstance(payload, list) else payload.get("results", [])
        return [GeoDataset.model_validate(d) for d in items]

    def import_geo(self, accession: str) -> GeoImportJob:
        return GeoImportJob.model_validate(
            self._transport.request("POST", "/api/geo-import/start", json={"accession": accession})
            or {"jobId": "", "accession": accession, "status": "queued"}
        )

    def import_status(self, job_id: str) -> GeoImportJob:
        return GeoImportJob.model_validate(
            self._transport.request("GET", f"/api/geo-import/status/{job_id}")
            or {"jobId": job_id, "accession": "", "status": "unknown"}
        )

    def list_datasets(self) -> list[Dataset]:
        payload = self._transport.request("GET", "/api/transcriptomics/datasets") or []
        items = payload if isinstance(payload, list) else payload.get("datasets", [])
        return [Dataset.model_validate(d) for d in items]

    def upload_dataset(self, file: Path | str, dataset_type: str) -> Dataset:
        path = Path(file)
        with path.open("rb") as f:
            files = {"file": (path.name, f)}
            data = {"datasetType": dataset_type}
            payload = self._transport.request(
                "POST", "/api/transcriptomics/upload", data=data, files=files
            ) or {}
        return Dataset.model_validate(payload)

    def delete_dataset(self, dataset_id: str | int) -> bool:
        try:
            self._transport.request("DELETE", f"/api/transcriptomics/datasets/{dataset_id}")
            return True
        except Exception:
            return False

    def transport_vasculome(
        self,
        modality: Literal["monovalent", "multivalent", "both"],
        min_score: float = 0.0,
        limit: int = 50,
        include_risks: bool = False,
    ) -> list[BBBReceptor]:
        """Enterprise-only. BBB transcytosis receptors."""
        if self._client is not None:
            self._client._require_feature("transport_vasculome")
        body = {
            "modality": modality,
            "minScore": min_score,
            "limit": limit,
            "includeRisks": include_risks,
        }
        payload = self._transport.request(
            "POST", "/api/transport-vasculome/query", json=body
        ) or []
        items = payload if isinstance(payload, list) else payload.get("results", [])
        return [BBBReceptor.model_validate(r) for r in items]

    def tissues(self) -> list[str]:
        payload = self._transport.request("GET", "/api/transcriptomics/tissues") or []
        return list(payload if isinstance(payload, list) else payload.get("tissues", []))

    def organ_systems(self) -> list[str]:
        payload = self._transport.request("GET", "/api/transcriptomics/organ-systems") or []
        return list(payload if isinstance(payload, list) else payload.get("systems", []))


class AsyncDiscovery(AsyncResource):
    async def tissue_markers(
        self,
        target_tissues: list[str] | None = None,
        custom_dataset_targets: list[CustomDatasetTarget | dict[str, Any]] | None = None,
        exclude_tissues: list[str] | None = None,
        top_n: int = 2000,
        receptor_only: bool = True,
        min_expression: float | None = None,
    ) -> MarkerResponse:
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
    ) -> MarkerResponse:
        body: dict[str, object] = {
            "scrnaTissue": scrna_tissue,
            "targetCellTypes": target_cell_types,
            "topN": top_n,
            "receptorOnly": receptor_only,
        }
        if exclude_tissues is not None:
            body["excludeTissues"] = exclude_tissues
        return MarkerResponse.model_validate(
            await self._transport.request("POST", "/api/scrna/cell-type-markers", json=body)
            or {"top": []}
        )

    async def gene_expression(self, gene: str) -> ExpressionProfile:
        return ExpressionProfile.model_validate(
            await self._transport.request("GET", f"/api/transcriptomics/gene-expression/{gene}")
            or {"gene": gene}
        )

    async def compare_groups(
        self,
        target_group: TargetGroup,
        reference_groups: list[ReferenceGroup] | None = None,
        mode: Literal["focus", "global", "compare"] = "compare",
        receptor_only: bool = False,
        top_n: int = 100,
    ) -> ComparisonResponse:
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

    async def search_geo(self, query: str) -> list[GeoDataset]:
        payload = await self._transport.request(
            "GET", "/api/geo-import/search", params={"query": query}
        ) or []
        items = payload if isinstance(payload, list) else payload.get("results", [])
        return [GeoDataset.model_validate(d) for d in items]

    async def import_geo(self, accession: str) -> GeoImportJob:
        return GeoImportJob.model_validate(
            await self._transport.request("POST", "/api/geo-import/start", json={"accession": accession})
            or {"jobId": "", "accession": accession, "status": "queued"}
        )

    async def import_status(self, job_id: str) -> GeoImportJob:
        return GeoImportJob.model_validate(
            await self._transport.request("GET", f"/api/geo-import/status/{job_id}")
            or {"jobId": job_id, "accession": "", "status": "unknown"}
        )

    async def list_datasets(self) -> list[Dataset]:
        payload = await self._transport.request("GET", "/api/transcriptomics/datasets") or []
        items = payload if isinstance(payload, list) else payload.get("datasets", [])
        return [Dataset.model_validate(d) for d in items]

    async def upload_dataset(self, file: Path | str, dataset_type: str) -> Dataset:
        path = Path(file)
        with path.open("rb") as f:
            files = {"file": (path.name, f)}
            data = {"datasetType": dataset_type}
            payload = await self._transport.request(
                "POST", "/api/transcriptomics/upload", data=data, files=files
            ) or {}
        return Dataset.model_validate(payload)

    async def delete_dataset(self, dataset_id: str | int) -> bool:
        try:
            await self._transport.request("DELETE", f"/api/transcriptomics/datasets/{dataset_id}")
            return True
        except Exception:
            return False

    async def transport_vasculome(
        self,
        modality: Literal["monovalent", "multivalent", "both"],
        min_score: float = 0.0,
        limit: int = 50,
        include_risks: bool = False,
    ) -> list[BBBReceptor]:
        if self._client is not None:
            self._client._require_feature("transport_vasculome")
        body = {
            "modality": modality,
            "minScore": min_score,
            "limit": limit,
            "includeRisks": include_risks,
        }
        payload = await self._transport.request(
            "POST", "/api/transport-vasculome/query", json=body
        ) or []
        items = payload if isinstance(payload, list) else payload.get("results", [])
        return [BBBReceptor.model_validate(r) for r in items]

    async def tissues(self) -> list[str]:
        payload = await self._transport.request("GET", "/api/transcriptomics/tissues") or []
        return list(payload if isinstance(payload, list) else payload.get("tissues", []))

    async def organ_systems(self) -> list[str]:
        payload = await self._transport.request("GET", "/api/transcriptomics/organ-systems") or []
        return list(payload if isinstance(payload, list) else payload.get("systems", []))
