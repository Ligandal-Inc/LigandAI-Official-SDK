# Copyright © 2026 Ligandal, Inc. All rights reserved.
"""Structure resolution endpoints — gene → PDB / AlphaFold / pocket analysis."""

from __future__ import annotations

from typing import Any, Literal

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

    # ------------------------------------------------------------------
    # v0.5.0 — fold-structure listing endpoints
    # (LIGANDAI_ALPHA_V2-vzkei.3 / Andrew Keene SDK gaps)
    # ------------------------------------------------------------------
    def list(
        self,
        program_id: int | None = None,
        *,
        limit: int = 50,
        offset: int = 0,
    ) -> list[dict[str, Any]]:
        """``GET /api/v1/structures/list`` — list folded structures.

        Returns metadata only (gene, scores, ``pdb_url``); use
        :meth:`get_pdb` to fetch the actual PDB content.

        **Auth:** Free tier sees their own structures. Paid tier sees full
        atomic data; free tier downloads polyalanine PDBs.

        Args:
            program_id: Optional program scope.
            limit: Page size (max 200).
            offset: Pagination offset.

        Returns:
            List of structure metadata dicts. Each row includes
            ``structure_id``, ``fold_id``, ``gene``, ``ipsae``, ``ptm``,
            ``iptm``, ``plddt``, ``isElite``, ``createdAt``, and ``pdb_url``.
        """
        params: dict[str, Any] = {"limit": limit, "offset": offset}
        if program_id is not None:
            params["program_id"] = program_id
        payload = self._transport.request(
            "GET", "/api/v1/structures/list", params=params
        ) or {}
        items = payload.get("structures", []) if isinstance(payload, dict) else (payload or [])
        return list(items)

    def get_pdb(self, structure_id: int | str) -> str:
        """``GET /api/v1/structures/:id/pdb`` — fetch PDB content for a fold.

        Returns the raw PDB text. Free tier receives polyalanine (sidechains
        stripped, ``REMARK 1`` redaction header inserted at top); paid tier
        receives full atomic detail.

        Args:
            structure_id: ``ptf_fold_results.id`` (positive integer).

        Returns:
            PDB content as a string.
        """
        try:
            id_int = int(structure_id)
        except (TypeError, ValueError) as exc:
            raise ValueError(
                f"structure_id must be a positive integer (got {structure_id!r})"
            ) from exc
        if id_int <= 0:
            raise ValueError(f"structure_id must be > 0 (got {id_int})")
        resp = self._transport.request(
            "GET", f"/api/v1/structures/{id_int}/pdb", expect_json=False
        )
        # resp is an httpx.Response when expect_json=False
        return resp.text if hasattr(resp, "text") else str(resp)


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

    # ------------------------------------------------------------------
    # v0.5.0 async fold-structure listing endpoints
    # ------------------------------------------------------------------
    async def list(
        self,
        program_id: int | None = None,
        *,
        limit: int = 50,
        offset: int = 0,
    ) -> list[dict[str, Any]]:
        """Async variant of :meth:`Structures.list`."""
        params: dict[str, Any] = {"limit": limit, "offset": offset}
        if program_id is not None:
            params["program_id"] = program_id
        payload = await self._transport.request(
            "GET", "/api/v1/structures/list", params=params
        ) or {}
        items = payload.get("structures", []) if isinstance(payload, dict) else (payload or [])
        return list(items)

    async def get_pdb(self, structure_id: int | str) -> str:
        """Async variant of :meth:`Structures.get_pdb`."""
        try:
            id_int = int(structure_id)
        except (TypeError, ValueError) as exc:
            raise ValueError(
                f"structure_id must be a positive integer (got {structure_id!r})"
            ) from exc
        if id_int <= 0:
            raise ValueError(f"structure_id must be > 0 (got {id_int})")
        resp = await self._transport.request(
            "GET", f"/api/v1/structures/{id_int}/pdb", expect_json=False
        )
        return resp.text if hasattr(resp, "text") else str(resp)
