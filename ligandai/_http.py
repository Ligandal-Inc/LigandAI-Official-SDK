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
    Retrying,
    retry_if_exception_type,
    stop_after_attempt,
    wait_exponential,
)

from ligandai._constants import (
    DEFAULT_BASE_URL,
    DEFAULT_MAX_RETRIES,
    DEFAULT_RETRY_BASE_DELAY,
    DEFAULT_RETRY_MAX_DELAY,
    DEFAULT_TIMEOUT_SECS,
)
from ligandai._version import __version__
from ligandai.errors import (
    LigandAIError,
    LigandAIRateLimitError,
    LigandAIServerError,
    error_from_response,
)

logger = logging.getLogger("ligandai")


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
    """Raise the appropriate :class:`LigandAIError` subclass for a bad status."""
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


# -- Sync transport ----------------------------------------------------------


class HTTPTransport:
    """Synchronous HTTP transport wrapping :class:`httpx.Client`."""

    def __init__(
        self,
        api_key: str | None,
        *,
        base_url: str = DEFAULT_BASE_URL,
        timeout: float = DEFAULT_TIMEOUT_SECS,
        max_retries: int = DEFAULT_MAX_RETRIES,
        impersonate_user: str | None = None,
        client_session_id: str | None = None,
        client: httpx.Client | None = None,
    ) -> None:
        self._api_key = api_key
        self._base_url = base_url.rstrip("/")
        self._timeout = timeout
        self._max_retries = max_retries
        self._impersonate_user = impersonate_user
        self._client_session_id = _normalize_client_session_id(client_session_id)
        self._owns_client = client is None
        self._client = client or httpx.Client(
            timeout=timeout,
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
    ) -> Any:
        """Send a request and return the decoded JSON body (or raw bytes when not JSON)."""
        url = path if path.startswith("http") else f"{self._base_url}{path}"
        merged_headers = dict(self._client.headers)
        if headers:
            merged_headers.update(headers)

        retrying = Retrying(
            stop=stop_after_attempt(self._max_retries),
            wait=wait_exponential(
                multiplier=DEFAULT_RETRY_BASE_DELAY,
                max=DEFAULT_RETRY_MAX_DELAY,
            ),
            retry=retry_if_exception_type((LigandAIRateLimitError, LigandAIServerError, httpx.TransportError)),
            reraise=True,
        )

        debug = _debug_enabled()
        for attempt in retrying:
            with attempt:
                t0 = time.monotonic() if debug else 0.0
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

        with self._client.stream(
            method,
            url,
            params=_clean_params(params),
            json=json,
            headers=merged_headers,
            timeout=timeout if timeout is not None else None,
        ) as resp:
            _raise_for_status(resp)
            for line in resp.iter_lines():
                if line:
                    yield line


# -- Async transport ----------------------------------------------------------


class AsyncHTTPTransport:
    """Async HTTP transport wrapping :class:`httpx.AsyncClient`."""

    def __init__(
        self,
        api_key: str | None,
        *,
        base_url: str = DEFAULT_BASE_URL,
        timeout: float = DEFAULT_TIMEOUT_SECS,
        max_retries: int = DEFAULT_MAX_RETRIES,
        impersonate_user: str | None = None,
        client_session_id: str | None = None,
        client: httpx.AsyncClient | None = None,
    ) -> None:
        self._api_key = api_key
        self._base_url = base_url.rstrip("/")
        self._timeout = timeout
        self._max_retries = max_retries
        self._impersonate_user = impersonate_user
        self._client_session_id = _normalize_client_session_id(client_session_id)
        self._owns_client = client is None
        self._client = client or httpx.AsyncClient(
            timeout=timeout,
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
    ) -> Any:
        url = path if path.startswith("http") else f"{self._base_url}{path}"
        merged_headers = dict(self._client.headers)
        if headers:
            merged_headers.update(headers)

        retrying = AsyncRetrying(
            stop=stop_after_attempt(self._max_retries),
            wait=wait_exponential(
                multiplier=DEFAULT_RETRY_BASE_DELAY,
                max=DEFAULT_RETRY_MAX_DELAY,
            ),
            retry=retry_if_exception_type((LigandAIRateLimitError, LigandAIServerError, httpx.TransportError)),
            reraise=True,
        )

        debug = _debug_enabled()
        async for attempt in retrying:
            with attempt:
                t0 = time.monotonic() if debug else 0.0
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

        async with self._client.stream(
            method,
            url,
            params=_clean_params(params),
            json=json,
            headers=merged_headers,
            timeout=timeout if timeout is not None else None,
        ) as resp:
            _raise_for_status(resp)
            async for line in resp.aiter_lines():
                if line:
                    yield line


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


async def asleep(secs: float) -> None:
    await asyncio.sleep(secs)
