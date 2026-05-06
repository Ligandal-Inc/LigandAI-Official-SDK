# Copyright © 2026 Ligandal, Inc. All rights reserved.
"""Job listing, cancellation, and SSE streaming."""

from __future__ import annotations

from collections.abc import AsyncIterator, Iterator
from typing import Literal

from ligandai._http import parse_sse_data
from ligandai.resources._base import AsyncResource, Resource
from ligandai.types import JobEvent, JobInfo, StopAllResult


class Jobs(Resource):
    """``/api/jobs/*`` endpoints — list, cancel, stream."""

    def list(
        self,
        type: Literal["generation", "folding", "scoring", "all"] = "all",
        limit: int = 20,
    ) -> list[JobInfo]:
        """``GET /api/jobs/history`` — filter by type."""
        params: dict[str, object] = {"limit": limit}
        if type != "all":
            params["type"] = type
        payload = self._transport.request("GET", "/api/jobs/history", params=params) or []
        items = payload if isinstance(payload, list) else payload.get("jobs", [])
        return [JobInfo.model_validate(j) for j in items]

    def get(self, job_id: str) -> JobInfo:
        """``GET /api/jobs/:id`` — ownership-checked detail."""
        return JobInfo.model_validate(
            self._transport.request("GET", f"/api/jobs/{job_id}") or {}
        )

    def cancel(self, job_id: str) -> bool:
        """``POST /api/jobs/:id/cancel``."""
        try:
            self._transport.request("POST", f"/api/jobs/{job_id}/cancel")
            return True
        except Exception:
            return False

    def stop_all(self) -> StopAllResult:
        """``POST /api/jobs/stop-mine`` — cancel ALL of the current user's running jobs."""
        return StopAllResult.model_validate(
            self._transport.request("POST", "/api/jobs/stop-mine") or {"cancelledCount": 0, "jobIds": []}
        )

    def stream(self, job_id: str) -> Iterator[JobEvent]:
        """``GET /api/jobs/:id/sse`` — yields :class:`JobEvent` instances live."""
        for line in self._transport.stream_lines("GET", f"/api/jobs/{job_id}/sse"):
            data = parse_sse_data(line)
            if data is None:
                continue
            yield JobEvent.model_validate(_normalize(data))


class AsyncJobs(AsyncResource):
    async def list(
        self,
        type: Literal["generation", "folding", "scoring", "all"] = "all",
        limit: int = 20,
    ) -> list[JobInfo]:
        params: dict[str, object] = {"limit": limit}
        if type != "all":
            params["type"] = type
        payload = await self._transport.request("GET", "/api/jobs/history", params=params) or []
        items = payload if isinstance(payload, list) else payload.get("jobs", [])
        return [JobInfo.model_validate(j) for j in items]

    async def get(self, job_id: str) -> JobInfo:
        return JobInfo.model_validate(
            await self._transport.request("GET", f"/api/jobs/{job_id}") or {}
        )

    async def cancel(self, job_id: str) -> bool:
        try:
            await self._transport.request("POST", f"/api/jobs/{job_id}/cancel")
            return True
        except Exception:
            return False

    async def stop_all(self) -> StopAllResult:
        return StopAllResult.model_validate(
            await self._transport.request("POST", "/api/jobs/stop-mine") or {"cancelledCount": 0, "jobIds": []}
        )

    async def stream(self, job_id: str) -> AsyncIterator[JobEvent]:
        async for line in self._transport.stream_lines("GET", f"/api/jobs/{job_id}/sse"):
            data = parse_sse_data(line)
            if data is None:
                continue
            yield JobEvent.model_validate(_normalize(data))


def _normalize(data: dict[str, object]) -> dict[str, object]:
    return {
        "eventType": data.get("event") or data.get("type") or data.get("stage") or "message",
        "stage": data.get("stage"),
        "message": data.get("message"),
        "progress": data.get("progress"),
        "payload": data,
    }
