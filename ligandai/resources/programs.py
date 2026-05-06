# Copyright © 2026 Ligandal, Inc. All rights reserved.
"""Programs, workstreams, projects, and sessions."""

from __future__ import annotations

from typing import List

from ligandai.resources._base import AsyncResource, Resource
from ligandai.types import (
    Program,
    ProgramDetail,
    Session,
    SessionDetail,
    Workstream,
)


class Programs(Resource):
    """``/api/ptf/programs/*``, ``/api/ptf/workstreams``, and ``/api/ptf/sessions/*``."""

    def list(self) -> List[Program]:
        payload = self._transport.request("GET", "/api/ptf/programs") or []
        items = payload if isinstance(payload, list) else payload.get("programs", [])
        return [Program.model_validate(p) for p in items]

    def create(
        self,
        name: str,
        description: str | None = None,
        color: str = "#3b82f6",
    ) -> Program:
        body: dict[str, object] = {"name": name, "color": color}
        if description is not None:
            body["description"] = description
        return Program.model_validate(
            self._transport.request("POST", "/api/ptf/programs", json=body) or {}
        )

    def get(self, program_id: int) -> ProgramDetail:
        return ProgramDetail.model_validate(
            self._transport.request("GET", f"/api/ptf/programs/{program_id}") or {}
        )

    def update(self, program_id: int, **fields: object) -> Program:
        return Program.model_validate(
            self._transport.request("PATCH", f"/api/ptf/programs/{program_id}", json=fields) or {}
        )

    def archive(self, program_id: int) -> bool:
        try:
            self._transport.request("DELETE", f"/api/ptf/programs/{program_id}")
            return True
        except Exception:
            return False

    def workstreams(self, program_id: int | None = None) -> List[Workstream]:
        params = {"program_id": program_id} if program_id is not None else None
        payload = self._transport.request("GET", "/api/ptf/workstreams", params=params) or []
        items = payload if isinstance(payload, list) else payload.get("workstreams", [])
        return [Workstream.model_validate(w) for w in items]

    def create_workstream(
        self,
        program_id: int,
        name: str,
        description: str | None = None,
        color: str = "#3b82f6",
        genes: List[str] | None = None,
    ) -> Workstream:
        body: dict[str, object] = {
            "programId": program_id,
            "name": name,
            "color": color,
        }
        if description is not None:
            body["description"] = description
        if genes is not None:
            body["genes"] = genes
        return Workstream.model_validate(
            self._transport.request("POST", "/api/ptf/workstreams", json=body) or {}
        )

    def list_sessions(self, gene: str | None = None, limit: int = 20) -> List[Session]:
        params: dict[str, object] = {"limit": limit}
        if gene is not None:
            params["gene"] = gene
        payload = self._transport.request("GET", "/api/ptf/sessions", params=params) or []
        items = payload if isinstance(payload, list) else payload.get("sessions", [])
        return [Session.model_validate(s) for s in items]

    def get_session(self, session_id: str) -> SessionDetail:
        return SessionDetail.model_validate(
            self._transport.request("GET", f"/api/ptf/sessions/{session_id}") or {}
        )

    def find_session_by_gene(self, gene: str) -> Session | None:
        payload = self._transport.request("GET", f"/api/ptf/sessions/by-gene/{gene}")
        if not payload:
            return None
        return Session.model_validate(payload)


class AsyncPrograms(AsyncResource):
    async def list(self) -> List[Program]:
        payload = await self._transport.request("GET", "/api/ptf/programs") or []
        items = payload if isinstance(payload, list) else payload.get("programs", [])
        return [Program.model_validate(p) for p in items]

    async def create(
        self,
        name: str,
        description: str | None = None,
        color: str = "#3b82f6",
    ) -> Program:
        body: dict[str, object] = {"name": name, "color": color}
        if description is not None:
            body["description"] = description
        return Program.model_validate(
            await self._transport.request("POST", "/api/ptf/programs", json=body) or {}
        )

    async def get(self, program_id: int) -> ProgramDetail:
        return ProgramDetail.model_validate(
            await self._transport.request("GET", f"/api/ptf/programs/{program_id}") or {}
        )

    async def update(self, program_id: int, **fields: object) -> Program:
        return Program.model_validate(
            await self._transport.request("PATCH", f"/api/ptf/programs/{program_id}", json=fields) or {}
        )

    async def archive(self, program_id: int) -> bool:
        try:
            await self._transport.request("DELETE", f"/api/ptf/programs/{program_id}")
            return True
        except Exception:
            return False

    async def workstreams(self, program_id: int | None = None) -> List[Workstream]:
        params = {"program_id": program_id} if program_id is not None else None
        payload = await self._transport.request("GET", "/api/ptf/workstreams", params=params) or []
        items = payload if isinstance(payload, list) else payload.get("workstreams", [])
        return [Workstream.model_validate(w) for w in items]

    async def create_workstream(
        self,
        program_id: int,
        name: str,
        description: str | None = None,
        color: str = "#3b82f6",
        genes: List[str] | None = None,
    ) -> Workstream:
        body: dict[str, object] = {
            "programId": program_id,
            "name": name,
            "color": color,
        }
        if description is not None:
            body["description"] = description
        if genes is not None:
            body["genes"] = genes
        return Workstream.model_validate(
            await self._transport.request("POST", "/api/ptf/workstreams", json=body) or {}
        )

    async def list_sessions(self, gene: str | None = None, limit: int = 20) -> List[Session]:
        params: dict[str, object] = {"limit": limit}
        if gene is not None:
            params["gene"] = gene
        payload = await self._transport.request("GET", "/api/ptf/sessions", params=params) or []
        items = payload if isinstance(payload, list) else payload.get("sessions", [])
        return [Session.model_validate(s) for s in items]

    async def get_session(self, session_id: str) -> SessionDetail:
        return SessionDetail.model_validate(
            await self._transport.request("GET", f"/api/ptf/sessions/{session_id}") or {}
        )

    async def find_session_by_gene(self, gene: str) -> Session | None:
        payload = await self._transport.request("GET", f"/api/ptf/sessions/by-gene/{gene}")
        if not payload:
            return None
        return Session.model_validate(payload)
