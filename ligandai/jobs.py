# Copyright © 2025 Ligandal, Inc. All rights reserved.
"""Long-running job abstractions.

Generation and folding operations return :class:`Job` (sync) or :class:`AsyncJob`
(async) instances. Both expose ``.wait()``, ``.poll()``, ``.cancel()`` and
``.stream()`` for live progress events.

Jobs are polymorphic over their result type — generation jobs resolve to
:class:`~ligandai.types.GenerationResult`, folding jobs to
:class:`~ligandai.types.FoldResult`.
"""

from __future__ import annotations

import asyncio
import contextlib
import inspect
import time
from collections.abc import AsyncIterator, Awaitable, Callable, Iterator
from typing import Any, Generic, TypeVar

from ligandai._constants import (
    DEFAULT_JOB_TIMEOUT_SECS,
    DEFAULT_POLL_INTERVAL_SECS,
)
from ligandai._http import AsyncHTTPTransport, HTTPTransport, parse_sse_data
from ligandai.errors import (
    LigandAIError,
    LigandAIJobError,
    LigandAITimeoutError,
)
from ligandai.types import JobEvent, JobInfo

T = TypeVar("T")
TERMINAL_STATUSES = frozenset({
    "complete", "completed", "failed", "cancelled", "error",
    "generation_complete", "fold_complete",
})
SUCCESS_STATUSES = frozenset({"complete", "completed", "generation_complete", "fold_complete"})
ResultLoader = Callable[[JobInfo], dict[str, Any] | None]
AsyncResultLoader = Callable[[JobInfo], Awaitable[dict[str, Any] | None] | dict[str, Any] | None]


class Job(Generic[T]):
    """A long-running server-side job.

    Created by resource methods like ``client.peptides.generate(...)`` and
    ``client.peptides.fold(...)``. Use ``.wait()`` to block until completion.

    Parameters
    ----------
    transport
        The shared HTTP transport.
    job_id
        Server-assigned id (generation: ``ligandforge-...``; fold: ``ptf-fold-...``).
    job_type
        ``"generation"``, ``"folding"``, or ``"scoring"``.
    parser
        Callable that turns the final result payload into a typed object.
    status_path
        URL path for the status endpoint, parametrized on ``{job_id}``.
    cancel_path
        URL path for the cancel endpoint.
    sse_path
        URL path for the SSE stream endpoint.
    initial
        Initial JobInfo from the server (e.g. ``{"jobId": "...", "status": "queued"}``).
    """

    def __init__(
        self,
        transport: HTTPTransport,
        job_id: str,
        *,
        job_type: str,
        parser: Callable[[dict[str, Any]], T],
        status_path: str,
        cancel_path: str | None = None,
        sse_path: str | None = None,
        initial: dict[str, Any] | None = None,
        result_loader: ResultLoader | None = None,
    ) -> None:
        self._transport = transport
        self._job_id = job_id
        self._job_type = job_type
        self._parser = parser
        self._status_path = status_path
        self._cancel_path = cancel_path
        self._sse_path = sse_path
        self._result_loader = result_loader
        self._info: JobInfo = JobInfo.model_validate(
            initial if initial is not None else {"id": job_id, "type": job_type, "status": "queued"}
        )
        self._result: T | None = None

    @property
    def id(self) -> str:
        return self._job_id

    @property
    def type(self) -> str:
        return self._job_type

    @property
    def status(self) -> str:
        return self._info.status

    @property
    def progress(self) -> float | None:
        return self._info.progress

    @property
    def info(self) -> JobInfo:
        return self._info

    @property
    def estimated_credits(self) -> int | None:
        return self._info.estimated_credits

    @property
    def session_id(self) -> str | None:
        """Session id from the underlying job result, when present."""
        if self._info.result:
            sid = self._info.result.get("sessionId") or self._info.result.get("session_id")
            if isinstance(sid, str):
                return sid
        extra = getattr(self._info, "model_extra", None) or {}
        sid = extra.get("sessionId") or extra.get("session_id")
        if isinstance(sid, str):
            return sid
        if self._job_type == "generation" and self._job_id.startswith("session"):
            return self._job_id
        return None

    @property
    def is_terminal(self) -> bool:
        return self.status in TERMINAL_STATUSES

    @property
    def succeeded(self) -> bool:
        return self.status in SUCCESS_STATUSES

    @property
    def results(self) -> T:
        """Parsed final result, fetched lazily.

        Raises :class:`LigandAIJobError` if the job failed.
        """
        if self._result is None:
            if not self.is_terminal:
                self.refresh()
            if not self.is_terminal:
                raise LigandAIError(
                    f"Job {self._job_id} not yet complete (status={self.status}). "
                    "Call .wait() first or check .status."
                )
            if not self.succeeded:
                raise LigandAIJobError(
                    self._info.error_message or f"Job {self._job_id} ended with status {self.status}",
                    job_id=self._job_id,
                    job_status=self.status,
                )
            payload = self._result_payload()
            self._result = self._parser(payload)
        return self._result

    def _result_payload(self) -> dict[str, Any]:
        payload = dict(self._info.result or {})
        if self._result_loader is None:
            return payload
        loaded = self._result_loader(self._info)
        if not loaded:
            return payload
        return _merge_result_payload(payload, loaded)

    def refresh(self) -> Job[T]:
        """Re-fetch the job status from the server."""
        path = self._status_path.format(job_id=self._job_id)
        payload = self._transport.request("GET", path) or {}
        # Server schemas vary slightly by endpoint; normalize to JobInfo fields.
        normalized = _normalize_job_payload(payload, self._job_id, self._job_type)
        self._info = JobInfo.model_validate(normalized)
        return self

    def poll(self) -> JobInfo:
        """Alias for ``.refresh().info``."""
        self.refresh()
        return self._info

    def cancel(self) -> bool:
        """Cancel the job. Returns True if cancellation was accepted."""
        if not self._cancel_path:
            return False
        path = self._cancel_path.format(job_id=self._job_id)
        try:
            self._transport.request("POST", path)
            return True
        except LigandAIError:
            return False

    def wait(
        self,
        timeout: float = DEFAULT_JOB_TIMEOUT_SECS,
        poll_interval: float = DEFAULT_POLL_INTERVAL_SECS,
        on_progress: Callable[[JobInfo], None] | None = None,
    ) -> T:
        """Block until the job completes (or raises) and return the parsed result."""
        deadline = time.monotonic() + timeout
        while not self.is_terminal:
            if time.monotonic() > deadline:
                raise LigandAITimeoutError(
                    f"Job {self._job_id} did not complete within {timeout}s "
                    f"(last status: {self.status})"
                )
            self.refresh()
            if on_progress:
                on_progress(self._info)
            if self.is_terminal:
                break
            time.sleep(poll_interval)
        if not self.succeeded:
            raise LigandAIJobError(
                self._info.error_message or f"Job {self._job_id} ended with status {self.status}",
                job_id=self._job_id,
                job_status=self.status,
            )
        return self.results

    def stream(self) -> Iterator[JobEvent]:
        """Stream live progress events via SSE.

        If the job has no SSE endpoint, falls back to polling and yielding a
        synthetic event per status change.
        """
        if self._sse_path:
            path = self._sse_path.format(job_id=self._job_id)
            for line in self._transport.stream_lines("GET", path):
                data = parse_sse_data(line)
                if data is None:
                    continue
                yield JobEvent.model_validate(_normalize_event_payload(data))
        else:
            last_status = self.status
            while not self.is_terminal:
                self.refresh()
                if self.status != last_status or self.progress is not None:
                    yield JobEvent.model_validate(
                        {
                            "eventType": "progress",
                            "stage": self._job_type,
                            "message": self.status,
                            "progress": self.progress,
                            "payload": self._info.model_dump(),
                        }
                    )
                    last_status = self.status
                if self.is_terminal:
                    break
                time.sleep(DEFAULT_POLL_INTERVAL_SECS)


class AsyncJob(Generic[T]):
    """Async sibling of :class:`Job`. See :class:`Job` for full semantics."""

    def __init__(
        self,
        transport: AsyncHTTPTransport,
        job_id: str,
        *,
        job_type: str,
        parser: Callable[[dict[str, Any]], T],
        status_path: str,
        cancel_path: str | None = None,
        sse_path: str | None = None,
        initial: dict[str, Any] | None = None,
        result_loader: AsyncResultLoader | None = None,
    ) -> None:
        self._transport = transport
        self._job_id = job_id
        self._job_type = job_type
        self._parser = parser
        self._status_path = status_path
        self._cancel_path = cancel_path
        self._sse_path = sse_path
        self._result_loader = result_loader
        self._info: JobInfo = JobInfo.model_validate(
            initial if initial is not None else {"id": job_id, "type": job_type, "status": "queued"}
        )
        self._result: T | None = None

    @property
    def id(self) -> str:
        return self._job_id

    @property
    def type(self) -> str:
        return self._job_type

    @property
    def status(self) -> str:
        return self._info.status

    @property
    def progress(self) -> float | None:
        return self._info.progress

    @property
    def info(self) -> JobInfo:
        return self._info

    @property
    def is_terminal(self) -> bool:
        return self.status in TERMINAL_STATUSES

    @property
    def succeeded(self) -> bool:
        return self.status in SUCCESS_STATUSES

    @property
    def session_id(self) -> str | None:
        if self._info.result:
            sid = self._info.result.get("sessionId") or self._info.result.get("session_id")
            if isinstance(sid, str):
                return sid
        extra = getattr(self._info, "model_extra", None) or {}
        sid = extra.get("sessionId") or extra.get("session_id")
        if isinstance(sid, str):
            return sid
        if self._job_type == "generation" and self._job_id.startswith("session"):
            return self._job_id
        return None

    async def refresh(self) -> AsyncJob[T]:
        path = self._status_path.format(job_id=self._job_id)
        payload = await self._transport.request("GET", path) or {}
        normalized = _normalize_job_payload(payload, self._job_id, self._job_type)
        self._info = JobInfo.model_validate(normalized)
        return self

    async def poll(self) -> JobInfo:
        await self.refresh()
        return self._info

    async def cancel(self) -> bool:
        if not self._cancel_path:
            return False
        path = self._cancel_path.format(job_id=self._job_id)
        try:
            await self._transport.request("POST", path)
            return True
        except LigandAIError:
            return False

    async def wait(
        self,
        timeout: float = DEFAULT_JOB_TIMEOUT_SECS,
        poll_interval: float = DEFAULT_POLL_INTERVAL_SECS,
        on_progress: Callable[[JobInfo], None] | None = None,
    ) -> T:
        deadline = time.monotonic() + timeout
        while not self.is_terminal:
            if time.monotonic() > deadline:
                raise LigandAITimeoutError(
                    f"Job {self._job_id} did not complete within {timeout}s "
                    f"(last status: {self.status})"
                )
            await self.refresh()
            if on_progress:
                on_progress(self._info)
            if self.is_terminal:
                break
            await asyncio.sleep(poll_interval)
        if not self.succeeded:
            raise LigandAIJobError(
                self._info.error_message or f"Job {self._job_id} ended with status {self.status}",
                job_id=self._job_id,
                job_status=self.status,
            )
        return await self.async_results()

    async def async_results(self) -> T:
        if self._result is None:
            if not self.is_terminal:
                await self.refresh()
            if not self.is_terminal:
                raise LigandAIError(
                    f"Job {self._job_id} not yet complete (status={self.status})"
                )
            if not self.succeeded:
                raise LigandAIJobError(
                    self._info.error_message or f"Job {self._job_id} ended with status {self.status}",
                    job_id=self._job_id,
                    job_status=self.status,
                )
            payload = await self._result_payload()
            self._result = self._parser(payload)
        return self._result

    async def _result_payload(self) -> dict[str, Any]:
        payload = dict(self._info.result or {})
        if self._result_loader is None:
            return payload
        loaded = self._result_loader(self._info)
        if inspect.isawaitable(loaded):
            loaded = await loaded
        if not loaded:
            return payload
        return _merge_result_payload(payload, loaded)

    async def stream(self) -> AsyncIterator[JobEvent]:
        if self._sse_path:
            path = self._sse_path.format(job_id=self._job_id)
            async for line in self._transport.stream_lines("GET", path):
                data = parse_sse_data(line)
                if data is None:
                    continue
                yield JobEvent.model_validate(_normalize_event_payload(data))
        else:
            last_status = self.status
            while not self.is_terminal:
                await self.refresh()
                if self.status != last_status or self.progress is not None:
                    yield JobEvent.model_validate(
                        {
                            "eventType": "progress",
                            "stage": self._job_type,
                            "message": self.status,
                            "progress": self.progress,
                            "payload": self._info.model_dump(),
                        }
                    )
                    last_status = self.status
                if self.is_terminal:
                    break
                await asyncio.sleep(DEFAULT_POLL_INTERVAL_SECS)


# -- Helpers -----------------------------------------------------------------


def _normalize_job_payload(
    payload: dict[str, Any], job_id: str, job_type: str
) -> dict[str, Any]:
    """Normalize the heterogeneous server status payloads to ``JobInfo`` fields.

    The server has multiple status shapes:

    - Generation: ``{job_id, status, progress, result?}``
    - Folding (PTF): ``{job_id, status, progress_percent?, results?}``
    - Modal callback: ``{jobId, status, ...}``

    We pick the right field on a best-effort basis without rejecting unknown
    keys (they remain in the JobInfo's ``extra``).
    """
    out = dict(payload)
    out.setdefault("id", payload.get("jobId") or payload.get("job_id") or job_id)
    out.setdefault("type", payload.get("type") or job_type)
    out.setdefault("status", payload.get("status") or "queued")
    if "progress" not in out:
        progress = payload.get("progress_percent") or payload.get("progressPercent")
        if progress is not None:
            with contextlib.suppress(TypeError, ValueError):
                out["progress"] = float(progress)
    if "result" not in out:
        result = (
            payload.get("results")
            or payload.get("data")
            or payload.get("output")
        )
        if result is not None:
            out["result"] = result
    if "errorMessage" not in out:
        err = payload.get("error_message") or payload.get("error")
        if isinstance(err, str):
            out["errorMessage"] = err
    return out


def _merge_result_payload(
    base: dict[str, Any], loaded: dict[str, Any]
) -> dict[str, Any]:
    """Merge a hydrated result payload without discarding status-endpoint data."""
    if not base:
        return dict(loaded)
    merged = dict(base)
    for key, value in loaded.items():
        if value is None:
            continue
        if key not in merged or merged[key] in ("", None, [], {}):
            merged[key] = value
    return merged


def _normalize_event_payload(payload: dict[str, Any]) -> dict[str, Any]:
    """Normalize SSE event payloads to :class:`JobEvent` fields."""
    out = {
        "eventType": payload.get("event")
        or payload.get("type")
        or payload.get("stage")
        or "message",
        "stage": payload.get("stage"),
        "message": payload.get("message"),
        "progress": payload.get("progress"),
        "payload": payload,
    }
    return out
