# Copyright © 2026 Ligandal, Inc. All rights reserved.
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
from ligandai._fold_time_model import estimate_fold_time, format_eta
from ligandai._http import AsyncHTTPTransport, HTTPTransport, parse_sse_data
from ligandai.errors import (
    LigandAIError,
    LigandAIIncompleteResult,
    LigandAIJobError,
    LigandAITimeoutError,
    LigandAIWaitTimeout,
)
from ligandai.types import JobEvent, JobInfo

T = TypeVar("T")
TERMINAL_STATUSES = frozenset({
    "complete", "completed", "failed", "cancelled", "error",
    "generation_complete", "fold_complete",
})
SUCCESS_STATUSES = frozenset({"complete", "completed", "generation_complete", "fold_complete"})

# Durable-success keys to look for in a fold result. When
# ``Job.wait(durable=True)`` is set (the default), the SDK refuses to return a
# "succeeded" Job whose result payload contains none of these — that state
# means the result callback never landed (status flipped to "completed" at
# spawn time with only ``{call_id, message, spawned}`` persisted as result).
_FOLD_PDB_KEYS = ("pdbContent", "pdb_content", "pdb_data", "pdbData", "pdb")
_FOLD_CIF_KEYS = ("cifContent", "cif_content", "cif_data", "cifData", "cif")
_FOLD_HAS_STRUCT_KEYS = ("hasStructure", "has_structure")
_FOLD_SPAWN_ONLY_KEYS = frozenset({"call_id", "job_id", "message", "spawned", "status"})


def _fold_has_durable_payload(info: "JobInfo", payload: dict[str, Any] | None) -> tuple[bool, list[str], str | None]:
    """Inspect a fold-job result payload to decide whether it carries durable
    structural data, or whether ``status='completed'`` is reporting prematurely.

    Returns
    -------
    (durable, missing_fields, call_id)
        ``durable`` — True when ``pdb_content`` (or ``cif_content``) is non-empty,
        OR when the server explicitly set ``has_structure=True``.
        ``missing_fields`` — names of the keys the SDK looked for and did not
        find. Empty when durable.
        ``call_id`` — compute call id when present in the partial result
        (so callers can hit the recovery endpoint with it).
    """
    result = (payload or {}) if isinstance(payload, dict) else {}
    if not isinstance(result, dict):
        result = {}

    # Server top-level may also carry hasStructure / pdbContent — pull both.
    extras = (getattr(info, "model_extra", None) or {}) if info is not None else {}
    has_struct_flags: list[bool] = []
    for src in (result, extras):
        if not isinstance(src, dict):
            continue
        for k in _FOLD_HAS_STRUCT_KEYS:
            v = src.get(k)
            if isinstance(v, bool):
                has_struct_flags.append(v)

    pdb_found = False
    cif_found = False
    for src in (result, extras):
        if not isinstance(src, dict):
            continue
        for k in _FOLD_PDB_KEYS:
            v = src.get(k)
            if isinstance(v, str) and v.strip():
                pdb_found = True
                break
        for k in _FOLD_CIF_KEYS:
            v = src.get(k)
            if isinstance(v, str) and v.strip():
                cif_found = True
                break

    server_has_struct = any(has_struct_flags) if has_struct_flags else None

    # Durable iff we have actual structural content OR the server explicitly
    # acknowledges it (which only happens after the webhook has landed and
    # rebuilt the canonicalized result).
    if pdb_found or cif_found:
        return True, [], None
    if server_has_struct is True:
        # Server flipped has_structure=True without the SDK seeing the content —
        # this means the result is in gpu_jobs.output_data and we'd need a richer
        # status fetch. Treat as durable to avoid infinite loops; the
        # result_loader (if installed) will hydrate the content next.
        return True, [], None

    call_id = None
    if isinstance(result, dict):
        cid = result.get("call_id") or result.get("callId")
        if isinstance(cid, str):
            call_id = cid

    missing: list[str] = []
    if not pdb_found:
        missing.append("pdb_data")
    if not cif_found:
        missing.append("cif_data")
    if server_has_struct is False:
        missing.append("has_structure")
    return False, missing, call_id


def _result_payload_is_spawn_ack_only(payload: dict[str, Any] | None) -> bool:
    """Heuristic: a result dict containing only the spawn-acknowledgement keys
    (``call_id``, ``job_id``, ``message``, ``spawned``, ``status``) means the
    result callback never landed."""
    if not isinstance(payload, dict) or not payload:
        return False
    keys = {k for k in payload.keys() if k}
    if not keys:
        return False
    return keys.issubset(_FOLD_SPAWN_ONLY_KEYS) and "call_id" in keys
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

    def _compute_eta_seconds(self) -> float | None:
        """best-effort fold ETA from the job info.

        Returns None when the necessary fields aren't present (e.g. generation
        jobs, where the fit doesn't apply). For fold jobs, falls back to
        reasonable defaults if extras are missing — better an approximate ETA
        than no signal at all.
        """
        if self._job_type != "folding":
            return None
        extras = (getattr(self._info, "model_extra", None) or {})
        result = self._info.result or {}

        def _get(*keys: str, default: float | int | None = None):
            for src in (extras, result):
                for k in keys:
                    if isinstance(src, dict) and src.get(k) is not None:
                        return src[k]
            return default

        L = int(_get("protein_length", "L", "n_residues", default=300) or 300)
        n_traj = int(_get("num_trajectories", "diffusion_samples", default=1) or 1)
        n_gpu = int(_get("n_parallel_gpus", "fold_gpu_count", default=1) or 1)
        sampling = int(_get("sampling_steps", default=15) or 15)
        recycling = int(_get("recycling_steps", default=3) or 3)
        return estimate_fold_time(
            protein_length=L,
            num_trajectories=n_traj,
            n_parallel_gpus=n_gpu,
            sampling_steps=sampling,
            recycling_steps=recycling,
        )

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
        save_to: str | None = None,
        *,
        durable: bool = True,
    ) -> T:
        """Block until the job completes (or raises) and return the parsed result.

        Args:
            save_to: Optional local directory. When provided AND the result
                exposes a ``save_to(...)`` method (i.e. ``GenerationResult``),
                automatically writes ``peptides.csv``, ``folds/*.pdb``, and
                ``summary.json`` to that directory. Prints a one-line confirmation.
                Pass an empty string to use the SDK default
                ``./ligandai_runs/<session_id>/``.
            durable: When True (default), ``wait()`` does NOT return until the
                result payload carries durable structural data (``pdb_data``
                non-empty OR ``has_structure`` true) for fold jobs. This guards
                against the case where ``status='completed'`` is reported with
                only the spawn acknowledgement (``{call_id, message, spawned}``)
                stored as result — i.e. the result callback never landed. Pass
                ``durable=False`` to opt back into the fast-but-permissive
                behavior.

                When ``durable=True`` and ``timeout`` elapses with no PDB,
                raises :class:`~ligandai.errors.LigandAIWaitTimeout` carrying
                the captured ``call_id`` so callers can hit the
                ``/recover-from-modal`` endpoint manually.
        """
        deadline = time.monotonic() + timeout
        # compute one-shot fold ETA up front so on_progress can report it.
        # We probe the job info for protein_length / num_trajectories / n_parallel_gpus;
        # missing fields fall back to typical defaults (L=300, traj=1, gpus=1).
        eta_seconds = self._compute_eta_seconds() if self._job_type == "folding" else None
        wait_start = time.monotonic()

        def _maybe_emit_progress() -> None:
            if not on_progress:
                return
            if eta_seconds is not None:
                elapsed = time.monotonic() - wait_start
                remaining = max(eta_seconds - elapsed, 0)
                try:
                    self._info.__dict__.setdefault("eta_seconds", remaining)
                    self._info.__dict__["eta_seconds"] = remaining
                    self._info.__dict__["eta_human"] = format_eta(remaining)
                except Exception:
                    pass
            on_progress(self._info)

        while not self.is_terminal:
            if time.monotonic() > deadline:
                raise LigandAITimeoutError(
                    f"Job {self._job_id} did not complete within {timeout}s "
                    f"(last status: {self.status})"
                )
            self.refresh()
            _maybe_emit_progress()
            if self.is_terminal:
                break
            time.sleep(poll_interval)
        if not self.succeeded:
            raise LigandAIJobError(
                self._info.error_message or f"Job {self._job_id} ended with status {self.status}",
                job_id=self._job_id,
                job_status=self.status,
            )

        # Durable-success contract for fold jobs. Outer status="completed" can
        # fire BEFORE the result callback posts the real PDB payload — the SDK
        # must NOT return until pdb_data lands, otherwise calling code sees
        # iptm=None and pdb_written=False. Re-poll until durable OR the caller's
        # timeout elapses.
        if durable and self._job_type == "folding":
            last_call_id: str | None = None
            last_missing: list[str] = []
            while True:
                durable_ok, missing, call_id = _fold_has_durable_payload(
                    self._info, self._info.result if self._info else None,
                )
                if durable_ok:
                    break
                last_call_id = call_id or last_call_id
                last_missing = missing or last_missing
                if time.monotonic() > deadline:
                    raise LigandAIWaitTimeout(
                        (
                            f"Job {self._job_id} reported status={self.status!r} "
                            f"but structural payload never landed within {timeout}s "
                            f"(missing fields: {missing or last_missing}). "
                            "This usually means the result callback did not fire — "
                            "use client.folds.recover(job_id) or pass "
                            "durable=False to opt out."
                        ),
                        job_id=self._job_id,
                        job_status=self.status,
                        missing_fields=last_missing,
                        call_id=last_call_id,
                        server_state=(self._info.result or {}) if self._info else None,
                    )
                _maybe_emit_progress()
                time.sleep(max(poll_interval, 1.0))
                self.refresh()

        result = self.results
        if save_to is not None and hasattr(result, "save_to"):
            try:
                target_dir = save_to or f"./ligandai_runs/{self._job_id}"
                info = result.save_to(target_dir, transport=self._transport)
                print(
                    f"[ligandai] saved {info['peptide_count']} peptides "
                    f"({info['pdb_count']} PDBs) to {info['directory']}"
                )
                print(f"[ligandai] view on platform: {info['view_url']}")
            except Exception as exc:
                print(f"[ligandai] save_to skipped: {exc}")
        return result

    def stream(self) -> Iterator[JobEvent]:
        """Stream live progress events via SSE.

        when SSE returns 404 (the legacy SDK pointed
        ``Job.stream()`` at ``/api/jobs/{id}/sse`` which never existed on the
        server), we transparently fall back to polling the status endpoint
        and yielding synthetic events. The caller sees the same
        :class:`~ligandai.types.JobEvent` stream either way.

        For fold jobs the stream now also yields a terminal ``"complete"``
        event whose ``payload`` is the durable result dict — so callers that
        do ``for event in job.stream(): ...`` get the PDB content immediately
        in the final tick without needing a separate ``job.results`` call.
        """
        if self._sse_path:
            path = self._sse_path.format(job_id=self._job_id)
            try:
                for line in self._transport.stream_lines("GET", path):
                    data = parse_sse_data(line)
                    if data is None:
                        continue
                    yield JobEvent.model_validate(_normalize_event_payload(data))
                # The SSE endpoint closed cleanly. Refresh once and emit a
                # terminal event so consumers see the durable payload.
                self.refresh()
                yield JobEvent.model_validate(
                    {
                        "eventType": "complete" if self.succeeded else "failed",
                        "stage": self._job_type,
                        "message": self.status,
                        "progress": 1.0 if self.succeeded else self.progress,
                        "payload": self._info.model_dump(),
                    }
                )
                return
            except LigandAIError as sse_err:
                # legacy server doesn't expose this SSE path — fall
                # back to polling. Future server versions will route the
                # `/logs/stream` endpoint through here.
                if getattr(sse_err, "status_code", None) == 404:
                    pass
                else:
                    raise

        last_status = self.status
        last_progress = self.progress
        while True:
            self.refresh()
            if (
                self.status != last_status
                or (self.progress is not None and self.progress != last_progress)
            ):
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
                last_progress = self.progress
            if self.is_terminal:
                # For fold jobs, additionally wait for the durable payload.
                if self._job_type == "folding":
                    durable_ok, _, _ = _fold_has_durable_payload(
                        self._info, self._info.result if self._info else None,
                    )
                    if not durable_ok and self.succeeded:
                        time.sleep(max(DEFAULT_POLL_INTERVAL_SECS, 1.0))
                        continue
                yield JobEvent.model_validate(
                    {
                        "eventType": "complete" if self.succeeded else "failed",
                        "stage": self._job_type,
                        "message": self.status,
                        "progress": 1.0 if self.succeeded else self.progress,
                        "payload": self._info.model_dump(),
                    }
                )
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
        *,
        durable: bool = True,
    ) -> T:
        """Async sibling of :meth:`Job.wait`. See :meth:`Job.wait` for full
        ``durable=True`` semantics."""
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

        if durable and self._job_type == "folding":
            last_call_id: str | None = None
            last_missing: list[str] = []
            while True:
                durable_ok, missing, call_id = _fold_has_durable_payload(
                    self._info, self._info.result if self._info else None,
                )
                if durable_ok:
                    break
                last_call_id = call_id or last_call_id
                last_missing = missing or last_missing
                if time.monotonic() > deadline:
                    raise LigandAIWaitTimeout(
                        (
                            f"Job {self._job_id} reported status={self.status!r} "
                            f"but structural payload never landed within {timeout}s "
                            f"(missing fields: {missing or last_missing}). "
                            "This usually means the result callback did not fire — "
                            "use client.folds.recover(job_id) or pass "
                            "durable=False to opt out."
                        ),
                        job_id=self._job_id,
                        job_status=self.status,
                        missing_fields=last_missing,
                        call_id=last_call_id,
                        server_state=(self._info.result or {}) if self._info else None,
                    )
                if on_progress:
                    on_progress(self._info)
                await asyncio.sleep(max(poll_interval, 1.0))
                await self.refresh()

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
        """Async sibling of :meth:`Job.stream`."""
        if self._sse_path:
            path = self._sse_path.format(job_id=self._job_id)
            try:
                async for line in self._transport.stream_lines("GET", path):
                    data = parse_sse_data(line)
                    if data is None:
                        continue
                    yield JobEvent.model_validate(_normalize_event_payload(data))
                await self.refresh()
                yield JobEvent.model_validate(
                    {
                        "eventType": "complete" if self.succeeded else "failed",
                        "stage": self._job_type,
                        "message": self.status,
                        "progress": 1.0 if self.succeeded else self.progress,
                        "payload": self._info.model_dump(),
                    }
                )
                return
            except LigandAIError as sse_err:
                if getattr(sse_err, "status_code", None) == 404:
                    pass
                else:
                    raise

        last_status = self.status
        last_progress = self.progress
        while True:
            await self.refresh()
            if (
                self.status != last_status
                or (self.progress is not None and self.progress != last_progress)
            ):
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
                last_progress = self.progress
            if self.is_terminal:
                if self._job_type == "folding":
                    durable_ok, _, _ = _fold_has_durable_payload(
                        self._info, self._info.result if self._info else None,
                    )
                    if not durable_ok and self.succeeded:
                        await asyncio.sleep(max(DEFAULT_POLL_INTERVAL_SECS, 1.0))
                        continue
                yield JobEvent.model_validate(
                    {
                        "eventType": "complete" if self.succeeded else "failed",
                        "stage": self._job_type,
                        "message": self.status,
                        "progress": 1.0 if self.succeeded else self.progress,
                        "payload": self._info.model_dump(),
                    }
                )
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
    - Folding (PTF): also returns top-level ``hasStructure`` flag — copied
      into ``result`` so :func:`_fold_has_durable_payload` can see it without
      re-parsing the raw response shape.
    - Async result callback: ``{jobId, status, ...}``

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

    # bubble the top-level hasStructure / modalStage
    # flags down INTO the result dict so durable-success checks can see them
    # regardless of which shape the server returned. The fold-jobs endpoint
    # emits hasStructure at the top level (see foldingJobsDb.get), but the
    # SDK's JobInfo.result is the canonical field that downstream parsers
    # read. Without this propagation, durable=True would loop forever waiting
    # on a flag stashed in JobInfo.model_extra that the result-merge path
    # never touches.
    has_struct = payload.get("hasStructure")
    if has_struct is None:
        has_struct = payload.get("has_structure")
    modal_stage = payload.get("modalStage") or payload.get("modal_stage")
    if has_struct is not None or modal_stage is not None:
        existing = out.get("result")
        if isinstance(existing, dict):
            merged = dict(existing)
        elif existing is None:
            merged = {}
        else:
            # Result is a list or scalar — preserve it under a key so we don't
            # silently lose the data, but DO NOT clobber.
            merged = {"_legacy_result": existing}
        if has_struct is not None and "hasStructure" not in merged and "has_structure" not in merged:
            merged["hasStructure"] = bool(has_struct)
        if modal_stage is not None and "modalStage" not in merged and "modal_stage" not in merged:
            merged["modalStage"] = modal_stage
        out["result"] = merged

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
