# Copyright © 2026 Ligandal, Inc. All rights reserved.
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


class LigandAIPaidTierRequired(LigandAIError):
    """402 — the SDK requires a paid subscription.

    Distinct from :class:`LigandAICreditError` (which means "your tier is fine
    but you've run out of credits"). This error means the **API key itself**
    resolves to a tier that does not include the requested paid surface.

    The server may return this on `/api/v1/peptides/*` requests from a
    free-tier key with::

        HTTP 402 Payment Required
        {"error": "upgrade_required",
         "message": "...",
         "tier_required": "basic",
         "current_tier": "free"}

    Visit https://ligandai.com/pricing to upgrade.

    Attributes
    ----------
    current_tier : str | None
        The tier of the API key in use.
    required_tier : str | None
        The minimum tier that grants access to the requested paid surface.
    upgrade_url : str
        URL the user should visit to upgrade their plan.
    """

    def __init__(
        self,
        message: str = (
            "This SDK requires a paid subscription. "
            "Visit https://ligandai.com/pricing to upgrade."
        ),
        *,
        current_tier: str | None = None,
        required_tier: str | None = "basic",
        upgrade_url: str = "https://ligandai.com/pricing",
        **kwargs: Any,
    ) -> None:
        super().__init__(message, **kwargs)
        self.current_tier = current_tier
        self.required_tier = required_tier
        self.upgrade_url = upgrade_url


class LigandAIUpgradeRequired(LigandAIPaidTierRequired):
    """402 — alias for :class:`LigandAIPaidTierRequired` exposed in v0.5.0.

    Catches the same server contract (``error: "upgrade_required"``) but is
    named to match the documented public-API behavior. New code should catch
    :class:`LigandAIUpgradeRequired`; legacy code catching
    :class:`LigandAIPaidTierRequired` continues to work because this class
    inherits from it.
    """


class LigandAIForbidden(LigandAIError):
    """403 — request was authenticated but the resource is not accessible.

    Distinct from :class:`LigandAITierError` (which means "your subscription tier
    is below the required level"). This error is raised when access is restricted
    for non-tier reasons such as pilot allowlists, ownership checks, or
    feature flags. The server's ``error_code`` (when present) is exposed as
    :attr:`reason`.

    Attributes
    ----------
    reason : str | None
        Server-provided ``error_code`` (e.g. ``"pilot_restricted"``), if any.
    """

    def __init__(
        self,
        message: str,
        *,
        reason: str | None = None,
        **kwargs: Any,
    ) -> None:
        super().__init__(message, **kwargs)
        self.reason = reason


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
        # Two flavors of 402:
        #   - upgrade_required (paid-tier gate on /api/v1/*) → LigandAIPaidTierRequired
        #   - insufficient credits (per-operation credit check) → LigandAICreditError
        # Discriminate by the `error` field so the SDK raises the most informative
        # subclass; both still satisfy `isinstance(e, LigandAIError)`.
        err_kind = (payload.get("error") or "").lower()
        if err_kind == "upgrade_required" or payload.get("tier_required"):
            return LigandAIUpgradeRequired(
                message,
                current_tier=payload.get("currentTier")
                or payload.get("current_tier"),
                required_tier=payload.get("requiredTier")
                or payload.get("required_tier")
                or payload.get("tier_required")
                or "pro",
                upgrade_url=payload.get("upgradeUrl")
                or payload.get("upgrade_url")
                or "https://ligandai.com/pricing",
                **common,
            )
        return LigandAICreditError(
            message,
            required=payload.get("required"),
            available=payload.get("available"),
            **common,
        )
    if status_code == 403:
        # Only synthesize a tier error if the server actually says it's a tier
        # problem. Otherwise (pilot allowlists, ownership checks, legal-acceptance
        # gates) raise an honest LigandAIForbidden carrying the server's message.
        err_kind_403 = (payload.get("error") or "").lower()
        is_tier_error = bool(
            payload.get("requiredTier")
            or payload.get("required_tier")
            or payload.get("tier_required")
            or payload.get("currentTier")
            or payload.get("current_tier")
            or payload.get("upgrade_required")
            or err_kind_403 in {"tier_required", "upgrade_required"}
            or (payload.get("code") in {"BASIC_TIER_REQUIRED", "PRO_TIER_REQUIRED", "ENTERPRISE_TIER_REQUIRED"})
        )
        if is_tier_error:
            return LigandAITierError(
                message,
                current_tier=payload.get("currentTier") or payload.get("current_tier"),
                required_tier=payload.get("requiredTier")
                or payload.get("required_tier")
                or payload.get("tier_required"),
                **common,
            )
        return LigandAIForbidden(
            message,
            reason=(
                payload.get("error_code")
                or payload.get("code")
                # Fall back to the `error` field (e.g. "payment_method_required",
                # "pilot_restricted") when no explicit code is provided. These are
                # short snake_case kinds, not user-facing prose.
                or payload.get("error")
            ),
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
