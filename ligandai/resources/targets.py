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
import hashlib
import re

from ligandai.errors import LigandAIValidationError
from ligandai.resources._base import AsyncResource, Resource

#: Name-match tiers, strongest first. `custom_name`/`gene_label` is what the
#: user typed; `filename_stem` is what they happened to call the file.
NAME_MATCH_TIERS: tuple[str, ...] = ("gene_label", "gene", "filename_stem")


def _structure_params(name, upload_id, expected_sha256):
    if (name is None) == (upload_id is None):
        raise LigandAIValidationError("Provide exactly one of name or upload_id.")
    params = {}
    if name is not None:
        params["name"] = _require_name(name)
    if upload_id is not None:
        if not isinstance(upload_id, str) or not re.fullmatch(r"(?:userpdb|userprotein)_[1-9]\d*", upload_id):
            raise LigandAIValidationError("upload_id must be store-qualified, e.g. userprotein_123.")
        params["uploadId"] = upload_id
    if expected_sha256 is not None:
        if not isinstance(expected_sha256, str) or not re.fullmatch(r"[a-fA-F0-9]{64}", expected_sha256):
            raise LigandAIValidationError("expected_sha256 must be a 64-character SHA-256 hex digest.")
        params["expectedSha256"] = expected_sha256.lower()
    return params


class SavedTargetStructure:
    """Exact saved PDB/mmCIF text, verified against its returned content hash.

    This is a user's upload, not a gene-level replacement. Residue numbers and
    chains remain as stored. Pin ``upload_id`` and ``content_sha256`` for reuse.
    ``format`` is ``pdb`` or ``cif``; ``content`` is never converted or trimmed.
    """

    def __init__(self, payload: Any, *, params: dict[str, str]) -> None:
        if not isinstance(payload, dict):
            raise LigandAIValidationError("Saved structure response was not an object.")
        content, digest, upload_id = (payload.get(k) for k in ("content", "contentSha256", "uploadId"))
        if (payload.get("source") != "user_upload" or
            payload.get("format") not in ("pdb", "cif") or
            not isinstance(content, str) or not content or
            not isinstance(upload_id, str) or
            not re.fullmatch(r"(?:userpdb|userprotein)_[1-9]\d*", upload_id) or
            digest != hashlib.sha256(content.encode("utf-8")).hexdigest() or
            payload.get("byteLength") != len(content.encode("utf-8")) or
            ("uploadId" in params and upload_id != params["uploadId"]) or
            ("name" in params and payload.get("requestedName") != params["name"]) or
            ("expectedSha256" in params and digest != params["expectedSha256"])):
            raise LigandAIValidationError("Saved structure identity or content hash did not match. No canonical fallback was accepted.")
        self.content: str = content
        self.format: str = payload["format"]
        self.upload_id: str = upload_id
        self.content_sha256: str = digest
        self.gene: str | None = payload.get("gene")
        self.choice: dict[str, Any] | None = payload.get("choice")


def _require_name(name: str) -> str:
    if not isinstance(name, str) or not name.strip():
        raise LigandAIValidationError(
            "name must be a non-empty string — an empty name is not a wildcard "
            "that matches every upload."
        )
    return name.strip()


class NamedTarget:
    """One addressable structure and every name it answers to."""

    __slots__ = (
        "store", "id", "upload_id", "names", "content_sha256",
        "duplicate_count", "duplicate_ids", "size_bytes", "chain_count",
        "residue_count", "uploaded_at", "chosen", "_raw",
    )

    def __init__(self, payload: dict[str, Any]) -> None:
        self._raw = payload
        self.store: str | None = payload.get("store")
        self.id: int | None = payload.get("id")
        #: Pass THIS as ``user_pdb_id=`` to target the structure unambiguously.
        self.upload_id: str | None = payload.get("uploadId")
        #: ``[{"name": ..., "tier": ...}, ...]``, strongest tier first.
        self.names: list[dict[str, Any]] = list(payload.get("names") or [])
        self.content_sha256: str | None = payload.get("contentSha256")
        #: [bd-etbo3] Byte-identical re-uploads collapsed into this entry. The
        #: index lists one entry per DISTINCT STRUCTURE, so an account with 41
        #: rows under one name shows the 3 structures and counts the 38.
        self.duplicate_count: int = int(payload.get("duplicateCount") or 0)
        self.duplicate_ids: list[int] = list(payload.get("duplicateIds") or [])
        self.size_bytes: int | None = payload.get("sizeBytes")
        self.chain_count: int | None = payload.get("chainCount")
        self.residue_count: int | None = payload.get("residueCount")
        self.uploaded_at: str | None = payload.get("uploadedAt")
        #: True when a saved choice points at this structure.
        self.chosen: bool = bool(payload.get("chosen"))

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

    __slots__ = ("targets", "count", "ambiguous_names", "ambiguous", "choices", "_raw")

    def __init__(self, payload: dict[str, Any]) -> None:
        self._raw = payload
        self.targets: list[NamedTarget] = [
            NamedTarget(t) for t in (payload.get("targets") or [])
        ]
        self.count: int = int(payload.get("count") or len(self.targets))
        #: Normalized names that match more than one upload. Each is an error
        #: waiting to happen — fix them before a campaign inherits one.
        self.ambiguous_names: list[str] = [
            a if isinstance(a, str) else str(a.get("name"))
            for a in (payload.get("ambiguousNames") or [])
        ]
        #: [bd-etbo3] The same collisions, with how many DISTINCT structures each
        #: covers and which one you already chose — so a collision you have
        #: already answered is not shown as a pending problem.
        self.ambiguous: list[dict[str, Any]] = list(payload.get("ambiguous") or [])
        #: Every saved pick on this account.
        self.choices: list[TargetNameChoice] = [
            TargetNameChoice(c) for c in (payload.get("choices") or [])
        ]

    def __len__(self) -> int:
        return len(self.targets)

    def __iter__(self):
        return iter(self.targets)

    def names(self) -> list[str]:
        """Flat list of every addressable name."""
        return [e["name"] for t in self.targets for e in t.names if e.get("name")]

    def __repr__(self) -> str:  # pragma: no cover - debug aid
        return f"<TargetIndex {self.count} targets, {len(self.ambiguous_names)} ambiguous>"


class TargetCandidate:
    """One structure you can choose between, and everything needed to choose it.

    [bd-LIGANDAI_ALPHA_V2-etbo3]

    When a name matches several of your uploads the platform refuses rather than
    guessing — a silently-picked conformer folds, scores and reaches a results
    table looking exactly like a correct one. A refusal is only useful if it can
    be *answered*, so each candidate carries both halves:

    * :attr:`upload_id` — the store-qualified id to re-call with, and
    * enough to tell the structures apart without a second round trip:
      :attr:`name`, :attr:`residue_count`, :attr:`chain_count`,
      :attr:`size_bytes`, :attr:`uploaded_at`, :attr:`content_sha256`.

    One entry per DISTINCT structure. Byte-identical re-uploads are collapsed
    into their representative and counted on :attr:`duplicate_count` — the
    account this was built for has 41 rows named ``BRD4ET`` which are 3
    structures and 38 re-uploads, and a 41-entry list is not a choice anyone can
    make.

    Anything the store could not answer is ``None``, never ``0``. ``"— residues"``
    is true; ``"0 residues"`` is a number you would act on.
    """

    __slots__ = (
        "upload_id", "store", "id", "name", "matched_on", "matched_value",
        "custom_name", "original_name", "gene", "content_sha256", "size_bytes",
        "chain_count", "residue_count", "uploaded_at", "duplicate_count",
        "duplicate_ids", "aliases", "chosen", "_raw",
    )

    def __init__(self, payload: dict[str, Any]) -> None:
        self._raw = payload
        #: ⛔ Pass THIS as ``user_pdb_id=`` to target this structure exactly.
        self.upload_id: str | None = payload.get("uploadId") or payload.get("upload_id")
        self.store: str | None = payload.get("store")
        self.id: int | None = payload.get("id")
        #: The most specific label its owner gave it that is NOT the ambiguous
        #: name — i.e. the string that actually distinguishes this one.
        self.name: str | None = payload.get("name")
        self.matched_on: str | None = payload.get("matchedOn") or payload.get("matched_on")
        self.matched_value: str | None = payload.get("matchedValue") or payload.get("matched_value")
        self.custom_name: str | None = payload.get("customName") or payload.get("custom_name")
        self.original_name: str | None = payload.get("originalName") or payload.get("original_name")
        self.gene: str | None = payload.get("gene")
        self.content_sha256: str | None = payload.get("contentSha256") or payload.get("content_sha256")
        self.size_bytes: int | None = payload.get("sizeBytes", payload.get("size_bytes"))
        self.chain_count: int | None = payload.get("chainCount", payload.get("chain_count"))
        self.residue_count: int | None = payload.get("residueCount", payload.get("residue_count"))
        self.uploaded_at: str | None = payload.get("uploadedAt") or payload.get("uploaded_at")
        #: Byte-identical re-uploads folded into this entry.
        self.duplicate_count: int = int(payload.get("duplicateCount") or payload.get("duplicate_count") or 0)
        self.duplicate_ids: list[int] = list(payload.get("duplicateIds") or payload.get("duplicate_ids") or [])
        #: Other names the folded rows answer to that this one does not.
        #: ⛔ Collapsing duplicates must never make a name unaddressable — 24 of
        #: the rows behind one of these structures carry three different labels,
        #: and all three resolve.
        self.aliases: list[str] = list(payload.get("aliases") or [])
        #: True when this is the choice you saved for the name.
        self.chosen: bool = bool(payload.get("chosen"))

    def __repr__(self) -> str:  # pragma: no cover - debug aid
        extra = f" +{self.duplicate_count} dup" if self.duplicate_count else ""
        return f"<TargetCandidate {self.upload_id} {self.name!r}{extra}>"


class TargetNameChoice:
    """Which structure you decided one of your own names means.

    [bd-LIGANDAI_ALPHA_V2-etbo3]

    ⛔ A choice is never inferred. It exists only because you made it, it is
    re-validated against the live candidates on every resolution (a deleted or
    renamed structure makes it stale, and a stale choice refuses rather than
    falling through to a neighbour), and it is always visible and always
    clearable. That is the whole difference between remembering your decision
    and silently defaulting to one.
    """

    __slots__ = (
        "name", "normalized_name", "upload_id", "store", "id",
        "content_sha256", "source", "chosen_at", "_raw",
    )

    def __init__(self, payload: dict[str, Any]) -> None:
        self._raw = payload
        self.name: str | None = payload.get("requestedName") or payload.get("name")
        self.normalized_name: str | None = payload.get("normalizedName") or payload.get("normalized_name")
        self.upload_id: str | None = payload.get("uploadId") or payload.get("upload_id")
        self.store: str | None = payload.get("store")
        self.id: int | None = payload.get("rowId", payload.get("id"))
        self.content_sha256: str | None = payload.get("contentSha256") or payload.get("content_sha256")
        #: Only ever ``"user"``. There is deliberately no inference source.
        self.source: str | None = payload.get("source")
        self.chosen_at: str | None = payload.get("chosenAt") or payload.get("chosen_at")

    def __repr__(self) -> str:  # pragma: no cover - debug aid
        return f"<TargetNameChoice {self.name!r} -> {self.upload_id}>"


class TargetResolution:
    """What one exact name string will resolve to."""

    __slots__ = (
        "name", "resolves_to", "gene", "outcome", "upload", "candidates",
        "duplicates_folded", "reason", "choice", "stale_choice",
        "matched_row_count", "_raw",
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
        #: One :class:`TargetCandidate` per DISTINCT structure this name matches
        #: — never one per duplicate row. [bd-etbo3]
        self.candidates: list[TargetCandidate] = [
            TargetCandidate(c) for c in (payload.get("candidates") or [])
        ]
        self.duplicates_folded: list[int] = list(payload.get("duplicatesFolded") or [])
        self.reason: str | None = payload.get("reason")
        #: Your saved pick for this name, or ``None``. When set, ``outcome`` is
        #: still ``"ambiguous"`` on purpose — the name IS ambiguous; the choice
        #: is how it gets answered, not evidence it never was.
        self.choice: TargetNameChoice | None = (
            TargetNameChoice(payload["choice"]) if isinstance(payload.get("choice"), dict) else None
        )
        #: Set when a saved choice no longer names one of the live candidates.
        #: The name refuses; this says why, instead of silently asking again.
        self.stale_choice: TargetNameChoice | None = (
            TargetNameChoice(payload["staleChoice"]) if isinstance(payload.get("staleChoice"), dict) else None
        )
        #: Rows matched before byte-identical duplicates were collapsed.
        self.matched_row_count: int | None = payload.get("matchedRowCount")

    @property
    def usable(self) -> bool:
        """True when this name targets something on its own.

        ``False`` for ``ambiguous`` — deliberately. An ambiguous name is not a
        name you can pass; pass ``upload_id`` from one of ``candidates``, or
        save a choice with :meth:`Targets.choose` and it becomes usable.

        An ambiguous name WITH a live saved choice is usable, because the choice
        is what it resolves through. [bd-etbo3]
        """
        if self.outcome == "ambiguous":
            return self.choice is not None
        return self.resolves_to in ("gene", "upload")

    @property
    def upload_id(self) -> str | None:
        """The store-qualified id to pass as ``user_pdb_id=``, when resolved."""
        return (self.upload or {}).get("uploadId")

    def __repr__(self) -> str:  # pragma: no cover - debug aid
        return f"<TargetResolution {self.name!r} -> {self.resolves_to} ({self.outcome})>"


class Targets(Resource):
    """The index of names your account can target."""

    def structure(self, name: str | None = None, *, upload_id: str | None = None,
                  expected_sha256: str | None = None) -> SavedTargetStructure:
        """Download your saved construct, never the public gene structure.

        Supply either a saved name or an exact store-qualified upload ID.
        Ambiguous names return HTTP 409 with candidates; choose explicitly via
        ``candidates()`` then use ``upload_id``. Pass ``expected_sha256`` on
        subsequent downloads to refuse changed content. No folding is launched.
        """
        params = _structure_params(name, upload_id, expected_sha256)
        payload = self._transport.request("GET", "/api/targets/structure", params=params)
        return SavedTargetStructure(payload, params=params)

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

    def candidates(self, name: str) -> list[TargetCandidate]:
        """The DISTINCT structures ``name`` matches — look before you call.

        [bd-LIGANDAI_ALPHA_V2-etbo3] The one-call way to see what a name means
        on your account without submitting anything::

            for c in client.targets.candidates("BRD4ET"):
                print(c.upload_id, c.name, c.residue_count, c.size_bytes,
                      f"+{c.duplicate_count} identical re-uploads")

        Byte-identical re-uploads are collapsed into their representative, so
        this returns the 3 structures rather than the 41 rows. Empty when the
        name is unambiguous or matches nothing.
        """
        return self.resolve(name).candidates

    # ── the saved choice ────────────────────────────────────────────────────
    # ⛔ A choice is a DECISION YOU MADE, never an inference. It is visible
    # (`choice`), reversible (`clear_choice`), and re-validated against the live
    # candidates on every resolution — a structure that is deleted or renamed
    # makes the choice stale, and a stale choice refuses rather than quietly
    # resolving to a neighbour.

    def choose(self, name: str, upload_id: str) -> TargetNameChoice:
        """Remember which structure ``name`` means, so you stop being asked.

        ::

            try:
                job = client.peptides.generate(gene="BRD4ET", num_peptides=200)
            except LigandAIAmbiguousTarget as e:
                client.targets.choose("BRD4ET", e.candidates[2].upload_id)
                job = client.peptides.generate(gene="BRD4ET", num_peptides=200)

        ``upload_id`` must be store-qualified (``"userprotein_598"`` /
        ``"userpdb_12"``) exactly as a candidate reports it. A bare integer is
        rejected: the two upload stores have independent id sequences and 31
        numeric ids exist in BOTH pointing at different structures.
        """
        wanted = _require_name(name)
        if not isinstance(upload_id, str) or not upload_id.strip():
            raise LigandAIValidationError(
                "upload_id must be a store-qualified upload id, e.g. "
                '"userprotein_598" — take it from a candidate\'s .upload_id.'
            )
        payload = self._transport.request(
            "PUT", "/api/targets/choice",
            json={"name": wanted, "uploadId": upload_id.strip()},
        ) or {}
        body = payload.get("choice") if isinstance(payload, dict) else None
        return TargetNameChoice(body if isinstance(body, dict) else {})

    def choice(self, name: str) -> TargetNameChoice | None:
        """Your saved pick for one name, or ``None``."""
        return self.resolve(name).choice

    def choices(self) -> list[TargetNameChoice]:
        """Every saved pick on this account — nothing here is hidden."""
        payload = self._transport.request("GET", "/api/targets/choices") or {}
        rows = payload.get("choices") if isinstance(payload, dict) else None
        return [TargetNameChoice(c) for c in (rows or [])]

    def clear_choice(self, name: str) -> bool:
        """Forget a saved pick. The name goes back to asking."""
        wanted = _require_name(name)
        payload = self._transport.request(
            "DELETE", "/api/targets/choice", params={"name": wanted}
        ) or {}
        return bool(payload.get("cleared")) if isinstance(payload, dict) else False


class AsyncTargets(AsyncResource):
    async def structure(self, name: str | None = None, *, upload_id: str | None = None,
                        expected_sha256: str | None = None) -> SavedTargetStructure:
        """Async variant of :meth:`Targets.structure`."""
        params = _structure_params(name, upload_id, expected_sha256)
        payload = await self._transport.request("GET", "/api/targets/structure", params=params)
        return SavedTargetStructure(payload, params=params)

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

    async def candidates(self, name: str) -> list[TargetCandidate]:
        return (await self.resolve(name)).candidates

    async def choose(self, name: str, upload_id: str) -> TargetNameChoice:
        wanted = _require_name(name)
        if not isinstance(upload_id, str) or not upload_id.strip():
            raise LigandAIValidationError(
                "upload_id must be a store-qualified upload id, e.g. "
                '"userprotein_598" — take it from a candidate\'s .upload_id.'
            )
        payload = await self._transport.request(
            "PUT", "/api/targets/choice",
            json={"name": wanted, "uploadId": upload_id.strip()},
        ) or {}
        body = payload.get("choice") if isinstance(payload, dict) else None
        return TargetNameChoice(body if isinstance(body, dict) else {})

    async def choice(self, name: str) -> TargetNameChoice | None:
        return (await self.resolve(name)).choice

    async def choices(self) -> list[TargetNameChoice]:
        payload = await self._transport.request("GET", "/api/targets/choices") or {}
        rows = payload.get("choices") if isinstance(payload, dict) else None
        return [TargetNameChoice(c) for c in (rows or [])]

    async def clear_choice(self, name: str) -> bool:
        wanted = _require_name(name)
        payload = await self._transport.request(
            "DELETE", "/api/targets/choice", params={"name": wanted}
        ) or {}
        return bool(payload.get("cleared")) if isinstance(payload, dict) else False
