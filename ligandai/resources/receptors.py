# Copyright © 2026 Ligandal, Inc. All rights reserved.
"""ReceptorDB endpoints — search, browse, download, classification."""

from __future__ import annotations

from collections.abc import AsyncIterator, Iterator
from pathlib import Path
from typing import Any, List

from ligandai.resources._base import AsyncResource, Resource, unwrap_rows
from ligandai.types import (
    ChainClassification,
    FoldQueueStatus,
    FoldRequest,
    ReceptorComplex,
    ReceptorListResponse,
)

#: Envelope keys ``GET /api/receptordb/search`` has been seen to use, live key
#: first. The route answers ``{query, matchAll, total, complexes: [...]}``
#: (measured against blue 2026-10-02: 38 rows for ``EGFR``); ``results`` /
#: ``data`` stay behind it for older servers. [bd-dre-3imtv.3]
_SEARCH_KEYS = ("complexes", "results", "data")

#: ``GET /api/receptordb/oligomeric-states`` answers a bare list of
#: ``{"state": ..., "count": ...}`` rows, not of strings. [bd-dre-3imtv.3]
_STATE_KEYS = ("states", "oligomericStates", "oligomeric_states")


def _state_names(rows: list[Any]) -> List[str]:
    """Oligomeric-state NAMES out of the route's rows.

    ⛔ The route answers ``[{"state": "homodimer", "count": 412}, ...]`` (255
    rows live), and this method is typed ``List[str]``. It used to hand the raw
    dicts back, so the declared type was a lie and ``"homodimer" in
    client.receptors.oligomeric_states()`` was always False — while
    ``oligomeric_state=`` on :meth:`Receptors.search` takes exactly that
    string. The count stays reachable through
    :meth:`Receptors.summary`'s ``byOligomericState``. [bd-dre-3imtv.3]
    """
    out: List[str] = []
    for row in rows:
        if isinstance(row, str):
            out.append(row)
        elif isinstance(row, dict):
            name = row.get("state", row.get("name", row.get("oligomericState")))
            if isinstance(name, str):
                out.append(name)
    return out


class Receptors(Resource):
    """``/api/receptordb/*``.

    Read-mostly. Most browse/search endpoints are public (AUTH-N). Downloads
    require any tier (AUTH-S, rate-limited 5/min). Fold-queue endpoints
    require auth.
    """

    def _stamp_organism(
        self, params: dict[str, Any], organism: str | None, species: str | None
    ) -> None:
        """Fail-closed stamp of the effective ``organism``/``species``.

        Species selection is entitlement-gated: a non-entitled key can never
        force ``mouse`` — it is coerced to ``human`` locally. No-op when no
        parent client is attached (e.g. a standalone ``Receptors`` instance).

        ReceptorDB browse/search is a public, mostly-anonymous surface, so the
        default-human case stays byte-identical to the legacy wire (no
        ``organism`` param) — the field is only stamped when the caller
        explicitly requests a species OR the client default is non-human. Either
        way the effective species is entitlement-gated (mouse -> human when not
        entitled).
        """
        requested = organism if organism is not None else species
        if self._client is None:
            return
        if requested is None and self._client.default_organism == "human":
            return
        self._client._apply_species(params, requested)

    def search(
        self,
        query: str,
        oligomeric_state: str | None = None,
        limit: int = 10,
        *,
        organism: str | None = None,
        species: str | None = None,
    ) -> List[ReceptorComplex]:
        """Search ReceptorDB complexes.

        :param organism: species namespace — ``"human"`` (default) or
            ``"mouse"``. Falls back to the client ``default_organism``; coerced to
            ``"human"`` when the key is not entitled (fail-closed).
        :param species: alias for ``organism`` (server-parity). ``organism`` wins.
        :raises LigandAIUnreadableResponse: the body carries no recognised
            list of complexes. An unreadable answer is never reported as
            "no matches" — see :func:`ligandai.resources._base.unwrap_rows`.
        """
        params: dict[str, object] = {"query": query, "limit": limit}
        if oligomeric_state is not None:
            params["oligomeric_state"] = oligomeric_state
        self._stamp_organism(params, organism, species)
        payload = self._transport.request("GET", "/api/receptordb/search", params=params)
        items = unwrap_rows(
            payload,
            _SEARCH_KEYS,
            what=f"GET /api/receptordb/search for {query!r}",
        )
        return [ReceptorComplex.model_validate(c) for c in items]

    def get(self, complex_id: str) -> ReceptorComplex:
        return ReceptorComplex.model_validate(
            self._transport.request("GET", f"/api/receptordb/complexes/{complex_id}") or {}
        )

    def list(
        self,
        offset: int = 0,
        limit: int = 50,
        sort_by: str = "name",
        sort_order: str = "asc",
        **filters: Any,
    ) -> ReceptorListResponse:
        params: dict[str, Any] = {
            "offset": offset,
            "limit": limit,
            "sort_by": sort_by,
            "sort_order": sort_order,
        }
        params.update(filters)
        payload = self._transport.request("GET", "/api/receptordb/complexes", params=params) or {}
        return ReceptorListResponse.model_validate(_normalize_list(payload, offset, limit))

    def iter_all(
        self,
        page_size: int = 100,
        sort_by: str = "name",
        sort_order: str = "asc",
        **filters: Any,
    ) -> Iterator[ReceptorComplex]:
        """Iterate every receptor across pages."""
        offset = 0
        while True:
            page = self.list(
                offset=offset,
                limit=page_size,
                sort_by=sort_by,
                sort_order=sort_order,
                **filters,
            )
            yield from page.complexes
            if not page.has_more:
                return
            offset += len(page.complexes)
            if not page.complexes:
                return  # safety

    def by_gene(
        self,
        gene: str,
        *,
        organism: str | None = None,
        species: str | None = None,
    ) -> List[ReceptorComplex]:
        """Complexes for a gene. ``organism`` (``"human"`` default / ``"mouse"``)
        selects the namespace, fail-closed to human when not entitled;
        ``species`` is an alias.
        """
        params: dict[str, Any] = {}
        self._stamp_organism(params, organism, species)
        payload = self._transport.request(
            "GET", f"/api/receptordb/by-gene/{gene}", params=params or None
        )
        items = unwrap_rows(
            payload,
            _SEARCH_KEYS,
            what=f"GET /api/receptordb/by-gene/{gene}",
        )
        return [ReceptorComplex.model_validate(c) for c in items]

    def chain_classification(self, gene: str) -> ChainClassification:
        return ChainClassification.model_validate(
            self._transport.request("GET", f"/api/receptordb/chain-classification/{gene}") or {}
        )

    def batch_chain_classification(
        self,
        genes: List[str],
        include_alphafold: bool = True,
    ) -> dict[str, ChainClassification]:
        body = {"genes": genes, "include_alphafold": include_alphafold}
        payload = self._transport.request(
            "POST", "/api/receptordb/batch-chain-classification", json=body
        ) or {}
        results = payload.get("results", payload)
        return {gene: ChainClassification.model_validate(v) for gene, v in results.items()}

    def download_pdb(self, complex_id: str, dest: Path | str) -> Path:
        """Download the PDB file. Rate-limited 5/min."""
        dest_path = Path(dest)
        resp = self._transport.request(
            "GET", f"/api/receptordb/download/{complex_id}", expect_json=False
        )
        dest_path.parent.mkdir(parents=True, exist_ok=True)
        dest_path.write_bytes(resp.content)
        return dest_path

    def request_fold(self, gene: str) -> FoldRequest:
        return FoldRequest.model_validate(
            self._transport.request("POST", "/api/receptordb/request-fold", json={"gene": gene}) or {}
        )

    def fold_queue_status(self, request_id: int | str) -> FoldQueueStatus:
        return FoldQueueStatus.model_validate(
            self._transport.request("GET", f"/api/receptordb/fold-queue/status/{request_id}") or {}
        )

    def summary(self) -> dict[str, Any]:
        """Database summary (counts, last update)."""
        return self._transport.request("GET", "/api/receptordb/summary") or {}

    def oligomeric_states(self) -> List[str]:
        """Oligomeric-state names, as :meth:`search` takes them."""
        payload = self._transport.request("GET", "/api/receptordb/oligomeric-states")
        return _state_names(
            unwrap_rows(payload, _STATE_KEYS, what="GET /api/receptordb/oligomeric-states")
        )

    def genes(self) -> List[str]:
        payload = self._transport.request("GET", "/api/receptordb/genes")
        return list(unwrap_rows(payload, ("genes",), what="GET /api/receptordb/genes"))


class AsyncReceptors(AsyncResource):
    def _stamp_organism(
        self, params: dict[str, Any], organism: str | None, species: str | None
    ) -> None:
        """Fail-closed stamp — see :meth:`Receptors._stamp_organism`.

        Default-human stays byte-identical to the legacy wire; only stamps when
        a species is requested or the client default is non-human.
        """
        requested = organism if organism is not None else species
        if self._client is None:
            return
        if requested is None and self._client.default_organism == "human":
            return
        self._client._apply_species(params, requested)

    async def search(
        self,
        query: str,
        oligomeric_state: str | None = None,
        limit: int = 10,
        *,
        organism: str | None = None,
        species: str | None = None,
    ) -> List[ReceptorComplex]:
        """Async ReceptorDB search. ``organism`` (``"human"`` default /
        ``"mouse"``) selects the namespace, fail-closed to human when not
        entitled; ``species`` is an alias.

        :raises LigandAIUnreadableResponse: the body carries no recognised
            list of complexes — never reported as "no matches".
        """
        params: dict[str, object] = {"query": query, "limit": limit}
        if oligomeric_state is not None:
            params["oligomeric_state"] = oligomeric_state
        self._stamp_organism(params, organism, species)
        payload = await self._transport.request("GET", "/api/receptordb/search", params=params)
        items = unwrap_rows(
            payload,
            _SEARCH_KEYS,
            what=f"GET /api/receptordb/search for {query!r}",
        )
        return [ReceptorComplex.model_validate(c) for c in items]

    async def get(self, complex_id: str) -> ReceptorComplex:
        return ReceptorComplex.model_validate(
            await self._transport.request("GET", f"/api/receptordb/complexes/{complex_id}") or {}
        )

    async def list(
        self,
        offset: int = 0,
        limit: int = 50,
        sort_by: str = "name",
        sort_order: str = "asc",
        **filters: Any,
    ) -> ReceptorListResponse:
        params: dict[str, Any] = {
            "offset": offset,
            "limit": limit,
            "sort_by": sort_by,
            "sort_order": sort_order,
        }
        params.update(filters)
        payload = await self._transport.request("GET", "/api/receptordb/complexes", params=params) or {}
        return ReceptorListResponse.model_validate(_normalize_list(payload, offset, limit))

    async def iter_all(
        self,
        page_size: int = 100,
        sort_by: str = "name",
        sort_order: str = "asc",
        **filters: Any,
    ) -> AsyncIterator[ReceptorComplex]:
        offset = 0
        while True:
            page = await self.list(
                offset=offset,
                limit=page_size,
                sort_by=sort_by,
                sort_order=sort_order,
                **filters,
            )
            for c in page.complexes:
                yield c
            if not page.has_more:
                return
            offset += len(page.complexes)
            if not page.complexes:
                return

    async def by_gene(
        self,
        gene: str,
        *,
        organism: str | None = None,
        species: str | None = None,
    ) -> List[ReceptorComplex]:
        """Async complexes for a gene. ``organism`` (``"human"`` default /
        ``"mouse"``) selects the namespace, fail-closed to human when not
        entitled; ``species`` is an alias.
        """
        params: dict[str, Any] = {}
        self._stamp_organism(params, organism, species)
        payload = await self._transport.request(
            "GET", f"/api/receptordb/by-gene/{gene}", params=params or None
        )
        items = unwrap_rows(
            payload,
            _SEARCH_KEYS,
            what=f"GET /api/receptordb/by-gene/{gene}",
        )
        return [ReceptorComplex.model_validate(c) for c in items]

    async def chain_classification(self, gene: str) -> ChainClassification:
        return ChainClassification.model_validate(
            await self._transport.request("GET", f"/api/receptordb/chain-classification/{gene}") or {}
        )

    async def batch_chain_classification(
        self,
        genes: List[str],
        include_alphafold: bool = True,
    ) -> dict[str, ChainClassification]:
        body = {"genes": genes, "include_alphafold": include_alphafold}
        payload = await self._transport.request(
            "POST", "/api/receptordb/batch-chain-classification", json=body
        ) or {}
        results = payload.get("results", payload)
        return {gene: ChainClassification.model_validate(v) for gene, v in results.items()}

    async def download_pdb(self, complex_id: str, dest: Path | str) -> Path:
        dest_path = Path(dest)
        resp = await self._transport.request(
            "GET", f"/api/receptordb/download/{complex_id}", expect_json=False
        )
        dest_path.parent.mkdir(parents=True, exist_ok=True)
        dest_path.write_bytes(resp.content)
        return dest_path

    async def request_fold(self, gene: str) -> FoldRequest:
        return FoldRequest.model_validate(
            await self._transport.request("POST", "/api/receptordb/request-fold", json={"gene": gene}) or {}
        )

    async def fold_queue_status(self, request_id: int | str) -> FoldQueueStatus:
        return FoldQueueStatus.model_validate(
            await self._transport.request("GET", f"/api/receptordb/fold-queue/status/{request_id}") or {}
        )

    async def summary(self) -> dict[str, Any]:
        return await self._transport.request("GET", "/api/receptordb/summary") or {}

    async def oligomeric_states(self) -> List[str]:
        """Oligomeric-state names, as :meth:`AsyncReceptors.search` takes them."""
        payload = await self._transport.request("GET", "/api/receptordb/oligomeric-states")
        return _state_names(
            unwrap_rows(payload, _STATE_KEYS, what="GET /api/receptordb/oligomeric-states")
        )

    async def genes(self) -> List[str]:
        payload = await self._transport.request("GET", "/api/receptordb/genes")
        return list(unwrap_rows(payload, ("genes",), what="GET /api/receptordb/genes"))


def _normalize_list(payload: dict[str, Any], offset: int, limit: int) -> dict[str, Any]:
    """Normalize list payload to ReceptorListResponse fields."""
    if "complexes" in payload:
        return payload
    if isinstance(payload, list):
        return {"complexes": payload, "total": len(payload), "offset": offset, "limit": limit}
    return {
        "complexes": payload.get("results", payload.get("items", [])),
        "total": payload.get("total", 0),
        "offset": payload.get("offset", offset),
        "limit": payload.get("limit", limit),
    }
