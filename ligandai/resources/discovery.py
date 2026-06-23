# Copyright © 2026 Ligandal, Inc. All rights reserved.
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
        specificity_weight: float = 0.0,
    ) -> list[BBBReceptor]:
        """Enterprise-only. BBB transcytosis receptors.

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
        """Compare 2+ desired target groups and surface SHARED vs DIFFERENTIAL
        genes plus specificity. [bd-LIGANDAI_ALPHA_V2-k5usa]

        ``groups[0]`` is the target; ``groups[1:]`` are references. A group can be
        a custom dataset (``type='custom'`` + ``dataset_id`` / ``cell_types``), a
        GTEx tissue (``type='gtex'`` + ``tissue``), or a gene/receptor set
        (``type='geneset'`` + ``genes``), e.g. a BBB transcytosis shuttle set
        or a pathway. This is what lets you compare "BBB-shuttle set vs brain-cortex
        targets" or "pathway A vs pathway B" in ONE call, and lets a user see
        which BBB shuttles are brain-selective vs broadly shared.

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
        payload = await self._transport.request("GET", "/api/transcriptomics/tissues") or []
        return list(payload if isinstance(payload, list) else payload.get("tissues", []))

    async def organ_systems(self) -> list[str]:
        payload = await self._transport.request("GET", "/api/transcriptomics/organ-systems") or []
        return list(payload if isinstance(payload, list) else payload.get("systems", []))
