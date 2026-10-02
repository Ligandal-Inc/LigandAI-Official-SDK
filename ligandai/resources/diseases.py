# Copyright © 2026 Ligandal, Inc. All rights reserved.
"""DiseaseViewer endpoints."""

from __future__ import annotations

from ligandai.resources._base import AsyncResource, Resource, unwrap_rows
from ligandai.types import Disease, Mutation

#: ``GET /api/disease-viewer/search`` answers ``{"diseases": [...], "total": n}``
#: — measured against blue 2026-10-02: 47 rows for ``"cancer"`` while this
#: module was reading ``results`` and reporting none. ``results`` / ``data``
#: stay behind it for older servers. [bd-dre-3imtv.3]
_DISEASE_KEYS = ("diseases", "results", "data")
_MUTATION_KEYS = ("mutations", "results", "data")


class Diseases(Resource):
    """``/api/disease-viewer/*``."""

    def search(self, query: str) -> list[Disease]:
        payload = self._transport.request(
            "GET", "/api/disease-viewer/search", params={"query": query}
        )
        items = unwrap_rows(
            payload, _DISEASE_KEYS, what=f"GET /api/disease-viewer/search for {query!r}"
        )
        return [Disease.model_validate(d) for d in items]

    def get(self, disease_id: int) -> Disease:
        return Disease.model_validate(
            self._transport.request("GET", f"/api/disease-viewer/disease/{disease_id}") or {}
        )

    def mutations(self, disease_id: int) -> list[Mutation]:
        payload = self._transport.request(
            "GET", f"/api/disease-viewer/mutations/{disease_id}"
        )
        items = unwrap_rows(
            payload,
            _MUTATION_KEYS,
            what=f"GET /api/disease-viewer/mutations/{disease_id}",
        )
        return [Mutation.model_validate(m) for m in items]

    def gene_mutations(self, gene: str) -> list[Mutation]:
        payload = self._transport.request(
            "GET", f"/api/disease-viewer/gene-mutations/{gene}"
        )
        items = unwrap_rows(
            payload,
            _MUTATION_KEYS,
            what=f"GET /api/disease-viewer/gene-mutations/{gene}",
        )
        return [Mutation.model_validate(m) for m in items]

    def categories(self) -> list[str]:
        payload = self._transport.request("GET", "/api/disease-viewer/categories")
        return list(
            unwrap_rows(
                payload, ("categories", "results"), what="GET /api/disease-viewer/categories"
            )
        )


class AsyncDiseases(AsyncResource):
    async def search(self, query: str) -> list[Disease]:
        payload = await self._transport.request(
            "GET", "/api/disease-viewer/search", params={"query": query}
        )
        items = unwrap_rows(
            payload, _DISEASE_KEYS, what=f"GET /api/disease-viewer/search for {query!r}"
        )
        return [Disease.model_validate(d) for d in items]

    async def get(self, disease_id: int) -> Disease:
        return Disease.model_validate(
            await self._transport.request("GET", f"/api/disease-viewer/disease/{disease_id}") or {}
        )

    async def mutations(self, disease_id: int) -> list[Mutation]:
        payload = await self._transport.request(
            "GET", f"/api/disease-viewer/mutations/{disease_id}"
        )
        items = unwrap_rows(
            payload,
            _MUTATION_KEYS,
            what=f"GET /api/disease-viewer/mutations/{disease_id}",
        )
        return [Mutation.model_validate(m) for m in items]

    async def gene_mutations(self, gene: str) -> list[Mutation]:
        payload = await self._transport.request(
            "GET", f"/api/disease-viewer/gene-mutations/{gene}"
        )
        items = unwrap_rows(
            payload,
            _MUTATION_KEYS,
            what=f"GET /api/disease-viewer/gene-mutations/{gene}",
        )
        return [Mutation.model_validate(m) for m in items]

    async def categories(self) -> list[str]:
        payload = await self._transport.request("GET", "/api/disease-viewer/categories")
        return list(
            unwrap_rows(
                payload, ("categories", "results"), what="GET /api/disease-viewer/categories"
            )
        )
