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
    retry_if_exception,
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
    LigandAIUnreadableResponse,
    body_excerpt,
    detect_edge,
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


def _request_never_sent(exc: BaseException) -> bool:
    """True when the failure happened BEFORE any request byte left this process.

    ⛔ A refused connection (``httpx.ConnectError``) or a connect-phase timeout
    (``httpx.ConnectTimeout``) means the TCP/TLS handshake never completed, so
    the server cannot have seen, let alone applied, the request. Retrying it is
    safe for ANY method — the duplicate-submission hazard the POST/PATCH rule
    above exists for (a response lost AFTER the effect landed) cannot occur.
    Without this a generate / fold / cofold POST sent during a server restart
    failed hard with ``Connection refused`` while a GET ten milliseconds later
    rode the same restart out. Measured 2026-10-02: a ``peptides.fold`` POST hit
    the ~15 s gap while production blue was being redeployed.
    A read timeout or a reset mid-response is NOT in this set: those may have
    been applied. [bd-dre-3imtv.4]
    """
    return isinstance(exc, LigandAIConnectionError) and bool(
        getattr(exc, "request_not_sent", False)
    )


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


# Why a JSON body could not be produced. `None` means it WAS produced.
# These names are the whole point: a caller (and a log grep) can tell an
# HTML login page from a truncated payload from a genuinely empty response,
# where before all three were spelled `None`.
_DECODE_OK = None
_DECODE_EMPTY = "empty_body"
_DECODE_NULL = "null_document"
_DECODE_NOT_JSON = "non_json_content_type"
_DECODE_MALFORMED = "malformed_json"


def _decode_response_verbose(
    resp: httpx.Response,
) -> tuple[dict[str, Any] | None, str | None]:
    """Decode a JSON body, returning ``(payload, failure_cause)``.

    ⛔ THE ORIGINAL SIGNATURE IS THE DEFECT. ``_decode_response`` returned a
    bare ``None`` for THREE unrelated situations — an empty body, a body that
    is not JSON at all (an HTML login page, an SPA shell, a CDN challenge),
    and a declared-JSON body that does not parse — and every resource method
    in this SDK then wrote ``or {}`` / ``or []`` over the top of it. So "the
    proxy handed me a web page instead of the API" arrived at the caller as
    ``designs: []`` and ``proposed: 0``: a clean, plausible, WRONG
    measurement, with nothing anywhere saying a lookup had failed.

    This returns the cause alongside, so the transport can raise something
    that names what happened instead of returning a falsy value that reads
    as data.
    """
    if not resp.content:
        return None, _DECODE_EMPTY
    ctype = resp.headers.get("content-type", "")
    if "application/json" not in ctype:
        return None, _DECODE_NOT_JSON
    try:
        parsed = resp.json()
    except _json.JSONDecodeError:
        return None, _DECODE_MALFORMED
    # ⛔ A literal JSON `null` parses SUCCESSFULLY to Python None — the same
    # value this function returns for "could not decode". Callers then write
    # `... or {}` over it and get an empty document.
    #
    # Observed live on 2026-09-21: GET https://ligandai.com/api/auth/user
    # answers `200` with the body `null` when the presented credential was not
    # accepted (verified: a malformed key gives 200+null, while a well-formed
    # `lgai_` key on the same route returns the full user). `Account.me()`
    # turned that into `User.model_validate({})` and raised
    # "Field required: id" — a complaint about our own schema for what is
    # really "the server did not identify you". Name it instead.
    if parsed is None:
        return None, _DECODE_NULL
    return parsed, _DECODE_OK  # type: ignore[no-any-return]


def _decode_response(resp: httpx.Response) -> dict[str, Any] | None:
    """Back-compat wrapper: payload or None. Prefer
    :func:`_decode_response_verbose` on any path that reports a measurement.
    """
    payload, _cause = _decode_response_verbose(resp)
    return payload


def _raw_text(resp: httpx.Response) -> str | None:
    """Best-effort decode of the response body to text, for diagnosis only.

    Never raises: this runs on the error path, and an undecodable body must
    not replace the error the caller is already being told about.
    """
    try:
        if not resp.content:
            return None
        return resp.text
    except Exception:
        return None


def _unreadable_body_error(
    resp: httpx.Response,
    cause: str,
    method: str,
    url: str,
    api_key: str | None = None,
) -> LigandAIUnreadableResponse:
    """Build the error for a SUCCESS status whose body the SDK cannot read.

    [bd-dre-rp11j.3] This is the guard against the six-week silent no-op: a
    request that "succeeded" but whose body was unusable used to return
    ``None``, become ``{}`` or ``[]`` at the call site, and be reported as a
    measurement of zero.
    """
    ctype = resp.headers.get("content-type") or None
    excerpt = body_excerpt(_raw_text(resp))
    edge, ray_id = detect_edge(resp.headers, _raw_text(resp), ctype)

    what = {
        _DECODE_NOT_JSON: (
            f"the body is {ctype or 'of unknown type'}, not JSON"
        ),
        _DECODE_MALFORMED: "the body claims to be JSON but does not parse",
        _DECODE_EMPTY: "the body is empty",
        _DECODE_NULL: "the body is the literal JSON document `null`",
    }.get(cause, cause)

    lines = [
        f"HTTP {resp.status_code} from {method.upper()} {url}, but {what}.",
        "This is NOT an empty result — the SDK could not read the response at "
        "all, and refuses to report that as zero/none/no-data.",
    ]
    if edge:
        lines.append(
            f"  likely cause   : {edge} answered instead of the API "
            f"(ray id {ray_id})." if ray_id
            else f"  likely cause   : {edge} answered instead of the API."
        )
        lines.append(
            "  what to do     : point the client at a non-proxied origin, or "
            "have it allowlisted for the API paths."
        )
    elif cause == _DECODE_NOT_JSON:
        lines.append(
            "  likely cause   : base_url points at a web front end or a proxy "
            "rather than the API, or an intermediary rewrote the response."
        )
        lines.append(
            f"  what to do     : check base_url (currently reaching {url}); "
            f"set LIGANDAI_BASE_URL or pass base_url= explicitly."
        )
    elif cause == _DECODE_MALFORMED:
        lines.append(
            "  likely cause   : the response was truncated or rewritten in transit."
        )
        lines.append(
            "  what to do     : retry; if it persists, report the request id below."
        )
    elif cause == _DECODE_NULL:
        lines.append(
            "  likely cause   : the route answered 200 with `null`, which on this "
            "platform means it did not identify the caller. The usual reason is a "
            "credential that was rejected — commonly a malformed API key, which "
            "is refused on SHAPE before validity is ever checked."
        )
        # The three causes (absent / malformed / well-formed-but-invalid) are
        # indistinguishable in the RESPONSE — all three yield 200 + `null`.
        # They are distinguishable in the REQUEST, so say which one we sent.
        _g = _auth_guidance_for(api_key)
        lines.append(
            "  what to do     : `null` here is NOT an empty account — the server "
            "could not tell who you are."
        )
        if _g:
            for _l in _g.splitlines():
                lines.append(f"  {_l}" if _l.startswith("  ") else f"  {_l}")
    else:
        lines.append(
            "  likely cause   : the route returned no document — it may not exist "
            "on this server build."
        )
        lines.append(
            "  what to do     : verify the endpoint path and the server version."
        )
    lines.append(f"  content-type   : {ctype or '<none>'}")
    lines.append(f"  body served    : {excerpt or '<empty>'}")
    rid = resp.headers.get("x-request-id")
    if rid:
        lines.append(f"  request id     : {rid}")

    return LigandAIUnreadableResponse(
        "\n".join(lines),
        cause=cause,
        method=method.upper(),
        url=url,
        content_type=ctype,
        body_excerpt=excerpt,
        status_code=resp.status_code,
        request_id=rid,
    )


def _decode_or_raise(
    resp: httpx.Response, method: str, url: str, api_key: str | None = None
) -> Any:
    """Decode a SUCCESS response, raising when the body cannot be read.

    An empty body is tolerated for COMMANDS (POST/PUT/PATCH/DELETE), whose
    callers discard the document and only need the status — a bare 200 ack is
    a normal, correct answer there. It is NOT tolerated for a GET: a GET is a
    measurement, and a measurement that came back with no document is exactly
    the "empty vs could-not-look" conflation this guard exists to break. A
    non-empty body that cannot be parsed always raises, whatever the method.
    """
    payload, cause = _decode_response_verbose(resp)
    if cause is _DECODE_OK:
        return payload
    if cause in (_DECODE_EMPTY, _DECODE_NULL) and method.upper() not in ("GET", "HEAD"):
        return None
    raise _unreadable_body_error(resp, cause, method, url, api_key)


def _auth_guidance_for(api_key: str | None) -> str:
    """Key-shape-specific guidance, imported lazily to avoid a cycle."""
    try:
        from ligandai.client import auth_guidance
        return auth_guidance(api_key)
    except Exception:  # pragma: no cover - guidance must never break a request
        return ""


def _raise_for_status(
    resp: httpx.Response,
    *,
    method: str | None = None,
    url: str | None = None,
    api_key: str | None = None,
) -> None:
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
    # ⛔ [bd-dre-2d7za.9] The raw body used to stop here. `_decode_response`
    # answers None for a non-JSON body, `or {}` turned that into an empty
    # payload, and `error_from_response` had nothing left to describe but the
    # status code — so a proxy-rewritten 403 (and a CDN bot-filter block,
    # bd-dre-3fduj) both became the string "HTTP 403". Hand the mapper the
    # undecoded response so it can name WHO answered and surface whatever
    # explanation the body still carries.
    err = error_from_response(
        resp.status_code,
        payload,
        request_id=request_id,
        retry_after=retry_after,
        raw_body=_raw_text(resp),
        content_type=resp.headers.get("content-type"),
        headers=resp.headers,
        method=method,
        url=url,
        auth_guidance=_auth_guidance_for(api_key) if resp.status_code in (401, 403) else None,
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
    msg = f"Connection error calling {method.upper()} {url}: {exc}"

    # ⛔ [bd-LIGANDAI_ALPHA_V2-ci4wp] A DNS failure against a NON-PUBLISHED host
    # is almost always a stale base_url, not an outage — and it presents as a
    # generic timeout that sends people looking at the network.
    #
    # Observed 2026-08-22: ~/.ligandai/credentials.json carries
    # server_url = https://api.ligandai.com, which DOES NOT RESOLVE
    # (gaierror -5). The SDK's own DEFAULT_BASE_URL is correct
    # (https://ligandai.com) and client.py documents that api.ligandai.com is
    # not a published host — but nothing checked the value it was HANDED, so a
    # user taking that file's default could not reach the API at all and got no
    # hint why.
    #
    # Diagnose only. Never rewrite the caller's base_url: silently redirecting a
    # request to a different host than the caller named is worse than failing.
    _dns = any(t in str(exc).lower() for t in (
        "name or service not known", "nodename nor servname",
        "temporary failure in name resolution", "no address associated",
        "getaddrinfo", "name resolution",
    ))
    if _dns:
        try:
            from urllib.parse import urlsplit
            _host = (urlsplit(url).hostname or "").lower()
        except Exception:
            _host = ""
        _pub = (DEFAULT_BASE_URL.split("//", 1)[-1].split("/", 1)[0] or "").lower()
        # ⛔ NO SUBDOMAIN EXEMPTION. The first version of this check exempted
        # anything ending in ".ligandai.com" — which is exactly what
        # api.ligandai.com is, so the guard silently skipped the one case it was
        # written for. A host that DID NOT RESOLVE is worth diagnosing whether or
        # not it looks related to the published one.
        if _host and _host != _pub:
            msg += (
                f"\n\nThe hostname {_host!r} did not resolve, and it is not the "
                f"published API host. The SDK default is {DEFAULT_BASE_URL!r}.\n"
                f"  * If you passed base_url explicitly, check it.\n"
                f"  * If you did not, something handed one to the client — a stale "
                f"~/.ligandai/credentials.json 'server_url' is the known cause; "
                f"'api.ligandai.com' in particular has never been a published host.\n"
                f"  * Set LIGANDAI_BASE_URL={DEFAULT_BASE_URL} to override."
            )

    err = LigandAIConnectionError(msg, original_error=repr(exc))
    # Handshake never completed -> nothing was transmitted. See _request_never_sent.
    err.request_not_sent = isinstance(exc, (httpx.ConnectError, httpx.ConnectTimeout))
    return err


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
            # A non-idempotent call is still retried when the failure proves the
            # request was never sent (refused / connect-timeout); nothing else.
            stop=stop_after_attempt(self._max_retries),
            wait=self._wait,
            retry=(
                retry_if_exception_type((LigandAIRateLimitError, LigandAIServerError, LigandAIConnectionError))
                if can_retry
                else retry_if_exception(_request_never_sent)
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
                _raise_for_status(resp, method=method, url=url, api_key=self._api_key)
                if not expect_json:
                    return resp
                return _decode_or_raise(resp, method, url, self._api_key)
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
                _raise_for_status(resp, method=method, url=url, api_key=self._api_key)
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
            # A non-idempotent call is still retried when the failure proves the
            # request was never sent (refused / connect-timeout); nothing else.
            stop=stop_after_attempt(self._max_retries),
            wait=self._wait,
            retry=(
                retry_if_exception_type((LigandAIRateLimitError, LigandAIServerError, LigandAIConnectionError))
                if can_retry
                else retry_if_exception(_request_never_sent)
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
                _raise_for_status(resp, method=method, url=url, api_key=self._api_key)
                if not expect_json:
                    return resp
                return _decode_or_raise(resp, method, url, self._api_key)
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
                _raise_for_status(resp, method=method, url=url, api_key=self._api_key)
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
