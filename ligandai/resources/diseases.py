# Copyright © 2026 Ligandal, Inc. All rights reserved.
"""DiseaseViewer endpoints."""

from __future__ import annotations

from ligandai.resources._base import AsyncResource, Resource
from ligandai.types import Disease, Mutation


class Diseases(Resource):
    """``/api/disease-viewer/*``."""

    def search(self, query: str) -> list[Disease]:
        payload = self._transport.request(
            "GET", "/api/disease-viewer/search", params={"query": query}
        ) or []
        items = payload if isinstance(payload, list) else payload.get("results", [])
        return [Disease.model_validate(d) for d in items]

    def get(self, disease_id: int) -> Disease:
        return Disease.model_validate(
            self._transport.request("GET", f"/api/disease-viewer/disease/{disease_id}") or {}
        )

    def mutations(self, disease_id: int) -> list[Mutation]:
        payload = self._transport.request(
            "GET", f"/api/disease-viewer/mutations/{disease_id}"
        ) or []
        items = payload if isinstance(payload, list) else payload.get("mutations", [])
        return [Mutation.model_validate(m) for m in items]

    def gene_mutations(self, gene: str) -> list[Mutation]:
        payload = self._transport.request(
            "GET", f"/api/disease-viewer/gene-mutations/{gene}"
        ) or []
        items = payload if isinstance(payload, list) else payload.get("mutations", [])
        return [Mutation.model_validate(m) for m in items]

    def categories(self) -> list[str]:
        payload = self._transport.request("GET", "/api/disease-viewer/categories") or []
        return list(payload if isinstance(payload, list) else payload.get("categories", []))


class AsyncDiseases(AsyncResource):
    async def search(self, query: str) -> list[Disease]:
        payload = await self._transport.request(
            "GET", "/api/disease-viewer/search", params={"query": query}
        ) or []
        items = payload if isinstance(payload, list) else payload.get("results", [])
        return [Disease.model_validate(d) for d in items]

    async def get(self, disease_id: int) -> Disease:
        return Disease.model_validate(
            await self._transport.request("GET", f"/api/disease-viewer/disease/{disease_id}") or {}
        )

    async def mutations(self, disease_id: int) -> list[Mutation]:
        payload = await self._transport.request(
            "GET", f"/api/disease-viewer/mutations/{disease_id}"
        ) or []
        items = payload if isinstance(payload, list) else payload.get("mutations", [])
        return [Mutation.model_validate(m) for m in items]

    async def gene_mutations(self, gene: str) -> list[Mutation]:
        payload = await self._transport.request(
            "GET", f"/api/disease-viewer/gene-mutations/{gene}"
        ) or []
        items = payload if isinstance(payload, list) else payload.get("mutations", [])
        return [Mutation.model_validate(m) for m in items]

    async def categories(self) -> list[str]:
        payload = await self._transport.request("GET", "/api/disease-viewer/categories") or []
        return list(payload if isinstance(payload, list) else payload.get("categories", []))
