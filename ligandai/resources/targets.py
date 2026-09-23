# Copyright © 2026 Ligandal, Inc. All rights reserved.
"""Named targets — addressing YOUR OWN uploaded structure by a name you chose.

[bd-LIGANDAI_ALPHA_V2-pw9mu]

Gene resolution answers one question: *is this a symbol in the global
gene/UniProt universe?* A domain-scale construct can only ever answer no. The
ET domain of BRD4 is a ~68-residue sub-domain, so ``"BRD4ET"`` is a real name
for a real structure and a hard 404 as a gene — which is why
``generate(gene="BRD4ET")`` used to fail with
``503 upstream_unavailable: resolver_error:ValueError`` and the only workaround
was to carry ``user_pdb_id`` on every single call.

Name an upload once and the name targets it::

    prot = client.proteins.upload_pdb(
        "Brd4ET.pdb", gene="BRD4", custom_name="BRD4ET",
    )
    job = client.peptides.generate(gene="BRD4ET", num_peptides=200)
    #                                    ^ no user_pdb_id needed

``client.targets.list()`` shows every name your account can target, and
``client.targets.resolve("BRD4ET")`` shows what one exact string will hit —
the same decision the generate path makes, so the two cannot disagree.

Three behaviours worth knowing before you rely on a name:

1. **The global gene universe wins.** Name an upload ``"BRD4"`` and ``BRD4``
   still means the gene. :meth:`Targets.resolve` reports the collision
   (``resolves_to == "gene"`` with ``candidates`` populated) so you can pass
   the upload id when you meant your own structure. A name never silently
   redefines a real gene for your account.

2. **Ambiguity refuses.** Two *different* structures under one name is an
   error, not a coin flip — the wrong structure would fold, score, and reach a
   results table looking exactly like a real answer. Byte-identical duplicates
   are folded together instead (same bytes, same answer).

3. **Names are tiered.** ``custom_name`` (what you typed) beats an inferred
   gene symbol, which beats the filename stem. A lower tier never competes
   with a higher one, so renaming a *file* cannot change which structure a
   campaign targets.
"""

from __future__ import annotations

from typing import Any

from ligandai.errors import LigandAIValidationError
from ligandai.resources._base import AsyncResource, Resource

#: Name-match tiers, strongest first. `custom_name`/`gene_label` is what the
#: user typed; `filename_stem` is what they happened to call the file.
NAME_MATCH_TIERS: tuple[str, ...] = ("gene_label", "gene", "filename_stem")


def _require_name(name: str) -> str:
    if not isinstance(name, str) or not name.strip():
        raise LigandAIValidationError(
            "name must be a non-empty string — an empty name is not a wildcard "
            "that matches every upload."
        )
    return name.strip()


class NamedTarget:
    """One addressable structure and every name it answers to."""

    __slots__ = ("store", "id", "upload_id", "names", "content_sha256", "_raw")

    def __init__(self, payload: dict[str, Any]) -> None:
        self._raw = payload
        self.store: str | None = payload.get("store")
        self.id: int | None = payload.get("id")
        #: Pass THIS as ``user_pdb_id=`` to target the structure unambiguously.
        self.upload_id: str | None = payload.get("uploadId")
        #: ``[{"name": ..., "tier": ...}, ...]``, strongest tier first.
        self.names: list[dict[str, Any]] = list(payload.get("names") or [])
        self.content_sha256: str | None = payload.get("contentSha256")

    @property
    def primary_name(self) -> str | None:
        """The strongest name for this structure — what to pass as ``gene=``."""
        for tier in NAME_MATCH_TIERS:
            for entry in self.names:
                if entry.get("tier") == tier:
                    return entry.get("name")
        return self.names[0].get("name") if self.names else None

    def __repr__(self) -> str:  # pragma: no cover - debug aid
        return f"<NamedTarget {self.primary_name!r} {self.upload_id}>"


class TargetIndex:
    """Every name this account can target, plus the collisions in it."""

    __slots__ = ("targets", "count", "ambiguous_names", "_raw")

    def __init__(self, payload: dict[str, Any]) -> None:
        self._raw = payload
        self.targets: list[NamedTarget] = [
            NamedTarget(t) for t in (payload.get("targets") or [])
        ]
        self.count: int = int(payload.get("count") or len(self.targets))
        #: Normalized names that match more than one upload. Each is an error
        #: waiting to happen — fix them before a campaign inherits one.
        self.ambiguous_names: list[str] = list(payload.get("ambiguousNames") or [])

    def __len__(self) -> int:
        return len(self.targets)

    def __iter__(self):
        return iter(self.targets)

    def names(self) -> list[str]:
        """Flat list of every addressable name."""
        return [e["name"] for t in self.targets for e in t.names if e.get("name")]

    def __repr__(self) -> str:  # pragma: no cover - debug aid
        return f"<TargetIndex {self.count} targets, {len(self.ambiguous_names)} ambiguous>"


class TargetResolution:
    """What one exact name string will resolve to."""

    __slots__ = (
        "name", "resolves_to", "gene", "outcome", "upload", "candidates",
        "duplicates_folded", "reason", "_raw",
    )

    def __init__(self, payload: dict[str, Any]) -> None:
        self._raw = payload
        self.name: str | None = payload.get("name")
        #: ``"gene"`` | ``"upload"`` | ``"nothing"``. ``"gene"`` means the
        #: global gene universe claimed this name — any ``candidates`` below
        #: are YOUR uploads that share it, reported so you can pass an id.
        self.resolves_to: str | None = payload.get("resolvesTo")
        self.gene: str | None = payload.get("gene")
        #: ``"resolved"`` | ``"ambiguous"`` | ``"no_match"`` — the verdict for
        #: the UPLOAD layer specifically, independent of ``gene``.
        self.outcome: str | None = payload.get("outcome")
        self.upload: dict[str, Any] | None = payload.get("upload")
        self.candidates: list[dict[str, Any]] = list(payload.get("candidates") or [])
        self.duplicates_folded: list[int] = list(payload.get("duplicatesFolded") or [])
        self.reason: str | None = payload.get("reason")

    @property
    def usable(self) -> bool:
        """True when this name targets something on its own.

        ``False`` for ``ambiguous`` — deliberately. An ambiguous name is not a
        name you can pass; pass ``upload_id`` from one of ``candidates``.
        """
        return self.resolves_to in ("gene", "upload") and self.outcome != "ambiguous"

    @property
    def upload_id(self) -> str | None:
        """The store-qualified id to pass as ``user_pdb_id=``, when resolved."""
        return (self.upload or {}).get("uploadId")

    def __repr__(self) -> str:  # pragma: no cover - debug aid
        return f"<TargetResolution {self.name!r} -> {self.resolves_to} ({self.outcome})>"


class Targets(Resource):
    """The index of names your account can target."""

    def list(self) -> TargetIndex:
        """Every addressable name on this account, with its match tier.

        ::

            idx = client.targets.list()
            for t in idx:
                print(t.primary_name, t.upload_id)
            if idx.ambiguous_names:
                print("fix these first:", idx.ambiguous_names)
        """
        payload = self._transport.request("GET", "/api/targets") or {}
        return TargetIndex(payload if isinstance(payload, dict) else {})

    def resolve(self, name: str) -> TargetResolution:
        """What will ``gene=<name>`` actually hit?

        Runs the SAME decision the generate path runs, so a name that resolves
        here resolves there. Use it to check a name before spending credits::

            r = client.targets.resolve("BRD4ET")
            assert r.usable, r.reason
        """
        wanted = _require_name(name)
        payload = self._transport.request(
            "GET", "/api/targets", params={"name": wanted}
        ) or {}
        return TargetResolution(payload if isinstance(payload, dict) else {})


class AsyncTargets(AsyncResource):
    """Async variant of :class:`Targets`."""

    async def list(self) -> TargetIndex:
        payload = await self._transport.request("GET", "/api/targets") or {}
        return TargetIndex(payload if isinstance(payload, dict) else {})

    async def resolve(self, name: str) -> TargetResolution:
        wanted = _require_name(name)
        payload = await self._transport.request(
            "GET", "/api/targets", params={"name": wanted}
        ) or {}
        return TargetResolution(payload if isinstance(payload, dict) else {})
