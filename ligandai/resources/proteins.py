# Copyright © 2026 Ligandal, Inc. All rights reserved.
"""UniProt info, variants, glycosylation, and custom protein uploads."""

from __future__ import annotations

from pathlib import Path
from typing import Literal

from ligandai.resources._base import AsyncResource, Resource
from ligandai.types import (
    DisorderProfile,
    GlycosylationData,
    ProteinInfo,
    ProteinVariant,
    ReceptorIntelligence,
    ReceptorTopology,
    UserProtein,
)


class Proteins(Resource):
    """``/api/protein-info/*``, ``/api/receptor-topology``, ``/api/protein-variants/*``,
    ``/api/internal-module/*``, ``/api/user-proteins/*``."""

    def info(
        self,
        query: str,
        include_sequence: bool = True,
        include_ptms: bool = True,
        include_domains: bool = True,
    ) -> ProteinInfo:
        params = {
            "include_sequence": include_sequence,
            "include_ptms": include_ptms,
            "include_domains": include_domains,
        }
        return ProteinInfo.model_validate(
            self._transport.request("GET", f"/api/protein-info/{query}", params=params)
            or {"gene": query}
        )

    def disorder_profile(self, gene: str) -> DisorderProfile:
        return DisorderProfile.model_validate(
            self._transport.request("GET", f"/api/protein-info/disorder/{gene}") or {"gene": gene}
        )

    def receptor_topology(self, gene: str) -> ReceptorTopology:
        return ReceptorTopology.model_validate(
            self._transport.request("GET", f"/api/receptor-topology/{gene}") or {"gene": gene}
        )

    def receptor_intelligence(
        self,
        gene: str | None = None,
        genes: list[str] | None = None,
    ) -> ReceptorIntelligence | dict[str, ReceptorIntelligence]:
        if genes is not None:
            payload = self._transport.request(
                "POST", "/api/receptor-intelligence/batch", json={"genes": genes}
            ) or {}
            results = payload.get("results", payload)
            return {g: ReceptorIntelligence.model_validate(v) for g, v in results.items()}
        if gene is None:
            raise ValueError("Pass gene= or genes=")
        return ReceptorIntelligence.model_validate(
            self._transport.request("GET", f"/api/receptor-intelligence/{gene}") or {"gene": gene}
        )

    def check_glycosylation(
        self,
        gene: str,
        tissue: str | None = None,
        site_type: Literal["N-linked", "O-linked"] | None = None,
    ) -> GlycosylationData:
        params: dict[str, object] = {}
        if tissue is not None:
            params["tissue"] = tissue
        if site_type is not None:
            params["site_type"] = site_type
        return GlycosylationData.model_validate(
            self._transport.request("GET", f"/api/internal-module/sites/{gene}", params=params)
            or {"gene": gene}
        )

    def variants(
        self,
        gene: str | None = None,
        include_shared: bool = True,
    ) -> list[ProteinVariant]:
        params: dict[str, object] = {"include_shared": include_shared}
        if gene is not None:
            params["gene"] = gene
        payload = self._transport.request("GET", "/api/protein-variants", params=params) or []
        items = payload if isinstance(payload, list) else payload.get("variants", [])
        return [ProteinVariant.model_validate(v) for v in items]

    def get_variant(self, variant_id: int) -> ProteinVariant:
        return ProteinVariant.model_validate(
            self._transport.request("GET", f"/api/protein-variants/{variant_id}") or {"id": variant_id, "gene": ""}
        )

    def save_fold_as_variant(self, fold_job_id: str, gene: str, alias: str) -> ProteinVariant:
        return ProteinVariant.model_validate(
            self._transport.request(
                "POST",
                "/api/protein-variants/from-fold",
                json={"foldJobId": fold_job_id, "gene": gene, "alias": alias},
            )
            or {"id": 0, "gene": gene, "alias": alias}
        )

    def delete_variant(self, variant_id: int) -> bool:
        try:
            self._transport.request("DELETE", f"/api/protein-variants/{variant_id}")
            return True
        except Exception:
            return False

    def upload_pdb(
        self,
        file: Path | str,
        gene: str,
        custom_name: str | None = None,
    ) -> UserProtein:
        path = Path(file)
        with path.open("rb") as f:
            files = {"file": (path.name, f, "chemical/x-pdb")}
            data = {"gene": gene}
            if custom_name is not None:
                data["customName"] = custom_name
            payload = self._transport.request(
                "POST", "/api/user-proteins/upload", data=data, files=files
            ) or {}
        return UserProtein.model_validate(payload)


class AsyncProteins(AsyncResource):
    async def info(
        self,
        query: str,
        include_sequence: bool = True,
        include_ptms: bool = True,
        include_domains: bool = True,
    ) -> ProteinInfo:
        params = {
            "include_sequence": include_sequence,
            "include_ptms": include_ptms,
            "include_domains": include_domains,
        }
        return ProteinInfo.model_validate(
            await self._transport.request("GET", f"/api/protein-info/{query}", params=params)
            or {"gene": query}
        )

    async def disorder_profile(self, gene: str) -> DisorderProfile:
        return DisorderProfile.model_validate(
            await self._transport.request("GET", f"/api/protein-info/disorder/{gene}") or {"gene": gene}
        )

    async def receptor_topology(self, gene: str) -> ReceptorTopology:
        return ReceptorTopology.model_validate(
            await self._transport.request("GET", f"/api/receptor-topology/{gene}") or {"gene": gene}
        )

    async def receptor_intelligence(
        self,
        gene: str | None = None,
        genes: list[str] | None = None,
    ) -> ReceptorIntelligence | dict[str, ReceptorIntelligence]:
        if genes is not None:
            payload = await self._transport.request(
                "POST", "/api/receptor-intelligence/batch", json={"genes": genes}
            ) or {}
            results = payload.get("results", payload)
            return {g: ReceptorIntelligence.model_validate(v) for g, v in results.items()}
        if gene is None:
            raise ValueError("Pass gene= or genes=")
        return ReceptorIntelligence.model_validate(
            await self._transport.request("GET", f"/api/receptor-intelligence/{gene}") or {"gene": gene}
        )

    async def check_glycosylation(
        self,
        gene: str,
        tissue: str | None = None,
        site_type: Literal["N-linked", "O-linked"] | None = None,
    ) -> GlycosylationData:
        params: dict[str, object] = {}
        if tissue is not None:
            params["tissue"] = tissue
        if site_type is not None:
            params["site_type"] = site_type
        return GlycosylationData.model_validate(
            await self._transport.request("GET", f"/api/internal-module/sites/{gene}", params=params)
            or {"gene": gene}
        )

    async def variants(
        self,
        gene: str | None = None,
        include_shared: bool = True,
    ) -> list[ProteinVariant]:
        params: dict[str, object] = {"include_shared": include_shared}
        if gene is not None:
            params["gene"] = gene
        payload = await self._transport.request("GET", "/api/protein-variants", params=params) or []
        items = payload if isinstance(payload, list) else payload.get("variants", [])
        return [ProteinVariant.model_validate(v) for v in items]

    async def get_variant(self, variant_id: int) -> ProteinVariant:
        return ProteinVariant.model_validate(
            await self._transport.request("GET", f"/api/protein-variants/{variant_id}")
            or {"id": variant_id, "gene": ""}
        )

    async def save_fold_as_variant(self, fold_job_id: str, gene: str, alias: str) -> ProteinVariant:
        return ProteinVariant.model_validate(
            await self._transport.request(
                "POST",
                "/api/protein-variants/from-fold",
                json={"foldJobId": fold_job_id, "gene": gene, "alias": alias},
            )
            or {"id": 0, "gene": gene, "alias": alias}
        )

    async def delete_variant(self, variant_id: int) -> bool:
        try:
            await self._transport.request("DELETE", f"/api/protein-variants/{variant_id}")
            return True
        except Exception:
            return False

    async def upload_pdb(
        self,
        file: Path | str,
        gene: str,
        custom_name: str | None = None,
    ) -> UserProtein:
        path = Path(file)
        with path.open("rb") as f:
            files = {"file": (path.name, f, "chemical/x-pdb")}
            data = {"gene": gene}
            if custom_name is not None:
                data["customName"] = custom_name
            payload = await self._transport.request(
                "POST", "/api/user-proteins/upload", data=data, files=files
            ) or {}
        return UserProtein.model_validate(payload)
