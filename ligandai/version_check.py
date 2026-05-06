# Copyright © 2026 Ligandal, Inc. All rights reserved.
"""PyPI version checks for the LIGANDAI Python SDK."""

from __future__ import annotations

import os
import re
import warnings
from collections.abc import Callable
from typing import Any

import httpx

from ligandai._version import __version__

PACKAGE_NAME = "ligandai"
PYPI_JSON_URL = "https://pypi.org/pypi/ligandai/json"
PYPI_RELEASE_URL = "https://pypi.org/pypi/ligandai/{version}/json"
SKIP_ENV_VAR = "LIGANDAI_SKIP_VERSION_CHECK"
EXPECTED_REPOSITORY = "github.com/ligandal/ligandai-python-sdk"

_VERSION_NOTICE_CACHE: str | None = None
_VERSION_NOTICE_CHECKED = False
_VERSION_NOTICE_EMITTED = False


def get_installed_version() -> str:
    """Return the SDK version from the imported package."""
    return __version__


def _version_key(version: str) -> tuple[int, ...]:
    parts: list[int] = []
    for chunk in re.split(r"[.+-]", version):
        match = re.match(r"(\d+)", chunk)
        if not match:
            break
        parts.append(int(match.group(1)))
    return tuple(parts)


def is_outdated(installed: str, latest: str) -> bool:
    """Return True when ``installed`` is older than ``latest``."""
    return _version_key(installed) < _version_key(latest)


def _metadata_matches_sdk(payload: dict[str, Any]) -> bool:
    info = payload.get("info") or {}
    project_urls = info.get("project_urls") or {}
    urls = [
        str(info.get("home_page") or ""),
        *(str(value or "") for value in project_urls.values()),
    ]
    return any(EXPECTED_REPOSITORY in url for url in urls)


def _release_has_active_files(payload: dict[str, Any]) -> bool:
    files = payload.get("urls") or []
    return any(not file.get("yanked") for file in files)


def _sorted_versions(releases: dict[str, Any]) -> list[str]:
    return sorted(releases, key=_version_key, reverse=True)


def get_latest_pypi_version(timeout: float = 2.0) -> str | None:
    """Fetch the latest valid LIGANDAI SDK release from PyPI.

    The check validates that the release metadata points back to the real
    ``ligandal/ligandai-python-sdk`` repository and has at least one active
    file. That prevents accidental uploads from another package root from being
    recommended as an SDK update target.
    """
    if os.environ.get(SKIP_ENV_VAR, "").lower() in {"1", "true", "yes", "on"}:
        return None

    try:
        with httpx.Client(timeout=timeout, follow_redirects=True) as client:
            response = client.get(PYPI_JSON_URL, headers={"Accept": "application/json"})
            response.raise_for_status()
            payload = response.json()

            latest = str((payload.get("info") or {}).get("version") or "")
            if latest and _metadata_matches_sdk(payload) and _release_has_active_files(payload):
                return latest

            releases = payload.get("releases") or {}
            for version in _sorted_versions(releases):
                release_response = client.get(
                    PYPI_RELEASE_URL.format(version=version),
                    headers={"Accept": "application/json"},
                )
                if not release_response.is_success:
                    continue
                release_payload = release_response.json()
                if _metadata_matches_sdk(release_payload) and _release_has_active_files(release_payload):
                    return str(version)
    except Exception:
        return None
    return None


def get_update_notice(
    installed_version: str | None = None,
    latest_version: str | None = None,
) -> str | None:
    """Build an update notice when a newer valid SDK release exists."""
    installed = installed_version or get_installed_version()
    latest = latest_version or get_latest_pypi_version()
    if not installed or not latest or not is_outdated(installed, latest):
        return None
    return (
        f"ligandai {installed} is behind the latest PyPI SDK release {latest}. "
        "Run `python -m pip install --upgrade ligandai` before using the SDK."
    )


def get_cached_update_notice() -> str | None:
    """Fetch and cache the update notice once per process."""
    global _VERSION_NOTICE_CACHE, _VERSION_NOTICE_CHECKED
    if not _VERSION_NOTICE_CHECKED:
        _VERSION_NOTICE_CACHE = get_update_notice()
        _VERSION_NOTICE_CHECKED = True
    return _VERSION_NOTICE_CACHE


def emit_update_notice(printer: Callable[[str], None] | None = None) -> str | None:
    """Emit the update notice at most once per process."""
    global _VERSION_NOTICE_EMITTED
    notice = get_cached_update_notice()
    if not notice or _VERSION_NOTICE_EMITTED:
        return notice

    _VERSION_NOTICE_EMITTED = True
    if printer is None:
        warnings.warn(notice, UserWarning, stacklevel=2)
    else:
        printer(notice)
    return notice

