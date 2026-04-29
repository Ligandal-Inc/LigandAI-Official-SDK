# Copyright © 2025 Ligandal, Inc. All rights reserved.
"""Bivalent / bispecific peptide design.

Server endpoints (``/api/ligandforge/bivalent/*``):

- ``POST /run1``           — create session, configure Run 1 (binder_T1 + linker mask)
- ``POST /run2``           — start Run 2 with selected Run 1 seeds as fixed terminus
- ``POST /fold``           — fold candidates against T1, T2, or ternary
- ``GET  /:id``            — session detail
- ``GET  /``               — list sessions
- ``POST /analyze-generation`` — AI review of Run 1 / Run 2 outputs
- ``POST /analyze-folds``      — AI review of fold candidates

Tier: pro+ for all bivalent endpoints (server-gated).
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
