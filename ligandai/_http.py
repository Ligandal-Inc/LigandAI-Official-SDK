# Copyright © 2026 Ligandal, Inc. All rights reserved.
"""Internal HTTP transport: shared sync/async client with retry + backoff.

Both :class:`LigandAI` and :class:`AsyncLigandAI` build resource namespaces
on top of these transports. Resource methods call ``transport.request()``
which handles auth headers, retry, rate-limit parsing, and error mapping.
"""

from __future__ import annotations

import asyncio
import json as _json
import logging
import os
import time
from collections.abc import AsyncIterator, Iterator, Mapping
from typing import Any

import httpx
from tenacity import (
    AsyncRetrying,
    RetryCallState,
    Retrying,
    retry_if_exception_type,
    stop_after_attempt,
    wait_exponential_jitter,
)

from ligandai._constants import (
    DEFAULT_BASE_URL,
    DEFAULT_CONNECT_TIMEOUT_SECS,
    DEFAULT_MAX_RETRIES,
    DEFAULT_RETRY_BASE_DELAY,
    DEFAULT_RETRY_MAX_DELAY,
    DEFAULT_TIMEOUT_SECS,
)
from ligandai._version import __version__
from ligandai.errors import (
    LigandAIConnectionError,
    LigandAIError,
    LigandAIRateLimitError,
    LigandAIServerError,
    error_from_response,
)
from ligandai.server_compat import note_response_headers

logger = logging.getLogger("ligandai")

# HTTP methods that are safe to retry unconditionally — a GET/HEAD/PUT/DELETE/
# OPTIONS is defined to be idempotent, so re-sending it after a dropped
# response (5xx, timeout, connection error) cannot double-apply an effect.
# POST/PATCH are NOT retried unless the caller supplies an idempotency key —
# [bd-LIGANDAI_ALPHA_V2-x4vt8 contract §5] "Never retry non-idempotent POSTs
# unless an idempotency key is sent." A generation/fold submission that
# *did* land server-side but whose response was lost must not be silently
# re-submitted (double credit charge, double GPU job) by the retry wrapper.
_IDEMPOTENT_METHODS: frozenset[str] = frozenset({"GET", "HEAD", "PUT", "DELETE", "OPTIONS"})


class _BackoffWait:
    """Exponential backoff + jitter, honouring the server's ``Retry-After`` /
    ``X-RateLimit-Reset`` when the raised exception carries one.
    [bd-LIGANDAI_ALPHA_V2-x4vt8 contract §5]

    :class:`LigandAIRateLimitError` carries ``retry_after`` parsed from the
    response headers (see :func:`_parse_retry_after`) — previously that value
    was captured on the exception but never fed back into the retry loop's
    wait duration, so the SDK backed off on its OWN fixed exponential curve
    regardless of what the server explicitly asked for. When present,
    ``retry_after`` wins (bounded by ``max_delay`` so a server-supplied value
    can't stall a caller indefinitely); otherwise falls back to jittered
    exponential backoff.
    """

    def __init__(self, base: float, max_delay: float) -> None:
        self._jittered = wait_exponential_jitter(initial=base, max=max_delay)
        self._max_delay = max_delay

    def __call__(self, retry_state: RetryCallState) -> float:
        exc = retry_state.outcome.exception() if retry_state.outcome else None
        retry_after = getattr(exc, "retry_after", None)
        if retry_after is not None:
            try:
                return max(0.0, min(float(retry_after), self._max_delay))
            except (TypeError, ValueError):
                pass
        return self._jittered(retry_state)


def _debug_enabled() -> bool:
    """Whether request-level debug logging is enabled.

    Customers (and the AI agents they hand the SDK to) need a way to see
    what URL the client is actually calling. Set ``LIGANDAI_DEBUG=1`` to
    log every request as ``METHOD URL → STATUS (Xms)`` at DEBUG level on
    the ``ligandai`` logger. Standard logging config still applies — set
    ``logging.getLogger("ligandai").setLevel(logging.DEBUG)`` to see them.
    """
    val = os.environ.get("LIGANDAI_DEBUG", "").strip().lower()
    return val in ("1", "true", "yes", "on")


_RETRYABLE_STATUSES = frozenset({429, 500, 502, 503, 504})


def _parse_retry_after(headers: Mapping[str, str]) -> float | None:
    """Parse ``Retry-After`` or ``X-RateLimit-Reset`` to seconds-to-wait.

    ``Retry-After`` may be either an integer number of seconds or an HTTP-date.
    ``X-RateLimit-Reset`` is typically a Unix timestamp.
    """
    ra = headers.get("retry-after") or headers.get("Retry-After")
    if ra:
        try:
            return float(ra)
        except ValueError:
            # HTTP-date is rare for our server; fall through.
            pass
    reset = headers.get("x-ratelimit-reset") or headers.get("X-RateLimit-Reset")
    if reset:
        try:
            return max(0.0, float(reset) - time.time())
        except ValueError:
            return None
    return None


def _build_user_agent() -> str:
    import platform

    return (
        f"ligandai-python/{__version__} "
        f"(httpx/{httpx.__version__} "
        f"python/{platform.python_version()} {platform.system()})"
    )


def _build_headers(
    api_key: str | None,
    extra: Mapping[str, str] | None = None,
    *,
    impersonate_user: str | None = None,
    client_session_id: str | None = None,
) -> dict[str, str]:
    headers: dict[str, str] = {
        "User-Agent": _build_user_agent(),
        "Accept": "application/json",
    }
    if api_key:
        # The server accepts both x-api-key and Authorization: Bearer.
        # We use Authorization per industry convention.
        headers["Authorization"] = f"Bearer {api_key}"
    if impersonate_user:
        # Internal operator hook; the platform independently authorizes it.
        headers["X-Impersonate-User"] = impersonate_user
    if client_session_id:
        headers["X-LigandAI-Client-Session-Id"] = client_session_id
    if extra:
        headers.update(extra)
    return headers


def _normalize_client_session_id(client_session_id: str | None) -> str | None:
    if client_session_id is None:
        return None
    session_id = client_session_id.strip()
    if not session_id:
        return None
    if len(session_id) > 120:
        raise ValueError("client_session_id must be 120 characters or fewer")
    allowed = set("._:-")
    if any(not (char.isalnum() or char in allowed) for char in session_id):
        raise ValueError("client_session_id may contain only letters, numbers, '.', '_', ':', or '-'")
    return session_id


def _decode_response(resp: httpx.Response) -> dict[str, Any] | None:
    """Decode JSON body, returning None if empty or non-JSON.

    Server endpoints are documented as JSON, but a few (downloads, SSE start)
    return non-JSON responses. We only call this when expecting JSON.
    """
    if not resp.content:
        return None
    ctype = resp.headers.get("content-type", "")
    if "application/json" not in ctype:
        return None
    try:
        return resp.json()  # type: ignore[no-any-return]
    except _json.JSONDecodeError:
        return None


def _raise_for_status(resp: httpx.Response) -> None:
    """Raise the appropriate :class:`LigandAIError` subclass for a bad status.

    Also the SDK's single response funnel. EVERY response — 2xx, 4xx, 5xx, and
    SSE — passes through here before ``request()`` returns or raises, so the
    server's SDK-compatibility advisory headers are consumed exactly once, in
    one place, with no extra round-trip and no per-call-site plumbing.
    [bd-LIGANDAI_ALPHA_V2-x4vt8.3.2] The call is deliberately BEFORE the
    ``is_success`` early return: an outdated client is most worth telling about
    when its request just failed.
    """
    note_response_headers(resp.headers)
    if resp.is_success:
        return
    payload = _decode_response(resp) or {}
    request_id = resp.headers.get("x-request-id")
    retry_after = _parse_retry_after(resp.headers)
    err = error_from_response(
        resp.status_code,
        payload,
        request_id=request_id,
        retry_after=retry_after,
    )
    raise err


def _build_timeout(
    timeout: float | httpx.Timeout,
    connect_timeout: float | None,
) -> httpx.Timeout:
    """Build an explicit connect/read/write/pool timeout. [bd-LIGANDAI_ALPHA_V2-x4vt8]

    A bare float applied to ``httpx.Client(timeout=...)`` sets connect, read,
    write, and pool to the SAME value — a dead TCP handshake then waits as
    long as a caller's generous read timeout for a long GPU job's status
    poll. This gives the connect phase its own (shorter, by default)
    timeout so a network that's simply unreachable fails fast, independent
    of how long the caller is willing to wait for a slow-but-alive server.
    Caller-overridable via ``connect_timeout=`` on ``HTTPTransport`` /
    ``AsyncHTTPTransport`` / ``LigandAI`` / ``AsyncLigandAI``; passing an
    ``httpx.Timeout`` directly as ``timeout`` is honoured as-is.
    """
    if isinstance(timeout, httpx.Timeout):
        return timeout
    total = float(timeout)
    ct = connect_timeout if connect_timeout is not None else min(DEFAULT_CONNECT_TIMEOUT_SECS, total)
    return httpx.Timeout(total, connect=ct)


def _wrap_transport_error(exc: httpx.TransportError, method: str, url: str) -> LigandAIConnectionError:
    """Wrap a raw ``httpx`` transport-level failure in a typed SDK error.
    [bd-LIGANDAI_ALPHA_V2-x4vt8 contract §5] "Connection errors surface as a
    distinct typed error, not a generic one." Covers DNS failures, refused/
    reset connections, TLS handshake failures, and connect/read/write/pool
    timeouts — ``httpx.TimeoutException`` and every other transport-layer
    exception subclass ``httpx.TransportError``.
    """
    return LigandAIConnectionError(
        f"Connection error calling {method.upper()} {url}: {exc}",
        original_error=repr(exc),
    )


# -- Sync transport ----------------------------------------------------------


class HTTPTransport:
    """Synchronous HTTP transport wrapping :class:`httpx.Client`."""

    def __init__(
        self,
        api_key: str | None,
        *,
        base_url: str = DEFAULT_BASE_URL,
        timeout: float | httpx.Timeout = DEFAULT_TIMEOUT_SECS,
        connect_timeout: float | None = None,
        max_retries: int = DEFAULT_MAX_RETRIES,
        impersonate_user: str | None = None,
        client_session_id: str | None = None,
        client: httpx.Client | None = None,
    ) -> None:
        self._api_key = api_key
        self._base_url = base_url.rstrip("/")
        self._timeout = _build_timeout(timeout, connect_timeout)
        self._max_retries = max_retries
        self._wait = _BackoffWait(DEFAULT_RETRY_BASE_DELAY, DEFAULT_RETRY_MAX_DELAY)
        self._impersonate_user = impersonate_user
        self._client_session_id = _normalize_client_session_id(client_session_id)
        self._owns_client = client is None
        self._client = client or httpx.Client(
            timeout=self._timeout,
            follow_redirects=True,
            headers=_build_headers(
                api_key,
                impersonate_user=impersonate_user,
                client_session_id=self._client_session_id,
            ),
        )
        if client is not None and self._client_session_id:
            self._client.headers["X-LigandAI-Client-Session-Id"] = self._client_session_id

    @property
    def base_url(self) -> str:
        return self._base_url

    @property
    def api_key(self) -> str | None:
        return self._api_key

    @property
    def client_session_id(self) -> str | None:
        return self._client_session_id

    def set_client_session_id(self, client_session_id: str | None) -> str | None:
        self._client_session_id = _normalize_client_session_id(client_session_id)
        if self._client_session_id:
            self._client.headers["X-LigandAI-Client-Session-Id"] = self._client_session_id
        else:
            self._client.headers.pop("X-LigandAI-Client-Session-Id", None)
        return self._client_session_id

    def close(self) -> None:
        if self._owns_client:
            self._client.close()

    def __enter__(self) -> HTTPTransport:
        return self

    def __exit__(self, *exc: Any) -> None:
        self.close()

    def request(
        self,
        method: str,
        path: str,
        *,
        params: Mapping[str, Any] | None = None,
        json: Any = None,
        data: Any = None,
        files: Any = None,
        headers: Mapping[str, str] | None = None,
        timeout: float | None = None,
        expect_json: bool = True,
        idempotency_key: str | None = None,
    ) -> Any:
        """Send a request and return the decoded JSON body (or raw bytes when not JSON).

        ``idempotency_key``, when supplied, is sent as the ``Idempotency-Key``
        header AND opts a non-idempotent method (POST/PATCH) into the same
        retry policy as GET/HEAD/PUT/DELETE/OPTIONS.
        [bd-LIGANDAI_ALPHA_V2-x4vt8 contract §5] Without it, POST/PATCH are
        sent AT MOST ONCE by this layer — retrying a POST whose response was
        merely lost (not its effect) risks a duplicate submission (double
        credit charge, double GPU job) that only an idempotency key can make
        safe to re-send.
        """
        url = path if path.startswith("http") else f"{self._base_url}{path}"
        merged_headers = dict(self._client.headers)
        if headers:
            merged_headers.update(headers)
        if idempotency_key:
            merged_headers["Idempotency-Key"] = idempotency_key

        can_retry = method.upper() in _IDEMPOTENT_METHODS or idempotency_key is not None
        retrying = Retrying(
            stop=stop_after_attempt(self._max_retries if can_retry else 1),
            wait=self._wait,
            retry=(
                retry_if_exception_type((LigandAIRateLimitError, LigandAIServerError, LigandAIConnectionError))
                if can_retry
                else retry_if_exception_type(())
            ),
            reraise=True,
        )

        debug = _debug_enabled()
        for attempt in retrying:
            with attempt:
                t0 = time.monotonic() if debug else 0.0
                try:
                    resp = self._client.request(
                        method,
                        url,
                        params=_clean_params(params),
                        json=json,
                        data=data,
                        files=files,
                        headers=merged_headers,
                        timeout=timeout if timeout is not None else self._timeout,
                    )
                except httpx.TransportError as exc:
                    raise _wrap_transport_error(exc, method, url) from exc
                if debug:
                    elapsed_ms = int((time.monotonic() - t0) * 1000)
                    logger.debug(
                        "%s %s -> %s (%dms)",
                        method.upper(),
                        url,
                        resp.status_code,
                        elapsed_ms,
                    )
                _raise_for_status(resp)
                if not expect_json:
                    return resp
                return _decode_response(resp)
        # Unreachable — Retrying.reraise=True ensures we either return or raise.
        raise LigandAIError("retry loop exited without result")  # pragma: no cover

    def stream_lines(
        self,
        method: str,
        path: str,
        *,
        params: Mapping[str, Any] | None = None,
        json: Any = None,
        headers: Mapping[str, str] | None = None,
        timeout: float | None = None,
    ) -> Iterator[str]:
        """Open an SSE-style line stream against a server endpoint.

        Yields one decoded text line at a time. The server's SSE messages are
        prefixed with ``data: `` — callers strip the prefix and parse JSON.
        """
        url = path if path.startswith("http") else f"{self._base_url}{path}"
        merged_headers = dict(self._client.headers)
        merged_headers["Accept"] = "text/event-stream"
        if headers:
            merged_headers.update(headers)

        # [bd-LIGANDAI_ALPHA_V2-x4vt8] A connection-level failure — whether
        # opening the stream or mid-read (server closed the socket, network
        # dropped) — surfaces as a typed LigandAIConnectionError, matching
        # request(). No retry here: SSE streams are not restarted
        # transparently (a caller mid-stream has already consumed partial
        # state), consistent with existing behaviour.
        try:
            with self._client.stream(
                method,
                url,
                params=_clean_params(params),
                json=json,
                headers=merged_headers,
                timeout=timeout if timeout is not None else None,
            ) as resp:
                if not resp.is_success:
                    # ⛔ A streamed response has an UNREAD body. `_raise_for_status`
                    # calls `_decode_response`, which touches `resp.content` and
                    # raises `httpx.ResponseNotRead` — so every error on an SSE
                    # endpoint used to surface as an opaque httpx exception with
                    # the server's JSON body discarded. The legal-acceptance 403
                    # (and its acceptance URL) was destroyed before the error
                    # mapper ever saw it. Pull the body in first so the mapper can
                    # do its job; error bodies are small and already terminal.
                    resp.read()
                _raise_for_status(resp)
                for line in resp.iter_lines():
                    if line:
                        yield line
        except httpx.TransportError as exc:
            raise _wrap_transport_error(exc, method, url) from exc


# -- Async transport ----------------------------------------------------------


class AsyncHTTPTransport:
    """Async HTTP transport wrapping :class:`httpx.AsyncClient`."""

    def __init__(
        self,
        api_key: str | None,
        *,
        base_url: str = DEFAULT_BASE_URL,
        timeout: float | httpx.Timeout = DEFAULT_TIMEOUT_SECS,
        connect_timeout: float | None = None,
        max_retries: int = DEFAULT_MAX_RETRIES,
        impersonate_user: str | None = None,
        client_session_id: str | None = None,
        client: httpx.AsyncClient | None = None,
    ) -> None:
        self._api_key = api_key
        self._base_url = base_url.rstrip("/")
        self._timeout = _build_timeout(timeout, connect_timeout)
        self._max_retries = max_retries
        self._wait = _BackoffWait(DEFAULT_RETRY_BASE_DELAY, DEFAULT_RETRY_MAX_DELAY)
        self._impersonate_user = impersonate_user
        self._client_session_id = _normalize_client_session_id(client_session_id)
        self._owns_client = client is None
        self._client = client or httpx.AsyncClient(
            timeout=self._timeout,
            follow_redirects=True,
            headers=_build_headers(
                api_key,
                impersonate_user=impersonate_user,
                client_session_id=self._client_session_id,
            ),
        )
        if client is not None and self._client_session_id:
            self._client.headers["X-LigandAI-Client-Session-Id"] = self._client_session_id

    @property
    def base_url(self) -> str:
        return self._base_url

    @property
    def api_key(self) -> str | None:
        return self._api_key

    @property
    def client_session_id(self) -> str | None:
        return self._client_session_id

    def set_client_session_id(self, client_session_id: str | None) -> str | None:
        self._client_session_id = _normalize_client_session_id(client_session_id)
        if self._client_session_id:
            self._client.headers["X-LigandAI-Client-Session-Id"] = self._client_session_id
        else:
            self._client.headers.pop("X-LigandAI-Client-Session-Id", None)
        return self._client_session_id

    async def close(self) -> None:
        if self._owns_client:
            await self._client.aclose()

    async def __aenter__(self) -> AsyncHTTPTransport:
        return self

    async def __aexit__(self, *exc: Any) -> None:
        await self.close()

    async def request(
        self,
        method: str,
        path: str,
        *,
        params: Mapping[str, Any] | None = None,
        json: Any = None,
        data: Any = None,
        files: Any = None,
        headers: Mapping[str, str] | None = None,
        timeout: float | None = None,
        expect_json: bool = True,
        idempotency_key: str | None = None,
    ) -> Any:
        """Async sibling of :meth:`HTTPTransport.request` — see its docstring
        for the ``idempotency_key`` / non-idempotent-POST retry policy
        [bd-LIGANDAI_ALPHA_V2-x4vt8 contract §5].
        """
        url = path if path.startswith("http") else f"{self._base_url}{path}"
        merged_headers = dict(self._client.headers)
        if headers:
            merged_headers.update(headers)
        if idempotency_key:
            merged_headers["Idempotency-Key"] = idempotency_key

        can_retry = method.upper() in _IDEMPOTENT_METHODS or idempotency_key is not None
        retrying = AsyncRetrying(
            stop=stop_after_attempt(self._max_retries if can_retry else 1),
            wait=self._wait,
            retry=(
                retry_if_exception_type((LigandAIRateLimitError, LigandAIServerError, LigandAIConnectionError))
                if can_retry
                else retry_if_exception_type(())
            ),
            reraise=True,
        )

        debug = _debug_enabled()
        async for attempt in retrying:
            with attempt:
                t0 = time.monotonic() if debug else 0.0
                try:
                    resp = await self._client.request(
                        method,
                        url,
                        params=_clean_params(params),
                        json=json,
                        data=data,
                        files=files,
                        headers=merged_headers,
                        timeout=timeout if timeout is not None else self._timeout,
                    )
                except httpx.TransportError as exc:
                    raise _wrap_transport_error(exc, method, url) from exc
                if debug:
                    elapsed_ms = int((time.monotonic() - t0) * 1000)
                    logger.debug(
                        "%s %s -> %s (%dms)",
                        method.upper(),
                        url,
                        resp.status_code,
                        elapsed_ms,
                    )
                _raise_for_status(resp)
                if not expect_json:
                    return resp
                return _decode_response(resp)
        raise LigandAIError("retry loop exited without result")  # pragma: no cover

    async def stream_lines(
        self,
        method: str,
        path: str,
        *,
        params: Mapping[str, Any] | None = None,
        json: Any = None,
        headers: Mapping[str, str] | None = None,
        timeout: float | None = None,
    ) -> AsyncIterator[str]:
        url = path if path.startswith("http") else f"{self._base_url}{path}"
        merged_headers = dict(self._client.headers)
        merged_headers["Accept"] = "text/event-stream"
        if headers:
            merged_headers.update(headers)

        # [bd-LIGANDAI_ALPHA_V2-x4vt8] See the sync twin — connection-level
        # failures surface as a typed LigandAIConnectionError.
        try:
            async with self._client.stream(
                method,
                url,
                params=_clean_params(params),
                json=json,
                headers=merged_headers,
                timeout=timeout if timeout is not None else None,
            ) as resp:
                if not resp.is_success:
                    # See the sync twin: an unread streamed body makes
                    # `_raise_for_status` raise `httpx.ResponseNotRead` instead of
                    # the mapped error, discarding the acceptance URL.
                    await resp.aread()
                _raise_for_status(resp)
                async for line in resp.aiter_lines():
                    if line:
                        yield line
        except httpx.TransportError as exc:
            raise _wrap_transport_error(exc, method, url) from exc


# -- Helpers -----------------------------------------------------------------


def _clean_params(params: Mapping[str, Any] | None) -> dict[str, Any] | None:
    """Drop None values so they aren't serialized as ``?foo=None``."""
    if not params:
        return None
    return {k: v for k, v in params.items() if v is not None}


def parse_sse_data(line: str) -> dict[str, Any] | None:
    """Parse an SSE ``data: ...`` line. Returns None for non-data lines."""
    if not line.startswith("data:"):
        return None
    payload = line[5:].lstrip()
    if not payload or payload == "[DONE]":
        return None
    try:
        return _json.loads(payload)  # type: ignore[no-any-return]
    except _json.JSONDecodeError:
        return None


def parse_sse_event_name(line: str) -> str | None:
    """Parse an SSE ``event: <name>`` line. Returns None for non-event lines.

    [bd-LIGANDAI_ALPHA_V2-x4vt8] The server formats every SSE message as
    ``event: <name>\\ndata: <json>\\n\\n`` (see ``generate.py``'s
    ``_sse_event()`` helper) WITHOUT repeating ``<name>`` inside the JSON
    payload. :func:`parse_sse_data` only reads ``data:`` lines, so without
    this helper the standalone ``event:`` line is silently dropped and
    :func:`~ligandai.jobs._normalize_event_payload` falls back to the
    generic ``"message"`` event type for every server-sent event that
    doesn't independently carry a ``type``/``event``/``stage`` key in its
    JSON body — which is exactly how ``target_residues_applied`` /
    ``avoid_residues_applied`` / ``targeting_warning`` used to collapse into
    indistinguishable ``"message"`` events. Callers pair this with
    :func:`parse_sse_data`, tracking the most recent event name across
    lines — see ``Job.stream()`` / ``AsyncJob.stream()``.
    """
    if not line.startswith("event:"):
        return None
    name = line[6:].strip()
    return name or None


async def asleep(secs: float) -> None:
    await asyncio.sleep(secs)
