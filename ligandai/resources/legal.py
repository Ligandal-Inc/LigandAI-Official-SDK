# Copyright © 2026 Ligandal, Inc. All rights reserved.
"""Programmatic Terms of Service / EULA review + acceptance.

The legal endpoints are API-key reachable (server ``isAuthenticated`` accepts
``lgai_`` keys), so SDK/agent consumers can review and accept the current legal
terms without a browser. Version metadata and the document text come from
unauthed endpoints; status/accept require the API key.

Wired onto :class:`~ligandai.client.LigandAI` and
:class:`~ligandai.client.AsyncLigandAI` as ``client.legal`` the same way the
other resource namespaces are (``self._transport.request(...)``).
[bd-LIGANDAI_ALPHA_V2-yj7gp][bd-LIGANDAI_ALPHA_V2-ntmuv]
"""

from __future__ import annotations

from typing import Any

from ligandai.resources._base import AsyncResource, Resource

# Canonical document-type aliases the review endpoint understands.
_DOC_TYPES = {"tos", "eula"}


class Legal(Resource):
    """Resource client for programmatic ToS/EULA review + acceptance."""

    def versions(self) -> dict[str, Any]:
        """Current ToS + EULA version metadata (GET /api/legal/versions).

        Returns a dict with ``tos`` and ``eula`` keys, each either ``None`` or a
        metadata dict (``version``, ``effectiveDate``, ``summary``,
        ``isMaterialChange``). This does NOT include the document text — use
        :meth:`document` to read the full terms.
        """
        return self._transport.request("GET", "/api/legal/versions") or {}

    def document(self, type: str) -> dict[str, Any]:
        """Full text of a legal document for REVIEW (GET /api/legal/document/:type).

        Args:
            type: ``"tos"`` or ``"eula"``.

        Returns:
            Dict with ``type``, ``version``, ``effectiveDate`` and
            ``contentMarkdown`` (the full document body as markdown).
        """
        doc_type = str(type or "").strip().lower()
        if doc_type not in _DOC_TYPES:
            raise ValueError("type must be 'tos' or 'eula'")
        return self._transport.request("GET", f"/api/legal/document/{doc_type}") or {}

    def status(self) -> dict[str, Any]:
        """The current user's acceptance status (GET /api/legal/status).

        Requires an API key. Richer than ``/api/user/tos-status``: returns
        ``tosAccepted`` / ``eulaAccepted``, the user's accepted versions
        (``userAcceptedVersions``), the ``currentVersions``, and a
        ``needsReAcceptance`` flag.
        """
        return self._transport.request("GET", "/api/legal/status") or {}

    def accept(self, tos_version: str, eula_version: str) -> dict[str, Any]:
        """Record acceptance of the given ToS + EULA versions
        (POST /api/user/accept-tos).

        Requires an API key. Both versions are required by the server; fetch
        them from :meth:`versions` or from ``status()['currentVersions']``.

        Args:
            tos_version: The Terms of Service version being accepted.
            eula_version: The EULA version being accepted.

        Returns:
            Dict with ``success``, ``message``, ``tos_version``,
            ``eula_version`` and ``accepted_at``.
        """
        if not tos_version or not eula_version:
            raise ValueError("tos_version and eula_version are required")
        payload = {"tos_version": str(tos_version), "eula_version": str(eula_version)}
        return self._transport.request("POST", "/api/user/accept-tos", json=payload) or {}

    def accept_current(self) -> dict[str, Any]:
        """Convenience: fetch the current versions and accept them in one call.

        Equivalent to ``accept()`` seeded from ``versions()``.
        """
        versions = self.versions() or {}
        tos = (versions.get("tos") or {}).get("version")
        eula = (versions.get("eula") or {}).get("version")
        if not tos or not eula:
            raise ValueError(
                "No current ToS/EULA version is configured on the server; cannot accept."
            )
        return self.accept(tos_version=tos, eula_version=eula)


class AsyncLegal(AsyncResource):
    """Async resource client for programmatic ToS/EULA review + acceptance."""

    async def versions(self) -> dict[str, Any]:
        """Current ToS + EULA version metadata (GET /api/legal/versions)."""
        return await self._transport.request("GET", "/api/legal/versions") or {}

    async def document(self, type: str) -> dict[str, Any]:
        """Full text of a legal document for REVIEW (GET /api/legal/document/:type)."""
        doc_type = str(type or "").strip().lower()
        if doc_type not in _DOC_TYPES:
            raise ValueError("type must be 'tos' or 'eula'")
        return await self._transport.request("GET", f"/api/legal/document/{doc_type}") or {}

    async def status(self) -> dict[str, Any]:
        """The current user's acceptance status (GET /api/legal/status)."""
        return await self._transport.request("GET", "/api/legal/status") or {}

    async def accept(self, tos_version: str, eula_version: str) -> dict[str, Any]:
        """Record acceptance of the given ToS + EULA versions
        (POST /api/user/accept-tos)."""
        if not tos_version or not eula_version:
            raise ValueError("tos_version and eula_version are required")
        payload = {"tos_version": str(tos_version), "eula_version": str(eula_version)}
        return await self._transport.request("POST", "/api/user/accept-tos", json=payload) or {}

    async def accept_current(self) -> dict[str, Any]:
        """Convenience: fetch the current versions and accept them in one call."""
        versions = await self.versions() or {}
        tos = (versions.get("tos") or {}).get("version")
        eula = (versions.get("eula") or {}).get("version")
        if not tos or not eula:
            raise ValueError(
                "No current ToS/EULA version is configured on the server; cannot accept."
            )
        return await self.accept(tos_version=tos, eula_version=eula)
