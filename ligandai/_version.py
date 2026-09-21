# Copyright © 2026 Ligandal, Inc. All rights reserved.
"""Single source of truth for the SDK version.

Derived from the INSTALLED distribution metadata rather than a hand-maintained
literal. The literal used to be kept "in sync with pyproject.toml" by hand, and
it drifted: 0.8.1 shipped reporting ``__version__ == "0.8.0"``, which also went
out in the User-Agent on every request (see ``_http._build_user_agent``), so
server-side client-version telemetry was wrong for that release.

⛔ BUT ``importlib.metadata.version("ligandai")`` DOES NOT ANSWER "what version
is this code?" — it answers "what version does the installed *distribution*
claim?", and those are different questions whenever the module on ``sys.path``
is not the one that distribution installed. That is the normal state of a
source checkout on a machine that also has a release installed: observed
2026-09-21 on this box, where the repo at 0.13.0 imported cleanly while
``importlib.metadata`` reported **0.7.5** from an unrelated site-packages
install. Every request went out stamped ``ligandai-python/0.7.5``, and
``ligandai --version`` would have confidently printed a version five releases
stale — a plausible, precise, WRONG answer, which is worse than no answer.

So the distribution's version is trusted ONLY when that distribution actually
contains the module we imported. When it does not, the source literal wins and
:data:`__version_provenance__` records the disagreement so a caller (and
``ligandai --version``) can surface it instead of quietly picking one.
[bd-dre-rp11j.3]
"""

from __future__ import annotations

from importlib.metadata import PackageNotFoundError, distribution
from pathlib import Path

#: Fallback for an uninstalled source tree, and the authority whenever the
#: installed distribution metadata describes a DIFFERENT installation than the
#: code being executed. Kept equal to ``pyproject.toml``'s ``version``.
_FALLBACK_VERSION = "0.13.0"


def _resolve_version() -> tuple[str, str]:
    """Return ``(version, provenance)``.

    ``provenance`` is empty when the answer is trustworthy, and otherwise
    describes exactly why the two sources disagree — so "I could not
    determine the version" is never spelled the same way as a real version.
    """
    try:
        dist = distribution("ligandai")
    except PackageNotFoundError:
        # A bare source checkout: no metadata exists, the literal is correct.
        return _FALLBACK_VERSION, ""
    except Exception:
        return _FALLBACK_VERSION, (
            "installed distribution metadata for 'ligandai' could not be read; "
            "reporting the source-tree version instead"
        )

    here = Path(__file__).resolve()
    try:
        owned = Path(str(dist.locate_file("ligandai/_version.py"))).resolve()
    except Exception:
        owned = None

    if owned is not None and owned == here:
        return dist.version, ""

    claimed = getattr(dist, "version", "<unknown>")
    if claimed == _FALLBACK_VERSION:
        # Same version, different location — nothing misleading to report.
        return claimed, ""
    return _FALLBACK_VERSION, (
        f"the installed 'ligandai' distribution reports {claimed} but does not "
        f"contain the module actually imported from {here.parent}; reporting the "
        f"source-tree version {_FALLBACK_VERSION}. Requests would otherwise have "
        f"been stamped with {claimed}."
    )


__version__, __version_provenance__ = _resolve_version()
