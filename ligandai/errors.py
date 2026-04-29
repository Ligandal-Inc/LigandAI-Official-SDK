# Copyright © 2025 Ligandal, Inc. All rights reserved.
"""Typed exception hierarchy for the LIGANDAI SDK.

All errors raised by the SDK subclass :class:`LigandAIError`. Server-side errors
include a `code` (e.g. ``"E004"``), `message`, and `request_id` from the response.
"""

from __future__ import annotations

from typing import Any


class LigandAIError(Exception):
    """Base class for all SDK errors.

    Attributes
    ----------
    message : str
        Human-readable error message.
    code : str | None
        Server-defined error code (e.g. ``"E001"``, ``"E004"``).
    status_code : int | None
        HTTP status code, when applicable.
    request_id : str | None
        Server-issued request id from ``x-request-id`` header (if present).
    response : dict | None
        Raw response payload, when available.
    """

    def __init__(
        self,
        message: str,
        *,
        code: str | None = None,
        status_code: int | None = None,
        request_id: str | None = None,
        response: dict[str, Any] | None = None,
    ) -> None:
        super().__init__(message)
        self.message = message
        self.code = code
        self.status_code = status_code
        self.request_id = request_id
        self.response = response

    def __repr__(self) -> str:
        parts = [f"message={self.message!r}"]
        if self.code:
            parts.append(f"code={self.code!r}")
        if self.status_code is not None:
            parts.append(f"status_code={self.status_code}")
        if self.request_id:
            parts.append(f"request_id={self.request_id!r}")
        return f"{type(self).__name__}({', '.join(parts)})"


class LigandAIAuthError(LigandAIError):
    """401 — invalid, expired, or revoked API key."""


class LigandAITierError(LigandAIError):
    """403 — feature requires a higher subscription tier.

    Attributes
    ----------
    current_tier : str | None
        The tier of the API key that was used.
    required_tier : str | None
        The minimum tier required to access the feature.
    """

    def __init__(
        self,
        message: str,
        *,
        current_tier: str | None = None,
        required_tier: str | None = None,
        **kwargs: Any,
    ) -> None:
        super().__init__(message, **kwargs)
        self.current_tier = current_tier
        self.required_tier = required_tier


class LigandAIRateLimitError(LigandAIError):
    """429 — rate limit exceeded.

    Attributes
    ----------
    retry_after : float | None
        Seconds to wait before retrying, parsed from ``Retry-After`` or
        ``X-RateLimit-Reset`` headers.
    """

    def __init__(
        self,
        message: str,
        *,
        retry_after: float | None = None,
        **kwargs: Any,
    ) -> None:
        super().__init__(message, **kwargs)
        self.retry_after = retry_after


class LigandAICreditError(LigandAIError):
    """402 — insufficient credits for the operation.

    Attributes
    ----------
    required : int | None
        Credits needed for this operation.
    available : int | None
        Credits currently available on the account.
    """

    def __init__(
        self,
        message: str,
        *,
        required: int | None = None,
        available: int | None = None,
        **kwargs: Any,
    ) -> None:
        super().__init__(message, **kwargs)
        self.required = required
        self.available = available


class LigandAINotFoundError(LigandAIError):
    """404 — resource (gene, complex, job, etc.) not found."""


class LigandAIServerError(LigandAIError):
    """5xx — the LIGANDAI server hit an internal error.

    These are usually transient. The SDK retries automatically with exponential
    backoff up to ``max_retries`` before raising.
    """


class LigandAIValidationError(LigandAIError):
    """400 — request payload failed server-side validation.

    Pydantic validation failures raised on the client side are also wrapped
    in this exception.

    Attributes
    ----------
    errors : list[dict] | None
        Per-field validation errors when available.
    """

    def __init__(
        self,
        message: str,
        *,
        errors: list[dict[str, Any]] | None = None,
        **kwargs: Any,
    ) -> None:
        super().__init__(message, **kwargs)
        self.errors = errors


class LigandAIJobError(LigandAIError):
    """A long-running job (generation/folding) failed on the server.

    Attributes
    ----------
    job_id : str
        The id of the job that failed.
    job_status : str
        Final status, typically ``"failed"`` or ``"cancelled"``.
    """

    def __init__(
        self,
        message: str,
        *,
        job_id: str,
        job_status: str,
        **kwargs: Any,
    ) -> None:
        super().__init__(message, **kwargs)
        self.job_id = job_id
        self.job_status = job_status


class LigandAITimeoutError(LigandAIError):
    """A `.wait()` call exceeded its timeout while polling a job."""


class NotSupportedOnReceptorDB(LigandAIError):
    """Feature is not exposed via the ReceptorDB-restricted client.

    Use :class:`ligandai.LigandAI` with a tier-appropriate API key instead of
    :class:`ligandai.ReceptorDBClient`.
    """


def error_from_response(
    status_code: int,
    payload: dict[str, Any] | None,
    request_id: str | None = None,
    *,
    retry_after: float | None = None,
) -> LigandAIError:
    """Build the right error subclass for an HTTP response.

    The server's error codes (``E001``-``E013``) map to specific subclasses.
    Falls back to status-code-based dispatch when no code is present.
    """
    payload = payload or {}
    message = (
        payload.get("message")
        or payload.get("error")
        or f"HTTP {status_code}"
    )
    code = payload.get("code")

    common: dict[str, Any] = {
        "code": code,
        "status_code": status_code,
        "request_id": request_id,
        "response": payload,
    }

    if status_code == 401:
        return LigandAIAuthError(message, **common)
    if status_code == 402:
        return LigandAICreditError(
            message,
            required=payload.get("required"),
            available=payload.get("available"),
            **common,
        )
    if status_code == 403:
        return LigandAITierError(
            message,
            current_tier=payload.get("currentTier") or payload.get("current_tier"),
            required_tier=payload.get("requiredTier") or payload.get("required_tier"),
            **common,
        )
    if status_code == 404:
        return LigandAINotFoundError(message, **common)
    if status_code == 422 or status_code == 400:
        return LigandAIValidationError(
            message,
            errors=payload.get("errors") or payload.get("details"),
            **common,
        )
    if status_code == 429:
        return LigandAIRateLimitError(
            message,
            retry_after=retry_after,
            **common,
        )
    if 500 <= status_code < 600:
        return LigandAIServerError(message, **common)
    return LigandAIError(message, **common)
