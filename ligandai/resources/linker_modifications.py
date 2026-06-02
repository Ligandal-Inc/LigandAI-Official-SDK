# Copyright © 2026 Ligandal, Inc. All rights reserved.
"""Linker modifications + UAA + Mode B payload optimization.

Tier: pro+ (academia, pro, pro_commercial, pro_beta, enterprise,
discovery_partner). Free and basic users receive 403 on these endpoints.

Two flows are exposed:

* **Mode A — Generation-loop linker modifications.** Submit a peptide design
  template with per-position UAA constraints. Each slot can be:
    - ``canonical`` (informational only),
    - ``fixed_uaa`` (one CCD code pinned),
    - ``uaa_allowed`` (generator chooses from a palette),
    - ``payload`` (lipid / PEG / PDC ligand attached via covalent bond).

* **Mode B — Post-fold payload optimization.** For an already-designed
  peptide, batch-fold N payload variants (cross-product of payload library
  filter and attachment residues) on Boltz-2 and rank the resulting
  complexes by predicted dG / iPSAE.

Endpoints:

- ``GET /api/v1/linker_modifications/uaa_palette``  (authenticated)
- ``POST /api/v1/linker_modifications/fold``        (pro+)
- ``GET /api/v1/payload_optimization/libraries``    (pro+)
- ``GET /api/v1/payload_optimization/libraries/:k`` (pro+)
- ``POST /api/v1/payload_optimization/runs``        (pro+)
- ``GET /api/v1/payload_optimization/runs/:id``     (pro+)
"""
from __future__ import annotations

import asyncio
import time
from dataclasses import asdict, dataclass, field
from typing import Any

from ligandai.resources._base import AsyncResource, Resource


_MOD_TYPES = ("canonical", "fixed_uaa", "uaa_allowed", "payload")


@dataclass
class CovalentAttachment:
    """Specifies where a payload SMILES attaches to the peptide.

    ``ligand_atom`` names an atom in the SMILES mol-block (e.g. ``"C1"`` for
    a terminal carboxyl carbon). ``protein_atom`` names the atom on the
    residue side chain (e.g. ``"NZ"`` for Lys epsilon-amine).
    """
    ligand_atom: str
    protein_atom: str

    def to_camel(self) -> dict[str, Any]:
        return {"ligandAtom": self.ligand_atom, "proteinAtom": self.protein_atom}


@dataclass
class LinkerModification:
    """One slot in a peptide design template.

    Parameters
    ----------
    position : int
        1-indexed residue position in the sequence template.
    mod_type : {'canonical','fixed_uaa','uaa_allowed','payload'}
        Slot kind.
    ccd_code : str, optional
        CCD code (PDB or custom). Required for ``fixed_uaa``; optional for
        ``uaa_allowed`` (generator picks). Ignored for ``payload``.
    smiles : str, optional
        Payload SMILES. Required for ``payload``. Ignored otherwise.
    allowed_uaa_palette : list[str], optional
        For ``uaa_allowed``: list of CCD codes the generator may pick.
    covalent_attachment : CovalentAttachment, optional
        Required for ``payload``.
    mw_expected : float, optional
        Client-asserted molecular weight; server verifies against the curated
        palette / RDKit. Mismatch -> 422 ``mw_verification_failed``.
    label : str, optional
        Free-text label for UI display.
    """
    position: int
    mod_type: str
    ccd_code: str | None = None
    smiles: str | None = None
    allowed_uaa_palette: list[str] | None = None
    covalent_attachment: CovalentAttachment | None = None
    mw_expected: float | None = None
    label: str | None = None

    def __post_init__(self) -> None:
        if self.mod_type not in _MOD_TYPES:
            raise ValueError(
                f"mod_type must be one of {_MOD_TYPES!r}; got {self.mod_type!r}"
            )
        if self.mod_type == "fixed_uaa" and not self.ccd_code:
            raise ValueError("mod_type=fixed_uaa requires ccd_code")
        if self.mod_type == "payload":
            if not self.smiles:
                raise ValueError("mod_type=payload requires smiles")
            if not self.covalent_attachment:
                raise ValueError("mod_type=payload requires covalent_attachment")
        if self.position < 1:
            raise ValueError("position must be 1-indexed (>=1)")

    def to_camel(self) -> dict[str, Any]:
        body: dict[str, Any] = {
            "position": self.position,
            "modType": self.mod_type,
            "ccdCode": self.ccd_code,
            "smiles": self.smiles,
            "allowedUaaPalette": self.allowed_uaa_palette,
            "covalentAttachment": (
                self.covalent_attachment.to_camel()
                if self.covalent_attachment
                else None
            ),
            "mwExpected": self.mw_expected,
            "label": self.label,
        }
        return {k: v for k, v in body.items() if v is not None}


@dataclass
class ReceptorChain:
    chain_id: str
    sequence: str

    def to_camel(self) -> dict[str, Any]:
        return {"chainId": self.chain_id, "sequence": self.sequence}


@dataclass
class PayloadFilter:
    category: str | None = None
    max_mw: float | None = None
    min_mw: float | None = None
    tags: list[str] | None = None

    def to_camel(self) -> dict[str, Any]:
        body: dict[str, Any] = {}
        if self.category is not None:
            body["category"] = self.category
        if self.max_mw is not None:
            body["maxMw"] = self.max_mw
        if self.min_mw is not None:
            body["minMw"] = self.min_mw
        if self.tags is not None:
            body["tags"] = self.tags
        return body


@dataclass
class PayloadOptimizationRun:
    run_id: str
    status: str
    parent_session_id: str | None = None
    variants_total: int | None = None
    variants_completed: int | None = None
    variants_failed: int | None = None
    ranked_variants: list[dict[str, Any]] | None = None
    error_message: str | None = None

    @property
    def is_terminal(self) -> bool:
        return self.status in ("completed", "failed", "cancelled")


class LinkerModifications(Resource):
    """``/api/v1/linker_modifications/*`` and ``/api/v1/payload_optimization/*``."""

    # ---- UAA palette ----

    def list_uaa_palette(self) -> list[dict[str, Any]]:
        """Return the curated UAA palette (authenticated; no tier gate)."""
        result = self._transport.request(
            "GET", "/api/v1/linker_modifications/uaa_palette",
        ) or {}
        return result.get("palette") or []

    # ---- Mode A: generation-loop fold with linker mods ----

    def fold_with_linker_mods(
        self,
        peptide_sequence: str,
        receptor_chains: list[ReceptorChain | dict[str, Any]],
        modifications: list[LinkerModification | dict[str, Any]],
        session_id: str,
        gene: str | None = None,
        sampling_steps: int = 50,
        num_trajectories: int = 4,
    ) -> dict[str, Any]:
        """Dispatch a Mode A fold on the sibling Boltz-2 FDA v2 app.

        ``modifications`` are submitted to the MW verification gate before
        any fold is dispatched. On gate failure, raises ``LigandAIError``
        with status 422 and the per-row rejection list in ``.detail``.
        """
        if self._client is not None:
            # Beta module: require pro feature. Server re-enforces at the route.
            self._client._require_feature("linker_modifications")
        body = {
            "sessionId": session_id,
            "peptide_sequence": peptide_sequence,
            "receptor_chains": [
                rc.to_camel() if isinstance(rc, ReceptorChain) else rc
                for rc in receptor_chains
            ],
            "linker_modifications": [
                m.to_camel() if isinstance(m, LinkerModification) else m
                for m in modifications
            ],
            "gene": gene,
            "sampling_steps": sampling_steps,
            "num_trajectories": num_trajectories,
        }
        body = {k: v for k, v in body.items() if v is not None}
        return self._transport.request(
            "POST", "/api/v1/linker_modifications/fold", json=body,
        ) or {}

    # ---- Mode B: payload optimization ----

    def list_payload_libraries(self) -> list[dict[str, Any]]:
        """List system-seeded payload libraries (lipid / PEG / PDC)."""
        if self._client is not None:
            self._client._require_feature("payload_optimization")
        result = self._transport.request(
            "GET", "/api/v1/payload_optimization/libraries",
        ) or {}
        return result.get("libraries") or []

    def get_payload_library(self, library_key: str) -> dict[str, Any]:
        """Return a single library with all entries."""
        if self._client is not None:
            self._client._require_feature("payload_optimization")
        return self._transport.request(
            "GET", f"/api/v1/payload_optimization/libraries/{library_key}",
        ) or {}

    def optimize_payload(
        self,
        peptide_sequence: str,
        receptor_chains: list[ReceptorChain | dict[str, Any]],
        attachment_residues: list[int],
        library_key: str,
        parent_session_id: str,
        peptide_id: int | None = None,
        payload_ids: list[str] | None = None,
        payload_filter: PayloadFilter | dict[str, Any] | None = None,
        max_variants: int = 12,
        gene: str | None = None,
        sampling_steps: int = 50,
        num_trajectories: int = 4,
    ) -> PayloadOptimizationRun:
        """Spawn a Mode B payload optimization run.

        Returns immediately with a :class:`PayloadOptimizationRun` whose
        ``status='running'``. Use :meth:`get_payload_run` or :meth:`wait`
        to poll for completion.
        """
        if self._client is not None:
            self._client._require_feature("payload_optimization")
        body = {
            "parentSessionId": parent_session_id,
            "peptideId": peptide_id,
            "peptideSequence": peptide_sequence,
            "receptorChains": [
                rc.to_camel() if isinstance(rc, ReceptorChain) else rc
                for rc in receptor_chains
            ],
            "attachmentResidues": attachment_residues,
            "libraryKey": library_key,
            "payloadIds": payload_ids,
            "payloadFilter": (
                payload_filter.to_camel()
                if isinstance(payload_filter, PayloadFilter)
                else payload_filter
            ),
            "maxVariants": max_variants,
            "gene": gene,
            "samplingSteps": sampling_steps,
            "numTrajectories": num_trajectories,
        }
        body = {k: v for k, v in body.items() if v is not None}
        payload = self._transport.request(
            "POST", "/api/v1/payload_optimization/runs", json=body,
        ) or {}
        return PayloadOptimizationRun(
            run_id=payload.get("runId") or payload.get("run_id") or "",
            status=payload.get("status") or "pending",
            parent_session_id=payload.get("parentSessionId") or parent_session_id,
            variants_total=payload.get("variantsTotal") or payload.get("variants_total"),
        )

    def get_payload_run(self, run_id: str) -> PayloadOptimizationRun:
        """Poll a Mode B run; returns enriched results when status='completed'."""
        if self._client is not None:
            self._client._require_feature("payload_optimization")
        payload = self._transport.request(
            "GET", f"/api/v1/payload_optimization/runs/{run_id}",
        ) or {}
        return PayloadOptimizationRun(
            run_id=payload.get("runId") or payload.get("run_id") or run_id,
            status=payload.get("status") or "unknown",
            variants_total=payload.get("variantsTotal") or payload.get("variants_total"),
            variants_completed=payload.get("variantsCompleted") or payload.get("variants_completed"),
            variants_failed=payload.get("variantsFailed") or payload.get("variants_failed"),
            ranked_variants=payload.get("rankedVariants") or payload.get("ranked_variants"),
            error_message=payload.get("errorMessage") or payload.get("error_message"),
        )

    def wait(
        self,
        run: PayloadOptimizationRun | str,
        poll_interval_s: float = 5.0,
        timeout_s: float = 1800.0,
    ) -> PayloadOptimizationRun:
        """Block until a Mode B run reaches a terminal state.

        Mirrors the ergonomics of :meth:`ligandai.jobs.Job.wait`. Returns the
        enriched run record (with ``ranked_variants`` populated when complete).
        """
        run_id = run.run_id if isinstance(run, PayloadOptimizationRun) else run
        start = time.monotonic()
        last: PayloadOptimizationRun | None = None
        while time.monotonic() - start < timeout_s:
            last = self.get_payload_run(run_id)
            if last.is_terminal:
                return last
            time.sleep(poll_interval_s)
        if last is None:
            last = self.get_payload_run(run_id)
        return last


class AsyncLinkerModifications(AsyncResource):
    """Async sibling of :class:`LinkerModifications`."""

    async def list_uaa_palette(self) -> list[dict[str, Any]]:
        result = await self._transport.request(
            "GET", "/api/v1/linker_modifications/uaa_palette",
        ) or {}
        return result.get("palette") or []

    async def fold_with_linker_mods(
        self,
        peptide_sequence: str,
        receptor_chains: list[ReceptorChain | dict[str, Any]],
        modifications: list[LinkerModification | dict[str, Any]],
        session_id: str,
        gene: str | None = None,
        sampling_steps: int = 50,
        num_trajectories: int = 4,
    ) -> dict[str, Any]:
        if self._client is not None:
            self._client._require_feature("linker_modifications")
        body = {
            "sessionId": session_id,
            "peptide_sequence": peptide_sequence,
            "receptor_chains": [
                rc.to_camel() if isinstance(rc, ReceptorChain) else rc
                for rc in receptor_chains
            ],
            "linker_modifications": [
                m.to_camel() if isinstance(m, LinkerModification) else m
                for m in modifications
            ],
            "gene": gene,
            "sampling_steps": sampling_steps,
            "num_trajectories": num_trajectories,
        }
        body = {k: v for k, v in body.items() if v is not None}
        return await self._transport.request(
            "POST", "/api/v1/linker_modifications/fold", json=body,
        ) or {}

    async def list_payload_libraries(self) -> list[dict[str, Any]]:
        if self._client is not None:
            self._client._require_feature("payload_optimization")
        result = await self._transport.request(
            "GET", "/api/v1/payload_optimization/libraries",
        ) or {}
        return result.get("libraries") or []

    async def get_payload_library(self, library_key: str) -> dict[str, Any]:
        if self._client is not None:
            self._client._require_feature("payload_optimization")
        return await self._transport.request(
            "GET", f"/api/v1/payload_optimization/libraries/{library_key}",
        ) or {}

    async def optimize_payload(
        self,
        peptide_sequence: str,
        receptor_chains: list[ReceptorChain | dict[str, Any]],
        attachment_residues: list[int],
        library_key: str,
        parent_session_id: str,
        peptide_id: int | None = None,
        payload_ids: list[str] | None = None,
        payload_filter: PayloadFilter | dict[str, Any] | None = None,
        max_variants: int = 12,
        gene: str | None = None,
        sampling_steps: int = 50,
        num_trajectories: int = 4,
    ) -> PayloadOptimizationRun:
        if self._client is not None:
            self._client._require_feature("payload_optimization")
        body = {
            "parentSessionId": parent_session_id,
            "peptideId": peptide_id,
            "peptideSequence": peptide_sequence,
            "receptorChains": [
                rc.to_camel() if isinstance(rc, ReceptorChain) else rc
                for rc in receptor_chains
            ],
            "attachmentResidues": attachment_residues,
            "libraryKey": library_key,
            "payloadIds": payload_ids,
            "payloadFilter": (
                payload_filter.to_camel()
                if isinstance(payload_filter, PayloadFilter)
                else payload_filter
            ),
            "maxVariants": max_variants,
            "gene": gene,
            "samplingSteps": sampling_steps,
            "numTrajectories": num_trajectories,
        }
        body = {k: v for k, v in body.items() if v is not None}
        payload = await self._transport.request(
            "POST", "/api/v1/payload_optimization/runs", json=body,
        ) or {}
        return PayloadOptimizationRun(
            run_id=payload.get("runId") or payload.get("run_id") or "",
            status=payload.get("status") or "pending",
            parent_session_id=payload.get("parentSessionId") or parent_session_id,
            variants_total=payload.get("variantsTotal") or payload.get("variants_total"),
        )

    async def get_payload_run(self, run_id: str) -> PayloadOptimizationRun:
        if self._client is not None:
            self._client._require_feature("payload_optimization")
        payload = await self._transport.request(
            "GET", f"/api/v1/payload_optimization/runs/{run_id}",
        ) or {}
        return PayloadOptimizationRun(
            run_id=payload.get("runId") or payload.get("run_id") or run_id,
            status=payload.get("status") or "unknown",
            variants_total=payload.get("variantsTotal") or payload.get("variants_total"),
            variants_completed=payload.get("variantsCompleted") or payload.get("variants_completed"),
            variants_failed=payload.get("variantsFailed") or payload.get("variants_failed"),
            ranked_variants=payload.get("rankedVariants") or payload.get("ranked_variants"),
            error_message=payload.get("errorMessage") or payload.get("error_message"),
        )

    async def wait(
        self,
        run: PayloadOptimizationRun | str,
        poll_interval_s: float = 5.0,
        timeout_s: float = 1800.0,
    ) -> PayloadOptimizationRun:
        run_id = run.run_id if isinstance(run, PayloadOptimizationRun) else run
        start = time.monotonic()
        last: PayloadOptimizationRun | None = None
        while time.monotonic() - start < timeout_s:
            last = await self.get_payload_run(run_id)
            if last.is_terminal:
                return last
            await asyncio.sleep(poll_interval_s)
        if last is None:
            last = await self.get_payload_run(run_id)
        return last


__all__ = [
    "CovalentAttachment",
    "LinkerModification",
    "ReceptorChain",
    "PayloadFilter",
    "PayloadOptimizationRun",
    "LinkerModifications",
    "AsyncLinkerModifications",
]
