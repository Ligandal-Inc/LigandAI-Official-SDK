# Copyright © 2026 Ligandal, Inc. All rights reserved.
"""Headless / CI / non-interactive environment detection.

Guards any SDK code path that would otherwise pop a browser window and/or
block waiting on a human to click through it — specifically the legal-
acceptance interactive resolution flow
(:meth:`ligandai.resources.legal.Legal.resolve_interactively`), described in
``docs/clickwrap_legal_acceptance.md`` §3.6:

    "Headless/CI degrades to a clear error, never a hang. If no display is
    available, the process is non-interactive, or CI is set, raise
    immediately with the URL in the message. Every wait is bounded by a
    hard timeout."

No headless/CI/TTY detection existed anywhere in this SDK before. This
module is deliberately small and dependency-free so it can be imported from
both the sync and async resource modules (and, if needed, from ``_http.py``)
without risk of a circular import.
"""

from __future__ import annotations

import os
import sys

# Mirrors the truthy-value idiom already used by `_http._debug_enabled()`.
_TRUTHY = {"1", "true", "yes", "on"}


def _env_flag(name: str) -> bool:
    return os.environ.get(name, "").strip().lower() in _TRUTHY


def is_noninteractive_environment() -> bool:
    """True when this process cannot reasonably hand a human a browser click.

    Any of the following makes the process non-interactive:

    - ``CI`` or ``GITHUB_ACTIONS`` is set to a truthy value.
    - stdin is not a TTY (piped input, redirected from ``/dev/null``, a
      subprocess with no controlling terminal, a notebook kernel, etc.).
    - On Linux, neither ``DISPLAY`` nor ``WAYLAND_DISPLAY`` is set — no X11
      or Wayland session exists for ``webbrowser.open`` to target.

    macOS and Windows don't gate on ``DISPLAY``/``WAYLAND_DISPLAY`` — those
    platforms don't use that mechanism, and normally have a compositor
    running whenever a human is actually at the machine, so the TTY/CI
    checks alone are the signal there.
    """
    if _env_flag("CI") or _env_flag("GITHUB_ACTIONS"):
        return True
    try:
        if not sys.stdin.isatty():
            return True
    except (AttributeError, ValueError):
        # stdin closed/detached (e.g. some daemon or subprocess contexts).
        # Treat as non-interactive rather than risk blocking forever.
        return True
    if sys.platform.startswith("linux"):
        if not os.environ.get("DISPLAY") and not os.environ.get("WAYLAND_DISPLAY"):
            return True
    return False
