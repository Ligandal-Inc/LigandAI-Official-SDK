# Copyright © 2026 Ligandal, Inc. All rights reserved.
"""Server-advertised SDK compatibility — "is this client too old to be trusted?"
[bd-LIGANDAI_ALPHA_V2-x4vt8.3.2]

WHY THIS EXISTS
    ``ligandai.version_check`` asks **PyPI** whether a newer release exists.
    That answers "is there something newer?" — it cannot answer the question
    that actually bit a customer: *does the server consider this client too old
    to send parameters correctly?* An SDK build that predates a wire change is
    exactly the kind of client that silently drops a request parameter, which
    is how an exclusion becomes a no-op nobody is told about.

    So the PLATFORM answers it. Every response carries three advisory headers:

    ==============================  ===========================================
    ``X-LigandAI-SDK-Status``       ``current`` | ``outdated`` | ``unsupported``
                                    | ``unknown``
    ``X-LigandAI-SDK-Latest``       newest published SDK version
    ``X-LigandAI-SDK-Min-Supported``oldest version the server still vouches for
    ==============================  ===========================================

    They are read off responses the SDK was already making, so this costs **no
    extra round-trip**. When the headers are absent (older server), everything
    here is a no-op. :data:`COMPATIBILITY_PATH` is available for an explicit
    check when a caller wants one on demand.

RULES
    - Advisory only. This module NEVER upgrades anything, never mutates the
      environment, and never fails a request. The user decides.
    - At most ONE advisory per process, so a polling loop cannot spam.
    - Opt out with ``LIGANDAI_SKIP_VERSION_CHECK=1`` (shared with
      :mod:`ligandai.version_check`) or ``LIGANDAI_SKIP_SERVER_VERSION_CHECK=1``.
    - The three ``sdk_*`` codes deliberately carry NO doc anchor: they apply to
      both SDKs, so a Python-only docs link would be wrong. The actionable part
      lives in ``details["upgrade_command"]``.
"""

from __future__ import annotations

import logging
import os
import warnings
from collections.abc import Mapping
from typing import Any

from ligandai._version import __version__
from ligandai.diagnostics import Diagnostic, make_diagnostic, severity_rank
from ligandai.errors import LigandAIVersionWarning

logger = logging.getLogger("ligandai")

__all__ = [
    "COMPATIBILITY_PATH",
    "PACKAGE_NAME",
    "SDK_LATEST_HEADER",
    "SDK_MIN_SUPPORTED_HEADER",
    "SDK_STATUS_HEADER",
    "SKIP_ENV_VARS",
    "UPGRADE_COMMAND",
    "advisory_from_headers",
    "compatibility_diagnostics",
    "latest_advisory",
    "note_response_headers",
    "reset_advisory_state",
    "skip_check_enabled",
]

#: Advisory response headers. Compared case-insensitively — ``httpx.Headers`` is
#: already case-insensitive, and a plain dict is normalized before lookup.
SDK_STATUS_HEADER = "x-ligandai-sdk-status"
SDK_LATEST_HEADER = "x-ligandai-sdk-latest"
SDK_MIN_SUPPORTED_HEADER = "x-ligandai-sdk-min-supported"

#: Explicit, on-demand compatibility endpoint. Optional — the headers above
#: already answer the question on every response.
COMPATIBILITY_PATH = "/api/sdk/compatibility"

PACKAGE_NAME = "ligandai"
UPGRADE_COMMAND = "python -m pip install --upgrade ligandai"

#: Either variable disables the check. The first is shared with
#: :mod:`ligandai.version_check` so one opt-out silences both.
SKIP_ENV_VARS = ("LIGANDAI_SKIP_VERSION_CHECK", "LIGANDAI_SKIP_SERVER_VERSION_CHECK")

_TRUTHY = {"1", "true", "yes", "on"}

_STATUS_CURRENT = "current"
_STATUS_OUTDATED = "outdated"
_STATUS_UNSUPPORTED = "unsupported"
_STATUS_UNKNOWN = "unknown"

# Process-wide latch. A status poll every 3 seconds must not produce a warning
# every 3 seconds.
_advisory_emitted = False
_latest_advisory: Diagnostic | None = None


def skip_check_enabled() -> bool:
    """Whether the user opted out of server compatibility advisories."""
    return any(os.environ.get(var, "").strip().lower() in _TRUTHY for var in SKIP_ENV_VARS)


def reset_advisory_state() -> None:
    """Clear the once-per-process latch. For tests and long-lived REPLs."""
    global _advisory_emitted, _latest_advisory
    _advisory_emitted = False
    _latest_advisory = None


def latest_advisory() -> Diagnostic | None:
    """The advisory this process has already emitted, if any."""
    return _latest_advisory


def _header(headers: Mapping[str, str] | None, name: str) -> str | None:
    if not headers:
        return None
    try:
        value = headers.get(name)
        if value is None:
            # Plain dicts are case-sensitive; httpx.Headers is not.
            lowered = {str(k).lower(): v for k, v in headers.items()}
            value = lowered.get(name)
    except Exception:  # pragma: no cover - defensive
        return None
    if value is None:
        return None
    text = str(value).strip()
    return text or None


def advisory_from_headers(
    headers: Mapping[str, str] | None,
    *,
    installed: str | None = None,
) -> Diagnostic | None:
    """Build the diagnostic implied by the advisory headers, or ``None``.

    ``None`` means "nothing to say": the headers are absent (server predates the
    contract) or the server reports this client as ``current``. Pure function —
    no warning, no latch, no I/O. :func:`note_response_headers` is the one with
    side effects.
    """
    status = _header(headers, SDK_STATUS_HEADER)
    if not status:
        return None
    status_norm = status.strip().lower()
    if status_norm == _STATUS_CURRENT:
        return None

    latest = _header(headers, SDK_LATEST_HEADER)
    min_supported = _header(headers, SDK_MIN_SUPPORTED_HEADER)
    installed_version = installed or __version__

    details: dict[str, Any] = {
        "installed": installed_version,
        "latest": latest,
        "min_supported": min_supported,
        "package": PACKAGE_NAME,
        "upgrade_command": UPGRADE_COMMAND,
        "server_status": status,
    }

    if status_norm == _STATUS_UNSUPPORTED:
        return make_diagnostic(
            "sdk_unsupported",
            f"The LigandAI platform reports that {PACKAGE_NAME} {installed_version} is BELOW "
            f"the minimum supported version"
            f"{f' ({min_supported})' if min_supported else ''}"
            f"{f'; the current release is {latest}' if latest else ''}. "
            f"Requests from a client this old can silently mis-send or drop parameters, so "
            f"what your job actually ran may differ from what you asked for. "
            f"Upgrade with: {UPGRADE_COMMAND}",
            details,
        )

    if status_norm == _STATUS_OUTDATED:
        return make_diagnostic(
            "sdk_outdated",
            f"The LigandAI platform reports that {PACKAGE_NAME} {installed_version} is out of "
            f"date"
            f"{f' (current release: {latest})' if latest else ''}"
            f"{f'; minimum supported is {min_supported}' if min_supported else ''}. "
            f"Upgrade to pick up current parameter handling, diagnostics, and bug fixes: "
            f"{UPGRADE_COMMAND}",
            details,
        )

    if status_norm == _STATUS_UNKNOWN:
        return make_diagnostic(
            "sdk_version_unknown",
            "The LigandAI platform could not determine which SDK version sent this request, "
            "so it could not be checked for compatibility. This usually means the User-Agent "
            "was overridden. Use an official LigandAI SDK, or send a User-Agent of the form "
            f"ligandai-python/<version>. (This client reports {installed_version}.)",
            details,
        )

    # An unrecognized status is itself evidence the server is speaking a newer
    # protocol than this build knows — which is exactly the "upgrade" case. The
    # raw value is preserved in details rather than dropped.
    return make_diagnostic(
        "sdk_outdated",
        f"The LigandAI platform reported an SDK compatibility status this client does not "
        f"recognize ({status!r}), which means the server is newer than {PACKAGE_NAME} "
        f"{installed_version}. Upgrade with: {UPGRADE_COMMAND}",
        details,
    )


def note_response_headers(
    headers: Mapping[str, str] | None,
    *,
    installed: str | None = None,
) -> Diagnostic | None:
    """Consume advisory headers off a response. **Never raises.**

    Called from the SDK's single response funnel
    (:func:`ligandai._http._raise_for_status`), which every request — 2xx, 4xx,
    5xx, and SSE — passes through before returning. Emits a
    :class:`~ligandai.errors.LigandAIVersionWarning` for severity
    ``warning`` and above, at most once per process, and returns the diagnostic
    so a caller can inspect it as data.

    Returns ``None`` when there is nothing to say, when the user opted out, or
    when an advisory has already been emitted this process.
    """
    global _advisory_emitted, _latest_advisory
    if _advisory_emitted or skip_check_enabled():
        return None
    try:
        diag = advisory_from_headers(headers, installed=installed)
    except Exception as exc:  # pragma: no cover - defensive; never fail a request
        logger.debug("SDK compatibility advisory skipped: %r", exc)
        return None
    if diag is None:
        return None

    _advisory_emitted = True
    _latest_advisory = diag
    if severity_rank(diag.severity) >= severity_rank("warning"):
        warnings.warn(diag.message, LigandAIVersionWarning, stacklevel=3)
    else:
        logger.info("[ligandai] %s", diag.message)
    return diag


def compatibility_diagnostics(
    payload: Any,
    *,
    installed: str | None = None,
) -> list[Diagnostic]:
    """Turn a ``GET /api/sdk/compatibility`` JSON body into diagnostics.

    Accepts either the header-shaped body
    (``{"status": ..., "latest": ..., "min_supported": ...}``) or a body that
    already carries a canonical ``diagnostics`` array — whichever the server
    settles on, the caller gets :class:`~ligandai.diagnostics.Diagnostic`
    objects. Returns ``[]`` for a ``current`` client.
    """
    from ligandai.diagnostics import diagnostics_from_container

    if not isinstance(payload, dict):
        return []
    found = diagnostics_from_container(payload)
    if found:
        return found

    def _first(*keys: str) -> str:
        """First non-empty value among ``keys``. Both snake_case and camelCase
        are accepted: the endpoint is being built in parallel and a compatibility
        check that silently no-ops on a casing mismatch is worse than useless."""
        for key in keys:
            value = payload.get(key)
            if value not in (None, ""):
                return str(value)
        return ""

    pseudo_headers = {
        SDK_STATUS_HEADER: _first("status", "sdk_status", "sdkStatus"),
        SDK_LATEST_HEADER: _first("latest", "latest_version", "latestVersion", "sdkLatest"),
        SDK_MIN_SUPPORTED_HEADER: _first(
            "min_supported", "minSupported", "min_supported_version", "minSupportedVersion"
        ),
    }
    diag = advisory_from_headers(
        {k: str(v) for k, v in pseudo_headers.items() if v}, installed=installed
    )
    return [diag] if diag is not None else []
