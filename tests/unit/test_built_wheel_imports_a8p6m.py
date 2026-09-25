"""The built wheel must import. [bd-dre-a8p6m]

``ligandai 0.15.0`` was published unimportable and the 1528-test suite passed on
it, because the suite imports the SOURCE TREE. A concurrent session's uncommitted
``contrastive.py`` was sitting next to the committed file that imported from it,
so the tree had a symbol the wheel did not. "The suite passed" was therefore
compatible with "the artifact does not import", and the only step that imported
the built package ran AFTER upload -- by which point the version is burned.

This test closes that specific gap: it exercises ``dist/``, not the repo.

It SKIPS unless the newest wheel in ``dist/`` carries the version currently in
``ligandai/_version.py``. During a release you build first, so they match and
this runs. During ordinary development a stale wheel must not make the suite
slow or fail for reasons unrelated to the change in hand.
"""
from __future__ import annotations

import pathlib
import re
import subprocess

import pytest

REPO = pathlib.Path(__file__).resolve().parents[2]
DIST = REPO / "dist"
PREFLIGHT = REPO / "scripts" / "preflight_wheel_import.sh"


def _source_version() -> str | None:
    """The version this tree would release, from pyproject.toml.

    NOT from ligandai/_version.py: that module derives __version__ from the
    INSTALLED distribution metadata rather than a literal, so in a source tree it
    reports whatever happens to be installed -- which is exactly the kind of
    "reads the tree, not the artifact" confusion this test exists to eliminate.
    RELEASING.md step 4 bumps pyproject.toml and _version.py together, and
    pyproject is the one with a literal to read.
    """
    try:
        text = (REPO / "pyproject.toml").read_text()
    except OSError:
        return None
    m = re.search(r'(?m)^version\s*=\s*["\']([^"\']+)["\']', text)
    return m.group(1) if m else None


def _newest_wheel() -> pathlib.Path | None:
    if not DIST.is_dir():
        return None
    wheels = sorted(DIST.glob("ligandai-*.whl"), key=lambda p: p.stat().st_mtime, reverse=True)
    return wheels[0] if wheels else None


def test_built_wheel_installs_and_imports() -> None:
    if not PREFLIGHT.is_file():
        pytest.fail(f"{PREFLIGHT} is missing — the release preflight must exist")

    wheel = _newest_wheel()
    if wheel is None:
        pytest.skip("no wheel in dist/ — nothing built to check")

    src = _source_version()
    m = re.match(r"ligandai-([^-]+)-", wheel.name)
    built = m.group(1) if m else None
    if src is None or built is None or built != src:
        pytest.skip(
            f"newest wheel is {built!r} but the source tree is {src!r} — stale build, "
            "not the artifact this tree would release"
        )

    proc = subprocess.run(
        [str(PREFLIGHT), str(wheel)],
        capture_output=True, text=True, timeout=900,
    )
    if proc.returncode != 0:
        pytest.fail(
            f"the built wheel {wheel.name} does not import cleanly — DO NOT UPLOAD IT.\n"
            f"--- stdout ---\n{proc.stdout}\n--- stderr ---\n{proc.stderr}"
        )
