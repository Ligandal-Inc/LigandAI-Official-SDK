# Copyright © 2026 Ligandal, Inc. All rights reserved.
"""Persistent goal-directed AutoResearch runs."""

from __future__ import annotations

from collections.abc import AsyncIterator, Iterator
from typing import Any
from urllib.parse import urlencode

from ligandai._http import parse_sse_data
from ligandai.resources._base import AsyncResource, Resource
from ligandai.types import GoalProjectState, GoalRun, GoalRunEvent, GoalRunStart


def _start_body(
    *,
    goal: str,
    automatic_mode: bool,
    budget_cap_credits: int | None,
    program_db_id: int | None,
    project_db_id: int | None,
    program_id: str | None,
    project_id: str | None,
    conversation_id: str | None,
    max_iterations: int | None,
) -> dict[str, Any]:
    if not goal or len(goal.strip()) < 4:
        raise ValueError("goal must be at least 4 characters")
    if not automatic_mode:
        raise ValueError(
            "Starting a persistent goal run can spend credits after your process exits. "
            "Pass automatic_mode=True to acknowledge this."
        )
    if budget_cap_credits is not None and budget_cap_credits < 0:
        raise ValueError("budget_cap_credits must be non-negative")
    if max_iterations is not None and max_iterations < 0:
        raise ValueError("max_iterations must be non-negative")

    body: dict[str, Any] = {
        "goal": goal.strip(),
        "automaticMode": True,
        "automaticModeAcknowledged": True,
    }
    if budget_cap_credits is not None:
        body["budgetCapCredits"] = int(budget_cap_credits)
    if program_db_id is not None:
        body["programDbId"] = int(program_db_id)
    if project_db_id is not None:
        body["projectDbId"] = int(project_db_id)
    if program_id:
        body["programId"] = program_id
    if project_id:
        body["projectId"] = project_id
    if conversation_id:
        body["conversationId"] = conversation_id
    if max_iterations is not None:
        body["maxIterations"] = int(max_iterations)
    return body


def _runs_path(
    *,
    program_id: str | None = None,
    project_id: str | None = None,
    program_db_id: int | None = None,
    project_db_id: int | None = None,
    conversation_id: str | None = None,
) -> str:
    params: dict[str, Any] = {}
    if program_id:
        params["programId"] = program_id
    if project_id:
        params["projectId"] = project_id
    if program_db_id is not None:
        params["programDbId"] = int(program_db_id)
    if project_db_id is not None:
        params["projectDbId"] = int(project_db_id)
    if conversation_id:
        params["conversationId"] = conversation_id
    return f"/api/autoresearch/runs?{urlencode(params)}" if params else "/api/autoresearch/runs"


class Goals(Resource):
    """Manage persistent AutoResearch goal runs."""

    def start(
        self,
        goal: str,
        *,
        automatic_mode: bool = False,
        budget_cap_credits: int | None = 10_000,
        program_db_id: int | None = None,
        project_db_id: int | None = None,
        program_id: str | None = None,
        project_id: str | None = None,
        conversation_id: str | None = None,
        max_iterations: int | None = None,
    ) -> GoalRunStart:
        """Start a persistent goal-directed run.

        ``automatic_mode=True`` is required intentionally: these runs can keep
        working and consuming credits after the Python process exits until they
        are stopped or hit ``budget_cap_credits``.
        """

        payload = self._transport.request(
            "POST",
            "/api/autoresearch/start",
            json=_start_body(
                goal=goal,
                automatic_mode=automatic_mode,
                budget_cap_credits=budget_cap_credits,
                program_db_id=program_db_id,
                project_db_id=project_db_id,
                program_id=program_id,
                project_id=project_id,
                conversation_id=conversation_id,
                max_iterations=max_iterations,
            ),
        )
        return GoalRunStart.model_validate(payload or {})

    def list(
        self,
        *,
        program_id: str | None = None,
        project_id: str | None = None,
        program_db_id: int | None = None,
        project_db_id: int | None = None,
        conversation_id: str | None = None,
    ) -> list[GoalRun]:
        payload = self._transport.request(
            "GET",
            _runs_path(
                program_id=program_id,
                project_id=project_id,
                program_db_id=program_db_id,
                project_db_id=project_db_id,
                conversation_id=conversation_id,
            ),
        ) or {}
        return [GoalRun.model_validate(run) for run in payload.get("runs", [])]

    def get(self, run_id: str) -> GoalRun:
        payload = self._transport.request("GET", f"/api/autoresearch/runs/{run_id}") or {}
        return GoalRun.model_validate(payload.get("run") or payload)

    def graph(self, run_id: str) -> GoalProjectState:
        """Return the derived checklist/dependency/evidence graph for a run."""

        payload = self._transport.request("GET", f"/api/autoresearch/runs/{run_id}/graph") or {}
        return GoalProjectState.model_validate(payload.get("goalState") or payload)

    def stream(self, run_id: str, *, timeout: float | None = None) -> Iterator[GoalRunEvent]:
        """Stream live AutoResearch events for a persistent goal run.

        The first server event is usually ``hello`` and includes the latest
        run snapshot; subsequent events include planning, step, evaluation, and
        terminal status updates. This is a live stream, not durable replay.
        """

        for line in self._transport.stream_lines(
            "GET",
            f"/api/autoresearch/runs/{run_id}/stream",
            timeout=timeout,
        ):
            data = parse_sse_data(line)
            if data is None:
                continue
            yield GoalRunEvent.model_validate({**data, "payload": data})

    def pause(self, run_id: str) -> bool:
        payload = self._transport.request("POST", f"/api/autoresearch/runs/{run_id}/pause") or {}
        return bool(payload.get("ok", True))

    def resume(self, run_id: str) -> bool:
        payload = self._transport.request("POST", f"/api/autoresearch/runs/{run_id}/resume") or {}
        return bool(payload.get("ok", True))

    def stop(self, run_id: str) -> bool:
        payload = self._transport.request("POST", f"/api/autoresearch/runs/{run_id}/stop") or {}
        return bool(payload.get("ok", True))


class AsyncGoals(AsyncResource):
    """Async sibling of :class:`Goals`."""

    async def start(
        self,
        goal: str,
        *,
        automatic_mode: bool = False,
        budget_cap_credits: int | None = 10_000,
        program_db_id: int | None = None,
        project_db_id: int | None = None,
        program_id: str | None = None,
        project_id: str | None = None,
        conversation_id: str | None = None,
        max_iterations: int | None = None,
    ) -> GoalRunStart:
        payload = await self._transport.request(
            "POST",
            "/api/autoresearch/start",
            json=_start_body(
                goal=goal,
                automatic_mode=automatic_mode,
                budget_cap_credits=budget_cap_credits,
                program_db_id=program_db_id,
                project_db_id=project_db_id,
                program_id=program_id,
                project_id=project_id,
                conversation_id=conversation_id,
                max_iterations=max_iterations,
            ),
        )
        return GoalRunStart.model_validate(payload or {})

    async def list(
        self,
        *,
        program_id: str | None = None,
        project_id: str | None = None,
        program_db_id: int | None = None,
        project_db_id: int | None = None,
        conversation_id: str | None = None,
    ) -> list[GoalRun]:
        payload = await self._transport.request(
            "GET",
            _runs_path(
                program_id=program_id,
                project_id=project_id,
                program_db_id=program_db_id,
                project_db_id=project_db_id,
                conversation_id=conversation_id,
            ),
        ) or {}
        return [GoalRun.model_validate(run) for run in payload.get("runs", [])]

    async def get(self, run_id: str) -> GoalRun:
        payload = await self._transport.request("GET", f"/api/autoresearch/runs/{run_id}") or {}
        return GoalRun.model_validate(payload.get("run") or payload)

    async def graph(self, run_id: str) -> GoalProjectState:
        """Return the derived checklist/dependency/evidence graph for a run."""

        payload = await self._transport.request("GET", f"/api/autoresearch/runs/{run_id}/graph") or {}
        return GoalProjectState.model_validate(payload.get("goalState") or payload)

    async def stream(self, run_id: str, *, timeout: float | None = None) -> AsyncIterator[GoalRunEvent]:
        """Stream live AutoResearch events for a persistent goal run."""

        async for line in self._transport.stream_lines(
            "GET",
            f"/api/autoresearch/runs/{run_id}/stream",
            timeout=timeout,
        ):
            data = parse_sse_data(line)
            if data is None:
                continue
            yield GoalRunEvent.model_validate({**data, "payload": data})

    async def pause(self, run_id: str) -> bool:
        payload = await self._transport.request("POST", f"/api/autoresearch/runs/{run_id}/pause") or {}
        return bool(payload.get("ok", True))

    async def resume(self, run_id: str) -> bool:
        payload = await self._transport.request("POST", f"/api/autoresearch/runs/{run_id}/resume") or {}
        return bool(payload.get("ok", True))

    async def stop(self, run_id: str) -> bool:
        payload = await self._transport.request("POST", f"/api/autoresearch/runs/{run_id}/stop") or {}
        return bool(payload.get("ok", True))
