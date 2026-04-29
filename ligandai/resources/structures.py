# Copyright © 2025 Ligandal, Inc. All rights reserved.
"""Structure resolution endpoints — gene → PDB / AlphaFold / pocket analysis."""

from __future__ import annotations

from typing import Literal

from ligandai.resources._base import AsyncResource, Resource
from ligandai.types import (
    GeneResolution,
    Structure,
    StructureAnalysis,
    StructureCandidate,
)


class Structures(Resource):
    """``/api/structure/*``, ``/api/gene-resolver/*``."""

    def get(self, gene: str) -> Structure:
        """``GET /api/structure/:gene`` — best structure (FastAPI 5058 proxy)."""
        return Structure.model_validate(
            self._transport.request("GET", f"/api/structure/{gene}") or {"gene": gene, "source": "unknown"}
        )

    def candidates(self, gene: str) -> list[StructureCandidate]:
        payload = self._transport.request("GET", f"/api/structure/candidates/{gene}") or []
        items = payload if isinstance(payload, list) else payload.get("candidates", [])
        return [StructureCandidate.model_validate(c) for c in items]

    def analyze(
        self,
        gene: str,
        pdb_code: str | None = None,
        analysis_depth: Literal["quick", "full"] = "full",
    ) -> StructureAnalysis:
        body: dict[str, object] = {"gene": gene, "analysis_depth": analysis_depth}
        if pdb_code is not None:
            body["pdb_code"] = pdb_code
        return StructureAnalysis.model_validate(
            self._transport.request("POST", "/api/structure/analyze", json=body) or {"gene": gene}
        )

    def from_pdb(self, pdb_id: str) -> Structure:
        """``GET /api/structure/pdb/:pdbId`` — fetch by PDB code."""
        return Structure.model_validate(
            self._transport.request("GET", f"/api/structure/pdb/{pdb_id}")
            or {"gene": pdb_id, "source": "pdb", "pdbCode": pdb_id}
        )

    def from_alphafold(self, uniprot_id: str) -> Structure:
        """``GET /api/structure/alphafold/:uniprotId``."""
        return Structure.model_validate(
            self._transport.request("GET", f"/api/structure/alphafold/{uniprot_id}")
            or {"gene": uniprot_id, "source": "alphafold", "uniprotId": uniprot_id}
        )

    def from_uniprot(self, uniprot_id: str) -> Structure:
        """Alias for :meth:`from_alphafold`."""
        return self.from_alphafold(uniprot_id)

    def resolve_gene_name(self, query: str) -> GeneResolution:
        """``POST /api/gene-resolver/resolve`` — gene name → canonical symbol + UniProt."""
        return GeneResolution.model_validate(
            self._transport.request("POST", "/api/gene-resolver/resolve", json={"query": query}) or {"query": query}
        )

    def resolve(self, gene: str | None = None, *, pdb_id: str | None = None, uniprot_id: str | None = None) -> Structure:
        """Convenience: resolve a structure from any one of gene / PDB id / UniProt id."""
        if pdb_id is not None:
            return self.from_pdb(pdb_id)
        if uniprot_id is not None:
            return self.from_alphafold(uniprot_id)
        if gene is not None:
            return self.get(gene)
        raise ValueError("Pass one of gene=, pdb_id=, or uniprot_id=")


class AsyncStructures(AsyncResource):
    async def get(self, gene: str) -> Structure:
        return Structure.model_validate(
            await self._transport.request("GET", f"/api/structure/{gene}")
            or {"gene": gene, "source": "unknown"}
        )

    async def candidates(self, gene: str) -> list[StructureCandidate]:
        payload = await self._transport.request("GET", f"/api/structure/candidates/{gene}") or []
        items = payload if isinstance(payload, list) else payload.get("candidates", [])
        return [StructureCandidate.model_validate(c) for c in items]

    async def analyze(
        self,
        gene: str,
        pdb_code: str | None = None,
        analysis_depth: Literal["quick", "full"] = "full",
    ) -> StructureAnalysis:
        body: dict[str, object] = {"gene": gene, "analysis_depth": analysis_depth}
        if pdb_code is not None:
            body["pdb_code"] = pdb_code
        return StructureAnalysis.model_validate(
            await self._transport.request("POST", "/api/structure/analyze", json=body) or {"gene": gene}
        )

    async def from_pdb(self, pdb_id: str) -> Structure:
        return Structure.model_validate(
            await self._transport.request("GET", f"/api/structure/pdb/{pdb_id}")
            or {"gene": pdb_id, "source": "pdb", "pdbCode": pdb_id}
        )

    async def from_alphafold(self, uniprot_id: str) -> Structure:
        return Structure.model_validate(
            await self._transport.request("GET", f"/api/structure/alphafold/{uniprot_id}")
            or {"gene": uniprot_id, "source": "alphafold", "uniprotId": uniprot_id}
        )

    async def from_uniprot(self, uniprot_id: str) -> Structure:
        return await self.from_alphafold(uniprot_id)

    async def resolve_gene_name(self, query: str) -> GeneResolution:
        return GeneResolution.model_validate(
            await self._transport.request("POST", "/api/gene-resolver/resolve", json={"query": query})
            or {"query": query}
        )

    async def resolve(
        self,
        gene: str | None = None,
        *,
        pdb_id: str | None = None,
        uniprot_id: str | None = None,
    ) -> Structure:
        if pdb_id is not None:
            return await self.from_pdb(pdb_id)
        if uniprot_id is not None:
            return await self.from_alphafold(uniprot_id)
        if gene is not None:
            return await self.get(gene)
        raise ValueError("Pass one of gene=, pdb_id=, or uniprot_id=")
