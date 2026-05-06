# Copyright © 2025 Ligandal, Inc. All rights reserved.
"""Bivalent / bispecific peptide design (Beta).

This is a **niche, induced-proximity flow** — for forcing two SEPARATE proteins
that don't naturally interact into proximity via a single peptide that has two
binder domains joined by an optimised linker. Use cases: PROTAC-style induced
proximity, molecular-glue peptides, forced heterodimerisation.

If your target is itself a multimer (HER2 dimer, MHC class-II, antibody
Fab+Fc, CD8αβ), do NOT use this module — multi-chain receptors are first-class
via :class:`ligandai.resources.peptides.Peptides` with multi-chain target
input. That path is the documented default.

Two paradigms are supported (h1qv3):

**Mode 2 — Joint LEGO** (sequential generation: binder_T1 + linker mask, then
binder_T2):
    :meth:`Bivalent.start` → :meth:`Bivalent.run2` → :meth:`Bivalent.fold`

**Mode 1 — Generate-Then-Link** (parallel independent binders, then linker
optimisation between fixed termini):
    :meth:`Bivalent.run1_parallel` → :meth:`Bivalent.record_mode1_binders`
    → :meth:`Bivalent.optimize_linker` → :meth:`Bivalent.dispatch_folds`

Server endpoints (``/api/ligandforge/bivalent/*``):

Mode 2:

- ``POST /run1``                   — create session, configure Run 1
- ``POST /run2``                   — Run 2 with selected Run 1 seeds fixed
- ``POST /run1-fold-gate``         — fold-gate top-K Run 1 hits against T1

Mode 1:

- ``POST /run1-parallel``          — plan two independent binder runs
- ``POST /record-mode1-binders``   — persist top-K from each parallel run
- ``POST /optimize-linker``        — segment_config for linker-only generation

Common:

- ``POST /from-seed``              — start from a user-provided binder
- ``POST /dispatch-folds``         — dispatch validation folds (gene→PDB resolved)
- ``POST /fold``                   — build fold descriptors (individual / ternary / both)
- ``GET  /:id``                    — session detail
- ``GET  /``                       — list sessions
- ``POST /analyze-generation``     — AI review of Run 1 / Run 2 outputs
- ``POST /analyze-folds``          — AI review of fold candidates

Tier: pro+ for all bivalent endpoints (server-gated). Beta — API surface and
scoring conventions may shift before GA.
"""

from __future__ import annotations

from typing import Any, Literal

from ligandai.resources._base import AsyncResource, Resource
from ligandai.types import (
    BivalentSession,
    BivalentTarget,
    FoldAnalysis,
    FoldCandidate,
    GenerationAnalysis,
    LinkerConfig,
)

_FoldMode = Literal["target1", "target2", "individual", "ternary"]


def _target_dump(t: BivalentTarget | dict[str, Any]) -> dict[str, Any]:
    return t.model_dump(by_alias=True) if isinstance(t, BivalentTarget) else t


def _linker_dump(l: LinkerConfig | dict[str, Any]) -> dict[str, Any]:
    return l.model_dump(by_alias=True) if isinstance(l, LinkerConfig) else l


class Bivalent(Resource):
    """``/api/ligandforge/bivalent/*``."""

    def start(
        self,
        target1: BivalentTarget | dict[str, Any],
        target2: BivalentTarget | dict[str, Any],
        linker: LinkerConfig | dict[str, Any],
        binder_length_min: int,
        binder_length_max: int,
        num_designs: int = 100,
    ) -> BivalentSession:
        """``POST /api/ligandforge/bivalent/run1`` — create session + configure Run 1.

        Returns the session record. The caller still must call ``generate_peptides``
        with the returned ``segment_config`` to actually run generation; that flow
        is wrapped server-side in the unified-chat-agent. For SDK ergonomics this
        method only initializes the session; use :meth:`generate_run1` to kick off
        Run 1 generation.
        """
        if self._client is not None:
            self._client._require_feature("predict_structure")
        body = {
            "target1": _target_dump(target1),
            "target2": _target_dump(target2),
            "linker": _linker_dump(linker),
            "binder_length_min": binder_length_min,
            "binder_length_max": binder_length_max,
            "num_designs": num_designs,
        }
        payload = self._transport.request("POST", "/api/ligandforge/bivalent/run1", json=body) or {}
        # Server returns {session_id, status, segment_config, generation_params}
        return BivalentSession.model_validate(_normalize_session(payload, target1, target2, linker))

    def run2(
        self,
        session_id: str,
        selected_seeds: list[str],
    ) -> BivalentSession:
        """``POST /api/ligandforge/bivalent/run2``."""
        body = {"session_id": session_id, "selected_seeds": selected_seeds}
        payload = (
            self._transport.request("POST", "/api/ligandforge/bivalent/run2", json=body) or {}
        )
        return BivalentSession.model_validate(_normalize_session(payload))

    def fold(
        self,
        session_id: str,
        fold_mode: _FoldMode,
        candidates: list[FoldCandidate | dict[str, Any] | str],
    ) -> dict[str, Any]:
        """``POST /api/ligandforge/bivalent/fold``."""
        normalized = [_norm_candidate(c) for c in candidates]
        body = {
            "session_id": session_id,
            "fold_mode": fold_mode,
            "candidates": normalized,
        }
        return self._transport.request("POST", "/api/ligandforge/bivalent/fold", json=body) or {}

    def analyze_generation(
        self,
        session_id: str,
        stage: Literal["run1", "run2"],
        sequences: list[str],
        target_gene: str,
        scores: list[dict[str, Any]] | None = None,
    ) -> GenerationAnalysis:
        body: dict[str, Any] = {
            "session_id": session_id,
            "stage": stage,
            "sequences": sequences,
            "target_gene": target_gene,
        }
        if scores is not None:
            body["scores"] = scores
        return GenerationAnalysis.model_validate(
            self._transport.request(
                "POST", "/api/ligandforge/bivalent/analyze-generation", json=body
            )
            or {"sessionId": session_id, "stage": stage, "summary": ""}
        )

    def analyze_folds(
        self,
        session_id: str,
        fold_mode: _FoldMode,
        candidates: list[FoldCandidate | dict[str, Any]],
    ) -> FoldAnalysis:
        normalized = [_norm_candidate(c) for c in candidates]
        body = {
            "session_id": session_id,
            "fold_mode": fold_mode,
            "candidates": normalized,
        }
        return FoldAnalysis.model_validate(
            self._transport.request(
                "POST", "/api/ligandforge/bivalent/analyze-folds", json=body
            )
            or {"sessionId": session_id, "foldMode": fold_mode, "summary": ""}
        )

    # ------------------------------------------------------------------------
    # Mode 2 — joint LEGO addendum (fold-gate after Run 1).
    # ------------------------------------------------------------------------

    def run1_fold_gate(
        self,
        session_id: str,
        top_k: int = 5,
    ) -> dict[str, Any]:
        """``POST /api/ligandforge/bivalent/run1-fold-gate``.

        Fold the top-K Run 1 candidates against target 1, rank by iPSAE, and
        promote the best-folded one as Run 2's fixed terminus. Returns the
        dispatched fold job descriptors.
        """
        body = {"session_id": session_id, "top_k": top_k}
        return (
            self._transport.request(
                "POST", "/api/ligandforge/bivalent/run1-fold-gate", json=body,
            )
            or {}
        )

    # ------------------------------------------------------------------------
    # Mode 1 — generate-then-link (h1qv3 induced-proximity beta path).
    # ------------------------------------------------------------------------

    def run1_parallel(
        self,
        target1: BivalentTarget | dict[str, Any],
        target2: BivalentTarget | dict[str, Any],
        linker: LinkerConfig | dict[str, Any],
        binder_length_min: int,
        binder_length_max: int,
        num_designs: int = 100,
    ) -> BivalentSession:
        """``POST /api/ligandforge/bivalent/run1-parallel`` — Mode 1 step 1.

        Plans TWO independent binder generations, one per target, with NO
        linker context. Returns ``target1_plan`` and ``target2_plan`` that the
        caller pipes to the standard generation path (one call per plan, in
        parallel). After both runs complete and you've picked top-K binders
        for each, call :meth:`record_mode1_binders` and then
        :meth:`optimize_linker`.
        """
        if self._client is not None:
            self._client._require_feature("predict_structure")
        body = {
            "target1": _target_dump(target1),
            "target2": _target_dump(target2),
            "linker": _linker_dump(linker),
            "binder_length_min": binder_length_min,
            "binder_length_max": binder_length_max,
            "num_designs": num_designs,
        }
        payload = (
            self._transport.request(
                "POST", "/api/ligandforge/bivalent/run1-parallel", json=body,
            )
            or {}
        )
        return BivalentSession.model_validate(
            _normalize_session(payload, target1, target2, linker)
        )

    def record_mode1_binders(
        self,
        session_id: str,
        top_t1: list[str],
        top_t2: list[str],
        t1_job_id: str | None = None,
        t2_job_id: str | None = None,
    ) -> dict[str, Any]:
        """``POST /api/ligandforge/bivalent/record-mode1-binders`` — Mode 1 step 2.

        Persist the top-K binders from each of the two parallel runs. Selection
        is the caller's choice — LigandIQ score, validation fold iPSAE, manual.
        Required before :meth:`optimize_linker` can anchor the linker.
        """
        body: dict[str, Any] = {
            "session_id": session_id,
            "top_t1": top_t1,
            "top_t2": top_t2,
        }
        if t1_job_id is not None:
            body["t1_job_id"] = t1_job_id
        if t2_job_id is not None:
            body["t2_job_id"] = t2_job_id
        return (
            self._transport.request(
                "POST", "/api/ligandforge/bivalent/record-mode1-binders", json=body,
            )
            or {}
        )

    def optimize_linker(
        self,
        session_id: str,
        t1_index: int = 0,
        t2_index: int = 0,
        linker: LinkerConfig | dict[str, Any] | None = None,
        num_designs: int = 100,
    ) -> dict[str, Any]:
        """``POST /api/ligandforge/bivalent/optimize-linker`` — Mode 1 step 3.

        Returns a ``segment_config`` of the form
        ``[premade(top_t1[t1_index]) + linker(varied) + premade(top_t2[t2_index])]``
        plus ``generation_params`` with the auto-computed length window. Pipe
        the segment_config to the standard generation path; only the linker
        positions are designed — the two binder termini are frozen.
        """
        body: dict[str, Any] = {
            "session_id": session_id,
            "t1_index": t1_index,
            "t2_index": t2_index,
            "num_designs": num_designs,
        }
        if linker is not None:
            body["linker"] = _linker_dump(linker)
        return (
            self._transport.request(
                "POST", "/api/ligandforge/bivalent/optimize-linker", json=body,
            )
            or {}
        )

    # ------------------------------------------------------------------------
    # Common — folding + alternate entry path.
    # ------------------------------------------------------------------------

    def from_seed(
        self,
        target1: BivalentTarget | dict[str, Any],
        target2: BivalentTarget | dict[str, Any],
        linker: LinkerConfig | dict[str, Any],
        binder_t1_sequence: str,
        num_designs: int = 100,
    ) -> BivalentSession:
        """``POST /api/ligandforge/bivalent/from-seed``.

        Start a session from a user-provided binder sequence for target 1 —
        skips Run 1 generation entirely. The session goes straight to Run 2
        with the supplied binder fixed as the premade terminus. Useful when
        you already have a validated T1 binder and only need T2 + the linker.
        """
        if self._client is not None:
            self._client._require_feature("predict_structure")
        body = {
            "target1": _target_dump(target1),
            "target2": _target_dump(target2),
            "linker": _linker_dump(linker),
            "binder_t1_sequence": binder_t1_sequence,
            "num_designs": num_designs,
        }
        payload = (
            self._transport.request(
                "POST", "/api/ligandforge/bivalent/from-seed", json=body,
            )
            or {}
        )
        return BivalentSession.model_validate(
            _normalize_session(payload, target1, target2, linker)
        )

    def dispatch_folds(
        self,
        session_id: str,
        descriptors: list[dict[str, Any]],
        num_trajectories: int = 1,
        model: str = "boltz2",
    ) -> dict[str, Any]:
        """``POST /api/ligandforge/bivalent/dispatch-folds``.

        Dispatch validation folds through the bivalent fold orchestrator —
        gene→PDB resolution + per-target fold submissions + webhook routing
        back to the session. Pass the descriptors returned from :meth:`fold`
        (or constructed manually). Each descriptor folds peptide × T1 or
        peptide × T2 independently; aggregate score is
        ``min(iPTM_T1, iPTM_T2)``.
        """
        body = {
            "session_id": session_id,
            "descriptors": descriptors,
            "num_trajectories": num_trajectories,
            "model": model,
        }
        return (
            self._transport.request(
                "POST", "/api/ligandforge/bivalent/dispatch-folds", json=body,
            )
            or {}
        )

    def list_sessions(self) -> list[BivalentSession]:
        payload = self._transport.request("GET", "/api/ligandforge/bivalent") or []
        items = payload if isinstance(payload, list) else payload.get("sessions", [])
        return [BivalentSession.model_validate(_normalize_session(s)) for s in items]

    def get_session(self, session_id: str) -> BivalentSession:
        payload = self._transport.request("GET", f"/api/ligandforge/bivalent/{session_id}") or {}
        return BivalentSession.model_validate(_normalize_session(payload))


class AsyncBivalent(AsyncResource):
    async def start(
        self,
        target1: BivalentTarget | dict[str, Any],
        target2: BivalentTarget | dict[str, Any],
        linker: LinkerConfig | dict[str, Any],
        binder_length_min: int,
        binder_length_max: int,
        num_designs: int = 100,
    ) -> BivalentSession:
        if self._client is not None:
            self._client._require_feature("predict_structure")
        body = {
            "target1": _target_dump(target1),
            "target2": _target_dump(target2),
            "linker": _linker_dump(linker),
            "binder_length_min": binder_length_min,
            "binder_length_max": binder_length_max,
            "num_designs": num_designs,
        }
        payload = await self._transport.request("POST", "/api/ligandforge/bivalent/run1", json=body) or {}
        return BivalentSession.model_validate(_normalize_session(payload, target1, target2, linker))

    async def run2(
        self,
        session_id: str,
        selected_seeds: list[str],
    ) -> BivalentSession:
        body = {"session_id": session_id, "selected_seeds": selected_seeds}
        payload = (
            await self._transport.request("POST", "/api/ligandforge/bivalent/run2", json=body) or {}
        )
        return BivalentSession.model_validate(_normalize_session(payload))

    async def fold(
        self,
        session_id: str,
        fold_mode: _FoldMode,
        candidates: list[FoldCandidate | dict[str, Any] | str],
    ) -> dict[str, Any]:
        normalized = [_norm_candidate(c) for c in candidates]
        body = {
            "session_id": session_id,
            "fold_mode": fold_mode,
            "candidates": normalized,
        }
        return await self._transport.request("POST", "/api/ligandforge/bivalent/fold", json=body) or {}

    async def analyze_generation(
        self,
        session_id: str,
        stage: Literal["run1", "run2"],
        sequences: list[str],
        target_gene: str,
        scores: list[dict[str, Any]] | None = None,
    ) -> GenerationAnalysis:
        body: dict[str, Any] = {
            "session_id": session_id,
            "stage": stage,
            "sequences": sequences,
            "target_gene": target_gene,
        }
        if scores is not None:
            body["scores"] = scores
        return GenerationAnalysis.model_validate(
            await self._transport.request(
                "POST", "/api/ligandforge/bivalent/analyze-generation", json=body
            )
            or {"sessionId": session_id, "stage": stage, "summary": ""}
        )

    async def analyze_folds(
        self,
        session_id: str,
        fold_mode: _FoldMode,
        candidates: list[FoldCandidate | dict[str, Any]],
    ) -> FoldAnalysis:
        normalized = [_norm_candidate(c) for c in candidates]
        body = {
            "session_id": session_id,
            "fold_mode": fold_mode,
            "candidates": normalized,
        }
        return FoldAnalysis.model_validate(
            await self._transport.request(
                "POST", "/api/ligandforge/bivalent/analyze-folds", json=body
            )
            or {"sessionId": session_id, "foldMode": fold_mode, "summary": ""}
        )

    # ------------------------------------------------------------------------
    # Mode 2 — joint LEGO addendum (async).
    # ------------------------------------------------------------------------

    async def run1_fold_gate(
        self,
        session_id: str,
        top_k: int = 5,
    ) -> dict[str, Any]:
        body = {"session_id": session_id, "top_k": top_k}
        return (
            await self._transport.request(
                "POST", "/api/ligandforge/bivalent/run1-fold-gate", json=body,
            )
            or {}
        )

    # ------------------------------------------------------------------------
    # Mode 1 — generate-then-link (async).
    # ------------------------------------------------------------------------

    async def run1_parallel(
        self,
        target1: BivalentTarget | dict[str, Any],
        target2: BivalentTarget | dict[str, Any],
        linker: LinkerConfig | dict[str, Any],
        binder_length_min: int,
        binder_length_max: int,
        num_designs: int = 100,
    ) -> BivalentSession:
        if self._client is not None:
            self._client._require_feature("predict_structure")
        body = {
            "target1": _target_dump(target1),
            "target2": _target_dump(target2),
            "linker": _linker_dump(linker),
            "binder_length_min": binder_length_min,
            "binder_length_max": binder_length_max,
            "num_designs": num_designs,
        }
        payload = (
            await self._transport.request(
                "POST", "/api/ligandforge/bivalent/run1-parallel", json=body,
            )
            or {}
        )
        return BivalentSession.model_validate(
            _normalize_session(payload, target1, target2, linker)
        )

    async def record_mode1_binders(
        self,
        session_id: str,
        top_t1: list[str],
        top_t2: list[str],
        t1_job_id: str | None = None,
        t2_job_id: str | None = None,
    ) -> dict[str, Any]:
        body: dict[str, Any] = {
            "session_id": session_id,
            "top_t1": top_t1,
            "top_t2": top_t2,
        }
        if t1_job_id is not None:
            body["t1_job_id"] = t1_job_id
        if t2_job_id is not None:
            body["t2_job_id"] = t2_job_id
        return (
            await self._transport.request(
                "POST", "/api/ligandforge/bivalent/record-mode1-binders", json=body,
            )
            or {}
        )

    async def optimize_linker(
        self,
        session_id: str,
        t1_index: int = 0,
        t2_index: int = 0,
        linker: LinkerConfig | dict[str, Any] | None = None,
        num_designs: int = 100,
    ) -> dict[str, Any]:
        body: dict[str, Any] = {
            "session_id": session_id,
            "t1_index": t1_index,
            "t2_index": t2_index,
            "num_designs": num_designs,
        }
        if linker is not None:
            body["linker"] = _linker_dump(linker)
        return (
            await self._transport.request(
                "POST", "/api/ligandforge/bivalent/optimize-linker", json=body,
            )
            or {}
        )

    # ------------------------------------------------------------------------
    # Common — async.
    # ------------------------------------------------------------------------

    async def from_seed(
        self,
        target1: BivalentTarget | dict[str, Any],
        target2: BivalentTarget | dict[str, Any],
        linker: LinkerConfig | dict[str, Any],
        binder_t1_sequence: str,
        num_designs: int = 100,
    ) -> BivalentSession:
        if self._client is not None:
            self._client._require_feature("predict_structure")
        body = {
            "target1": _target_dump(target1),
            "target2": _target_dump(target2),
            "linker": _linker_dump(linker),
            "binder_t1_sequence": binder_t1_sequence,
            "num_designs": num_designs,
        }
        payload = (
            await self._transport.request(
                "POST", "/api/ligandforge/bivalent/from-seed", json=body,
            )
            or {}
        )
        return BivalentSession.model_validate(
            _normalize_session(payload, target1, target2, linker)
        )

    async def dispatch_folds(
        self,
        session_id: str,
        descriptors: list[dict[str, Any]],
        num_trajectories: int = 1,
        model: str = "boltz2",
    ) -> dict[str, Any]:
        body = {
            "session_id": session_id,
            "descriptors": descriptors,
            "num_trajectories": num_trajectories,
            "model": model,
        }
        return (
            await self._transport.request(
                "POST", "/api/ligandforge/bivalent/dispatch-folds", json=body,
            )
            or {}
        )

    async def list_sessions(self) -> list[BivalentSession]:
        payload = await self._transport.request("GET", "/api/ligandforge/bivalent") or []
        items = payload if isinstance(payload, list) else payload.get("sessions", [])
        return [BivalentSession.model_validate(_normalize_session(s)) for s in items]

    async def get_session(self, session_id: str) -> BivalentSession:
        payload = await self._transport.request("GET", f"/api/ligandforge/bivalent/{session_id}") or {}
        return BivalentSession.model_validate(_normalize_session(payload))


def _norm_candidate(c: FoldCandidate | dict[str, Any] | str) -> dict[str, Any]:
    if isinstance(c, FoldCandidate):
        return c.model_dump(by_alias=True)
    if isinstance(c, str):
        return {"sequence": c}
    return dict(c)


def _normalize_session(
    payload: dict[str, Any],
    target1: BivalentTarget | dict[str, Any] | None = None,
    target2: BivalentTarget | dict[str, Any] | None = None,
    linker: LinkerConfig | dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Normalize the heterogeneous server session payloads to BivalentSession fields."""
    out = dict(payload)
    out.setdefault("id", payload.get("session_id") or payload.get("sessionId") or payload.get("id") or "")
    out.setdefault("status", payload.get("status") or "queued")
    if target1 is not None:
        out.setdefault("target1", _target_dump(target1))
    elif "target1" not in out and "target1_gene" in payload:
        out["target1"] = {"gene": payload["target1_gene"], "chain": payload.get("target1_chain") or "A"}
    if target2 is not None:
        out.setdefault("target2", _target_dump(target2))
    elif "target2" not in out and "target2_gene" in payload:
        out["target2"] = {"gene": payload["target2_gene"], "chain": payload.get("target2_chain") or "A"}
    if linker is not None:
        out.setdefault("linker", _linker_dump(linker))
    elif "linker" not in out and "linker_position" in payload:
        out["linker"] = {
            "position": payload["linker_position"],
            "lengthMin": payload.get("linker_length_min", 0),
            "lengthMax": payload.get("linker_length_max", 0),
        }
    # Ensure required fields have something
    out.setdefault("target1", {"gene": "unknown", "chain": "A"})
    out.setdefault("target2", {"gene": "unknown", "chain": "A"})
    out.setdefault("linker", {"position": "C", "lengthMin": 8, "lengthMax": 16})
    return out
