# Copyright © 2026 Ligandal, Inc. All rights reserved.
"""Job listing, cancellation, and SSE streaming."""

from __future__ import annotations

from collections.abc import AsyncIterator, Iterator
from typing import Any, Literal

from ligandai._http import parse_sse_data
from ligandai.errors import LigandAINotFoundError
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
        """``GET /api/jobs/:id`` — ownership-checked status snapshot.

        Parallel-generation sessions (``session_parallel_*``) live in the PTF
        parallel store, **not** the generic jobs tables, so the bare
        ``/api/jobs/{id}`` lookup 404s on them. When that happens this method
        transparently falls back to ``GET /api/ptf/parallel/{id}/status`` and
        normalizes the response into a :class:`~ligandai.types.JobInfo`.

        This returns a one-shot snapshot. To *resume polling* a parallel-gen
        run and get a waitable handle, use
        :meth:`ligandai.resources.peptides.Peptides.reattach` instead::

            result = client.peptides.reattach(session_id).wait()
        """
        try:
            payload = self._transport.request("GET", f"/api/jobs/{job_id}") or {}
        except LigandAINotFoundError:
            if not _is_parallel_session_id(job_id):
                raise
            status = self._transport.request(
                "GET", f"/api/ptf/parallel/{job_id}/status"
            ) or {}
            payload = _normalize_parallel_status(status, job_id)
        return JobInfo.model_validate(payload)

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
        """Async sibling of :meth:`Jobs.get`. Falls back to the parallel-gen
        status endpoint on a 404 for ``session_parallel_*`` ids."""
        try:
            payload = await self._transport.request("GET", f"/api/jobs/{job_id}") or {}
        except LigandAINotFoundError:
            if not _is_parallel_session_id(job_id):
                raise
            status = await self._transport.request(
                "GET", f"/api/ptf/parallel/{job_id}/status"
            ) or {}
            payload = _normalize_parallel_status(status, job_id)
        return JobInfo.model_validate(payload)

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


def _is_parallel_session_id(job_id: str) -> bool:
    """True when ``job_id`` looks like a PTF parallel-generation session id.

    Parallel-gen sessions are returned by ``peptides.generate()`` as
    ``session_parallel_<ts>_<hash>`` (occasionally a bare ``session*`` id).
    These live in the PTF parallel store rather than the generic jobs tables,
    so the ``/api/jobs/{id}`` lookup 404s and we route to the parallel status
    endpoint instead. Anything else re-raises the original 404.
    """
    return isinstance(job_id, str) and job_id.startswith("session")


def _normalize_parallel_status(status: dict[str, Any], job_id: str) -> dict[str, Any]:
    """Map a ``/api/ptf/parallel/{id}/status`` response onto ``JobInfo`` fields.

    The parallel status payload keys its id as ``sessionId`` (not ``id``) and
    omits ``type``; the rest of the body is preserved both as ``JobInfo``
    extras and under ``result`` so callers can read the generation/elite stats.
    """
    out: dict[str, Any] = dict(status) if isinstance(status, dict) else {}
    out.setdefault("id", out.get("sessionId") or out.get("session_id") or job_id)
    out.setdefault("type", "generation")
    out.setdefault("status", out.get("status") or "running")
    if "result" not in out:
        out["result"] = dict(status) if isinstance(status, dict) else {}
    return out
