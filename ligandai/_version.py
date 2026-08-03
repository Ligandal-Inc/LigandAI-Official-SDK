# Copyright © 2026 Ligandal, Inc. All rights reserved.
"""Single source of truth for the SDK version.

Derived from the INSTALLED distribution metadata rather than a hand-maintained
literal. The literal used to be kept "in sync with pyproject.toml" by hand, and
it drifted: 0.8.1 shipped reporting ``__version__ == "0.8.0"``, which also went
out in the User-Agent on every request (see ``_http._build_user_agent``), so
server-side client-version telemetry was wrong for that release.

``importlib.metadata.version`` reads the real installed version, so a release can
no longer disagree with what it reports. The literal below is only a fallback for
running straight from a source checkout that was never installed, where no
distribution metadata exists.
"""

from __future__ import annotations

from importlib.metadata import PackageNotFoundError, version as _dist_version

#: Fallback for an uninstalled source tree. An installed package always wins —
#: this value is not what users receive.
_FALLBACK_VERSION = "0.9.0"

try:
    __version__: str = _dist_version("ligandai")
except PackageNotFoundError:  # bare source checkout
    __version__ = _FALLBACK_VERSION
