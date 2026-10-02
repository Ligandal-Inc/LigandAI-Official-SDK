# Copyright © 2026 Ligandal, Inc. All rights reserved.
"""Resource base classes.

Resource modules subclass ``Resource`` (sync) or ``AsyncResource`` (async).
They share a transport reference and a hold on the parent client (when the
methods need tier checks).
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any, Sequence

from ligandai._http import AsyncHTTPTransport, HTTPTransport
from ligandai.errors import LigandAIUnreadableResponse

if TYPE_CHECKING:
    from ligandai.client import AsyncLigandAI, LigandAI


def unwrap_rows(
    payload: Any,
    keys: Sequence[str],
    *,
    what: str,
) -> list[Any]:
    """Rows out of a body that is either a bare list or a one-key envelope.

    ⛔ WHY THIS EXISTS. Every list-returning method in this SDK was written as
    ``items = payload if isinstance(payload, list) else payload.get("<key>", [])``
    with ``<key>`` GUESSED at authoring time. When the guess is wrong the
    method answers ``[]`` — a clean, plausible, wrong measurement of zero that
    is indistinguishable from "the server holds nothing". Measured live against
    blue on 2026-10-02: ``receptors.search("EGFR")`` returned ``[]`` while
    ``GET /api/receptordb/search`` was answering ``{"complexes": [38 rows]}``;
    the same shape of mistake was silently emptying ``diseases.search``,
    ``memory.search``/``list``, ``discovery.organ_systems``,
    ``peptides.search_by_pocket`` and ``peptides.get_elite``.
    [bd-dre-3imtv.3; the npm SDK's own fix is 32f3afc / bd-dre-3imtv.1]

    So: ``keys`` are tried in order and the FIRST one holding a list wins —
    put the key the live server actually sends first and keep the older ones
    behind it for servers that have not been updated. A body carrying NONE of
    them is a server this SDK does not understand, which is raised, never
    reported as zero rows.

    :param payload: decoded JSON body. Pass it RAW — do not spell
        ``... or []`` at the call site, or a ``None`` body becomes a zero
        before this function can refuse it.
    :param keys: envelope keys to try, highest-confidence first.
    :param what: what was being read, for the message (e.g.
        ``'GET /api/receptordb/search for "EGFR"'``).
    :raises LigandAIUnreadableResponse: body is ``None``, not a list/dict, or
        a dict carrying none of ``keys``.
    """
    if isinstance(payload, list):
        return payload
    if isinstance(payload, dict):
        for key in keys:
            value = payload.get(key)
            if isinstance(value, list):
                return value
        present = ", ".join(sorted(payload)) or "none"
        raise LigandAIUnreadableResponse(
            f"{what} returned a body carrying none of {'/'.join(keys)} "
            f"(keys present: {present}). That is NOT \"zero rows\" — this SDK "
            f"does not recognise the response shape, so it refuses to report a "
            f"measurement of zero.",
            cause="unrecognised_envelope",
            response=payload,
        )
    if payload is None:
        raise LigandAIUnreadableResponse(
            f"{what} returned no body. That is NOT \"zero rows\".",
            cause="empty_body",
        )
    raise LigandAIUnreadableResponse(
        f"{what} returned a {type(payload).__name__} where a list or an "
        f"envelope carrying {'/'.join(keys)} was required. That is NOT "
        f'"zero rows".',
        cause="unrecognised_envelope",
    )


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
