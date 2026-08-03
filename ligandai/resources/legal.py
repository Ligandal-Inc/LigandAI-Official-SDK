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

import asyncio
import time
import webbrowser
from collections.abc import Callable
from typing import Any

from ligandai._constants import (
    DEFAULT_LEGAL_POLL_INTERVAL_SECS,
    DEFAULT_LEGAL_POLL_TIMEOUT_SECS,
)
from ligandai._headless import is_noninteractive_environment
from ligandai.errors import LigandAILegalAcceptanceRequired
from ligandai.resources._base import AsyncResource, Resource

# Canonical document-type aliases the review endpoint understands.
_DOC_TYPES = {"tos", "eula"}


def _acceptance_satisfied(
    status: dict[str, Any], error: LigandAILegalAcceptanceRequired
) -> bool:
    """Whether a ``GET /api/legal/status`` payload shows every document the
    original 403 flagged as outstanding (``error.needs_tos`` /
    ``error.needs_eula``) has now been accepted."""
    tos_ok = (not error.needs_tos) or bool(status.get("tosAccepted"))
    eula_ok = (not error.needs_eula) or bool(status.get("eulaAccepted"))
    return tos_ok and eula_ok


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

        ⛔ The server does NOT trust these version strings. Per
        ``docs/clickwrap_legal_acceptance.md`` §3.6/Contract B, the SDK must
        not be able to assert a document version — the server resolves the
        *current* version itself and records acceptance against that,
        regardless of what is sent here. This method's signature is kept
        required for back-compat with existing callers (0.9.0 and earlier
        relied on client-supplied versions reaching the wire), but the
        values are effectively advisory/ignored server-side now. Prefer
        :meth:`accept_current` for new code — it makes the "there is no
        version to choose" contract explicit.

        Args:
            tos_version: The Terms of Service version being accepted.
                Ignored by the server; kept for signature back-compat.
            eula_version: The EULA version being accepted.
                Ignored by the server; kept for signature back-compat.

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

        Equivalent to ``accept()`` seeded from ``versions()``. The server
        resolves the current version itself regardless of what is sent —
        this just avoids making the caller look the version up by hand.
        """
        versions = self.versions() or {}
        tos = (versions.get("tos") or {}).get("version")
        eula = (versions.get("eula") or {}).get("version")
        if not tos or not eula:
            raise ValueError(
                "No current ToS/EULA version is configured on the server; cannot accept."
            )
        return self.accept(tos_version=tos, eula_version=eula)

    def resolve_interactively(
        self,
        error: LigandAILegalAcceptanceRequired,
        retry: Callable[[], Any] | None = None,
        *,
        auto_open_browser: bool = False,
        poll_interval: float = DEFAULT_LEGAL_POLL_INTERVAL_SECS,
        timeout: float = DEFAULT_LEGAL_POLL_TIMEOUT_SECS,
    ) -> Any:
        """Interactively resolve a :class:`LigandAILegalAcceptanceRequired` 403.

        The opt-in browser-redirect + poll-and-resume flow from
        ``docs/clickwrap_legal_acceptance.md`` §3.6. Nothing here runs
        automatically — the SDK never intercepts a failed request for you.
        Call this explicitly from your own ``except`` block::

            try:
                job = client.folds.predict(...)
            except LigandAILegalAcceptanceRequired as e:
                job = client.legal.resolve_interactively(
                    e,
                    retry=lambda: client.folds.predict(...),
                    auto_open_browser=True,
                )

        ⛔ Headless-safe by construction. Before doing anything else — before
        touching the network or ``webbrowser`` — this checks
        :func:`ligandai._headless.is_noninteractive_environment` (no TTY, no
        ``DISPLAY``/``WAYLAND_DISPLAY`` on Linux, or ``CI``/``GITHUB_ACTIONS``
        set) and, if so, re-raises ``error`` immediately. It is the *same*
        exception object, so ``error.acceptance_url`` is still attached for
        the caller to print/log — it never opens a browser or blocks polling
        in a non-interactive process.

        Args:
            error: The exception the failed request raised.
            retry: Optional zero-arg callable that repeats the original
                request. Called **at most once**, and only after acceptance
                has been confirmed via ``GET /api/legal/status``. If
                omitted, this method just confirms acceptance and returns
                ``True`` — the caller retries on their own.
            auto_open_browser: Opt-in, defaults to **False**. When ``True``,
                calls ``webbrowser.open(error.acceptance_url)``. A library
                that pops a browser window unprompted inside a server
                process is hostile — this stays off unless explicitly
                requested. When ``False``, the caller is responsible for
                getting ``error.acceptance_url`` in front of a human some
                other way (print it, log it, surface it in a UI) — this
                method still polls either way.
            poll_interval: Seconds between ``GET /api/legal/status`` polls.
            timeout: Hard wall-clock budget in seconds, always enforced —
                every wait in this flow is bounded, headless or not.

        Returns:
            The return value of ``retry()`` when provided, else ``True``.

        Raises:
            LigandAILegalAcceptanceRequired: the environment is
                non-interactive, ``auto_open_browser=True`` but
                ``error.acceptance_url`` is missing or ``webbrowser.open``
                returned ``False``/raised, or the poll loop exhausted
                ``timeout`` with no acceptance recorded. In every case the
                exception raised is the original ``error`` — never a new,
                context-free one.
        """
        if is_noninteractive_environment():
            raise error
        if auto_open_browser:
            if not error.acceptance_url:
                raise error
            try:
                opened = webbrowser.open(error.acceptance_url)
            except Exception:
                opened = False
            if not opened:
                raise error

        deadline = time.monotonic() + timeout
        while True:
            status = self.status()
            if _acceptance_satisfied(status, error):
                break
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise error
            time.sleep(min(poll_interval, remaining))

        if retry is not None:
            return retry()
        return True


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
        (POST /api/user/accept-tos).

        ⛔ Same non-assertion contract as :meth:`Legal.accept` (sync): the
        server ignores these version strings and resolves the current
        version itself (docs/clickwrap_legal_acceptance.md §3.6/Contract B).
        Signature kept for back-compat; prefer :meth:`accept_current`.
        """
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

    async def resolve_interactively(
        self,
        error: LigandAILegalAcceptanceRequired,
        retry: Callable[[], Any] | None = None,
        *,
        auto_open_browser: bool = False,
        poll_interval: float = DEFAULT_LEGAL_POLL_INTERVAL_SECS,
        timeout: float = DEFAULT_LEGAL_POLL_TIMEOUT_SECS,
    ) -> Any:
        """Async sibling of :meth:`Legal.resolve_interactively`. See that
        method for the full opt-in / headless-safe contract — identical here,
        just awaited. ``retry`` may be a sync or async zero-arg callable;
        an async result is awaited before returning.
        """
        if is_noninteractive_environment():
            raise error
        if auto_open_browser:
            if not error.acceptance_url:
                raise error
            try:
                opened = webbrowser.open(error.acceptance_url)
            except Exception:
                opened = False
            if not opened:
                raise error

        deadline = time.monotonic() + timeout
        while True:
            status = await self.status()
            if _acceptance_satisfied(status, error):
                break
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise error
            await asyncio.sleep(min(poll_interval, remaining))

        if retry is not None:
            result = retry()
            if asyncio.iscoroutine(result):
                result = await result
            return result
        return True
