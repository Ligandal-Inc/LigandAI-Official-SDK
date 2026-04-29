# Copyright © 2025 Ligandal, Inc. All rights reserved.
"""Restricted ReceptorDB client.

For receptordb.com users — exposes only the receptor browse / search / download
surface plus tier-gated fold/generate. Endpoints not in the ReceptorDB subset
raise :class:`NotSupportedOnReceptorDB`.

See ``wiki/synthesis/receptordb_subset_plan.md`` for the canonical subset.
"""

from __future__ import annotations

import os
from typing import TYPE_CHECKING, Any

import httpx

from ligandai._constants import (
    DEFAULT_MAX_RETRIES,
    DEFAULT_RECEPTORDB_URL,
    DEFAULT_TIMEOUT_SECS,
)
from ligandai._http import AsyncHTTPTransport, HTTPTransport
from ligandai.errors import NotSupportedOnReceptorDB
from ligandai.resources.peptides import AsyncPeptides, Peptides
from ligandai.resources.receptors import AsyncReceptors, Receptors
from ligandai.resources.structures import AsyncStructures, Structures

if TYPE_CHECKING:
    pass


def _resolve_api_key(api_key: str | None) -> str | None:
    if api_key:
        return api_key
    return os.environ.get("RECEPTORDB_API_KEY") or os.environ.get("LIGANDAI_API_KEY")


class ReceptorDBClient:
    """Synchronous ReceptorDB-restricted client.

    Public read endpoints (search, browse, download) work without an API key.
    Fold and generate require a tier-appropriate API key (basic+).

    Parameters
    ----------
    api_key
        API key for fold/generate. Defaults to ``RECEPTORDB_API_KEY`` env var.
    base_url
        Defaults to ``https://receptordb.com``.
    """

    def __init__(
        self,
        api_key: str | None = None,
        *,
        base_url: str = DEFAULT_RECEPTORDB_URL,
        timeout: float = DEFAULT_TIMEOUT_SECS,
        max_retries: int = DEFAULT_MAX_RETRIES,
        http_client: httpx.Client | None = None,
    ) -> None:
        resolved = _resolve_api_key(api_key)
        self._api_key = resolved
        self._base_url = base_url
        self._transport = HTTPTransport(
            api_key=resolved,
            base_url=base_url,
            timeout=timeout,
            max_retries=max_retries,
            client=http_client,
        )
        self.receptors: Receptors = Receptors(self._transport)
        self.structures: Structures = Structures(self._transport)
        # Peptides is gated — calls raise NotSupportedOnReceptorDB if no API key
        # but can otherwise pass through to fold/generate.
        self._peptides: Peptides = Peptides(self._transport)

    @property
    def api_key(self) -> str | None:
        return self._api_key

    @property
    def base_url(self) -> str:
        return self._base_url

    # -- Convenience pass-throughs --

    def search(self, query: str, **kwargs: Any) -> Any:
        return self.receptors.search(query, **kwargs)

    def get(self, complex_id: str) -> Any:
        return self.receptors.get(complex_id)

    def list(self, **kwargs: Any) -> Any:
        return self.receptors.list(**kwargs)

    def by_gene(self, gene: str) -> Any:
        return self.receptors.by_gene(gene)

    def download_pdb(self, complex_id: str, dest: Any) -> Any:
        return self.receptors.download_pdb(complex_id, dest)

    def fold(self, *args: Any, **kwargs: Any) -> Any:
        if not self._api_key:
            raise NotSupportedOnReceptorDB(
                "Folding requires an API key. Pass api_key= to ReceptorDBClient or "
                "set RECEPTORDB_API_KEY."
            )
        return self._peptides.fold(*args, **kwargs)

    def generate(self, *args: Any, **kwargs: Any) -> Any:
        if not self._api_key:
            raise NotSupportedOnReceptorDB(
                "Generation requires an API key. Pass api_key= to ReceptorDBClient or "
                "set RECEPTORDB_API_KEY."
            )
        return self._peptides.generate(*args, **kwargs)

    # -- Lifecycle --

    def close(self) -> None:
        self._transport.close()

    def __enter__(self) -> ReceptorDBClient:
        return self

    def __exit__(self, *exc: Any) -> None:
        self.close()


class AsyncReceptorDBClient:
    """Async sibling of :class:`ReceptorDBClient`."""

    def __init__(
        self,
        api_key: str | None = None,
        *,
        base_url: str = DEFAULT_RECEPTORDB_URL,
        timeout: float = DEFAULT_TIMEOUT_SECS,
        max_retries: int = DEFAULT_MAX_RETRIES,
        http_client: httpx.AsyncClient | None = None,
    ) -> None:
        resolved = _resolve_api_key(api_key)
        self._api_key = resolved
        self._base_url = base_url
        self._transport = AsyncHTTPTransport(
            api_key=resolved,
            base_url=base_url,
            timeout=timeout,
            max_retries=max_retries,
            client=http_client,
        )
        self.receptors: AsyncReceptors = AsyncReceptors(self._transport)
        self.structures: AsyncStructures = AsyncStructures(self._transport)
        self._peptides: AsyncPeptides = AsyncPeptides(self._transport)

    @property
    def api_key(self) -> str | None:
        return self._api_key

    async def search(self, query: str, **kwargs: Any) -> Any:
        return await self.receptors.search(query, **kwargs)

    async def get(self, complex_id: str) -> Any:
        return await self.receptors.get(complex_id)

    async def fold(self, *args: Any, **kwargs: Any) -> Any:
        if not self._api_key:
            raise NotSupportedOnReceptorDB(
                "Folding requires an API key. Pass api_key= to AsyncReceptorDBClient."
            )
        return await self._peptides.fold(*args, **kwargs)

    async def generate(self, *args: Any, **kwargs: Any) -> Any:
        if not self._api_key:
            raise NotSupportedOnReceptorDB(
                "Generation requires an API key. Pass api_key= to AsyncReceptorDBClient."
            )
        return await self._peptides.generate(*args, **kwargs)

    async def close(self) -> None:
        await self._transport.close()

    async def __aenter__(self) -> AsyncReceptorDBClient:
        return self

    async def __aexit__(self, *exc: Any) -> None:
        await self.close()
