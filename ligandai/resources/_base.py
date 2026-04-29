# Copyright © 2025 Ligandal, Inc. All rights reserved.
"""Resource base classes.

Resource modules subclass ``Resource`` (sync) or ``AsyncResource`` (async).
They share a transport reference and a hold on the parent client (when the
methods need tier checks).
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from ligandai._http import AsyncHTTPTransport, HTTPTransport

if TYPE_CHECKING:
    from ligandai.client import AsyncLigandAI, LigandAI


class Resource:
    """Base for sync resource namespaces."""

    def __init__(self, transport: HTTPTransport, client: LigandAI | None = None) -> None:
        self._transport = transport
        self._client = client


class AsyncResource:
    """Base for async resource namespaces."""

    def __init__(
        self,
        transport: AsyncHTTPTransport,
        client: AsyncLigandAI | None = None,
    ) -> None:
        self._transport = transport
        self._client = client
