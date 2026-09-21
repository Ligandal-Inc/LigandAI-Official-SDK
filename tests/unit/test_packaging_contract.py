# Copyright © 2026 Ligandal, Inc. All rights reserved.
"""Packaging + distribution contract for `pip install ligandai`. [bd-dre-rp11j.1]

Every assertion here corresponds to a defect that was observed live on
2026-09-20 against a clean venv install of PyPI `ligandai` 0.12.2. None of them
are hypothetical, and none of them are caught by the existing suite: unit tests
import from the source tree, so they cannot see what the BUILT ARTIFACT does to
a customer's environment.

What was red before this file existed:

  * ``test_readme_banner_version_matches_pyproject`` — README banner said
    **v0.11.0**, ``pyproject.toml`` said **0.13.0**, PyPI served **0.12.2**. The
    README carries its own written ANTI-DRIFT CONTRACT naming an enforcement
    script — ``scripts/check-docs-drift.mjs`` — that lives in a *different*
    repository (LIGANDAI_ALPHA_V2) and therefore never runs in this one. A guard
    in another repo is a guard nobody here has seen fire.

  * ``test_wheel_installs_nothing_outside_the_package`` — installing 0.12.2
    wrote ``AGENTS.md``, ``CLAUDE.md``, ``skills/`` and ``.claude/`` into the
    ROOT of site-packages, and ``importlib.util.find_spec("skills")`` then
    resolved to our docs directory as a namespace package.

  * ``test_no_published_release_declares_a_lower_python_floor`` — PyPI 0.6.9
    (2026-06-11) declares ``Requires-Python: >=3.9`` while every other release
    declares ``>=3.10``. Consequence, measured: ``pip install ligandai`` on
    Python 3.9 does NOT refuse. It backtracks 24 releases and silently installs
    0.6.9 — a different SDK with a different dependency set — and exits 0. The
    customer is three months behind and is never told why. 0.6.9 is also one of
    six published versions with no corresponding commit in this repo, so no code
    review here could ever have caught it.
"""

from __future__ import annotations

import json
import os
import pathlib
import re
import subprocess
import sys
import zipfile

import pytest

REPO_ROOT = pathlib.Path(__file__).resolve().parents[2]
PYPROJECT = REPO_ROOT / "pyproject.toml"
README = REPO_ROOT / "README.md"
PKG_DIR = REPO_ROOT / "ligandai"


def _pyproject_text() -> str:
    return PYPROJECT.read_text(encoding="utf-8")


def _declared_version() -> str:
    m = re.search(r'^version\s*=\s*"([^"]+)"', _pyproject_text(), re.MULTILINE)
    assert m, "pyproject.toml has no [project] version"
    return m.group(1)


def _declared_python_floor() -> str:
    m = re.search(r'^requires-python\s*=\s*"([^"]+)"', _pyproject_text(), re.MULTILINE)
    assert m, "pyproject.toml has no requires-python"
    return m.group(1)


def _version_tuple(v: str) -> tuple[int, ...]:
    return tuple(int(p) for p in re.findall(r"\d+", v))


# --------------------------------------------------------------------------
# 1. Docs / metadata parity — enforced HERE, in the repo that owns the file.
# --------------------------------------------------------------------------

def test_readme_banner_version_matches_pyproject() -> None:
    """The README is the PyPI long description. Its banner is what the world reads.

    The README's own contract (rule 2 of the ANTI-DRIFT block at the top of the
    file) says the banner version MUST equal ``[project].version``. This test is
    that rule, executable, in this repo.
    """
    banners = re.findall(r"^>\s+\*\*v([0-9][^*]*)\*\*", README.read_text(encoding="utf-8"), re.MULTILINE)
    assert banners, "README has no `> **vX.Y.Z**` version banner"
    assert len(banners) == 1, (
        f"README must carry exactly ONE version banner (rule 1 of its own anti-drift "
        f"contract); found {len(banners)}: {banners}. Replace the banner, never stack a "
        f"new one above the old."
    )
    assert banners[0].strip() == _declared_version(), (
        f"README banner says v{banners[0].strip()} but pyproject.toml says "
        f"{_declared_version()}. The banner is the PyPI long description and only "
        f"changes when a release is published — a stale banner advertises the wrong "
        f"SDK to every visitor of https://pypi.org/project/ligandai/."
    )


def test_version_module_matches_pyproject() -> None:
    """``ligandai._version`` must not drift from the packaging metadata.

    Green when written (2026-09-20) — this is a hygiene guard, not a fix. The
    drift it watches for is real history: 0.8.1 shipped reporting 0.8.0, and
    that wrong version went out in the User-Agent on every request.

    Read from the file rather than by import: an installed copy of a DIFFERENT
    version shadows the tree under test and would make this pass or fail for
    reasons that have nothing to do with the tree.
    """
    src = (PKG_DIR / "_version.py").read_text(encoding="utf-8")
    m = re.search(r'_FALLBACK_VERSION\s*=\s*"([^"]+)"', src)
    assert m, "ligandai/_version.py declares no _FALLBACK_VERSION"
    assert m.group(1) == _declared_version(), (
        f"ligandai/_version.py _FALLBACK_VERSION is {m.group(1)!r} but "
        f"pyproject.toml declares {_declared_version()!r}. An installed package "
        f"reads its real version from distribution metadata, so this literal only "
        f"shows up for someone running from a bare source checkout — exactly the "
        f"person (a contributor, or an agent working in the repo) whose bug report "
        f"would then quote a version that was never built."
    )


# --------------------------------------------------------------------------
# 2. The built wheel — what `pip install` actually does to an environment.
# --------------------------------------------------------------------------

@pytest.fixture(scope="session")
def built_wheel(tmp_path_factory: pytest.TempPathFactory) -> pathlib.Path:
    """Build this tree into a wheel. Skips loudly if the build backend is absent.

    Uses ``--no-isolation`` so the test never needs the network. Install the dev
    extra (which pins ``build`` and ``hatchling``) to run it.
    """
    try:
        import build  # noqa: F401
        import hatchling  # noqa: F401
    except ImportError as exc:  # pragma: no cover - environment-dependent
        pytest.skip(
            f"cannot build a wheel here: {exc}. This test needs the build backend "
            f"available locally — `pip install -e '.[dev]'` provides it. It is "
            f"SKIPPED, not passed: nothing about the wheel has been checked."
        )

    outdir = tmp_path_factory.mktemp("wheel")
    proc = subprocess.run(
        [sys.executable, "-m", "build", "--wheel", "--no-isolation", "--outdir", str(outdir)],
        cwd=str(REPO_ROOT),
        capture_output=True,
        text=True,
    )
    if proc.returncode != 0:
        pytest.fail(
            "wheel build failed — the package cannot be distributed at all.\n"
            f"stdout:\n{proc.stdout[-4000:]}\nstderr:\n{proc.stderr[-4000:]}"
        )
    wheels = sorted(outdir.glob("*.whl"))
    assert len(wheels) == 1, f"expected exactly one wheel, got {[w.name for w in wheels]}"
    return wheels[0]


def test_wheel_installs_nothing_outside_the_package(built_wheel: pathlib.Path) -> None:
    """An SDK owns its own namespace in site-packages and nothing else.

    Observed on PyPI 0.12.2: the wheel's top-level entries were
    ``['.claude', 'AGENTS.md', 'CLAUDE.md', 'ligandai', '<dist-info>', 'skills']``.
    ``skills`` then resolved as an importable namespace package, so `import
    skills` in ANY downstream project bound to LigandAI's docs directory.
    ``AGENTS.md`` / ``CLAUDE.md`` are generic names: a second package shipping
    either one overwrites ours, and `pip uninstall ligandai` then deletes theirs.
    """
    with zipfile.ZipFile(built_wheel) as z:
        names = z.namelist()

    offenders = sorted(
        {
            n.split("/")[0]
            for n in names
            if not n.startswith("ligandai/")
            and not re.match(r"^ligandai-[^/]+\.dist-info/", n)
            and not re.match(r"^ligandai-[^/]+\.data/", n)
        }
    )
    assert not offenders, (
        f"wheel writes these into the ROOT of site-packages: {offenders}. "
        f"Everything must live under `ligandai/`, the `.dist-info/` or the "
        f"`.data/` directory. Force-include destinations are the usual cause — "
        f"point them at `ligandai/...`."
    )


def test_wheel_ships_no_bytecode(built_wheel: pathlib.Path) -> None:
    """`force-include` copies a directory verbatim; source exclusions do not apply.

    A builder who has imported anything under `skills/` would otherwise ship
    their own `__pycache__` — bytecode pinned to one Python minor — inside a
    `py3-none-any` wheel.
    """
    with zipfile.ZipFile(built_wheel) as z:
        bad = [n for n in z.namelist() if "__pycache__" in n or n.endswith((".pyc", ".pyo"))]
    assert not bad, f"wheel carries build-machine bytecode: {bad}"


def test_every_repo_module_ships_in_the_wheel(built_wheel: pathlib.Path) -> None:
    """A module present in git but absent from the wheel is a silent capability loss.

    This is the packaging half of ``scripts/check_published_superset.py``: that
    script compares this tree against PyPI, this test compares this tree against
    the artifact we are about to publish.
    """
    with zipfile.ZipFile(built_wheel) as z:
        shipped = {n for n in z.namelist() if n.startswith("ligandai/") and n.endswith(".py")}

    on_disk = {
        str(p.relative_to(REPO_ROOT)).replace(os.sep, "/")
        for p in PKG_DIR.rglob("*.py")
        if "__pycache__" not in p.parts
    }
    missing = sorted(on_disk - shipped)
    assert not missing, (
        f"{len(missing)} module(s) exist in the repo but are NOT in the wheel — a "
        f"customer installing this release cannot reach them: {missing}"
    )


def test_console_script_entry_point_resolves(built_wheel: pathlib.Path) -> None:
    """`ligandai` on PATH must point at something that actually imports."""
    with zipfile.ZipFile(built_wheel) as z:
        ep_name = next(n for n in z.namelist() if n.endswith(".dist-info/entry_points.txt"))
        ep = z.read(ep_name).decode("utf-8")

    m = re.search(r"^ligandai\s*=\s*([\w.]+):(\w+)\s*$", ep, re.MULTILINE)
    assert m, f"wheel declares no `ligandai` console script. entry_points.txt:\n{ep}"

    module, func = m.groups()
    import importlib

    mod = importlib.import_module(module)
    assert callable(getattr(mod, func, None)), (
        f"console script points at {module}:{func}, which is not callable. "
        f"`ligandai` would be on PATH and crash on first use."
    )


# --------------------------------------------------------------------------
# 3. What the INDEX will hand a customer — requires the network.
# --------------------------------------------------------------------------

@pytest.mark.integration
def test_no_published_release_declares_a_lower_python_floor() -> None:
    """A single release with a looser floor silently downgrades every new user.

    Measured 2026-09-20 with `pip install --dry-run --python-version 3.9`:
    because 0.6.9 alone declares ``>=3.9``, pip does not refuse on an
    unsupported interpreter. It resolves backwards to 0.6.9 — a June release
    with an entirely different dependency tree (requests/typer/rich/aiohttp) —
    prints no warning, and exits 0. "Unsupported Python" becomes "silently 24
    releases behind", which is precisely the failure mode this bead exists to
    eliminate.

    Remediation is publish-side and cannot be done from this repo: YANK the
    offending release on PyPI (`Requires-Python` on an existing file cannot be
    edited). A yanked release is still installable by exact pin, so nothing
    breaks for anyone who deliberately depends on it.
    """
    import urllib.request

    try:
        with urllib.request.urlopen("https://pypi.org/pypi/ligandai/json", timeout=30) as fh:
            data = json.load(fh)
    except Exception as exc:  # pragma: no cover - network-dependent
        pytest.skip(f"PyPI unreachable: {exc!r}. Nothing was checked.")

    floor = _declared_python_floor()
    want = _version_tuple(floor)

    offenders = []
    for version, files in data["releases"].items():
        for f in files:
            if f.get("yanked"):
                continue
            rp = f.get("requires_python")
            if rp is None:
                offenders.append((version, f["filename"], "(no Requires-Python at all)"))
                break
            if _version_tuple(rp) < want:
                offenders.append((version, f["filename"], rp))
                break

    assert not offenders, (
        f"pyproject declares requires-python {floor!r}, but these PUBLISHED, "
        f"UN-YANKED releases declare a looser floor:\n"
        + "\n".join(f"    {v:<10} {rp:<12} {fn}" for v, fn, rp in offenders)
        + "\n\npip will happily resolve BACKWARDS to one of these on an unsupported "
        "interpreter and install it without a word. Yank them on PyPI."
    )


@pytest.mark.integration
def test_published_latest_is_not_ahead_of_this_tree() -> None:
    """Guards the direction that means someone published from a tree not in git.

    On 2026-07-30 PyPI served 0.7.6 while this repo's last release commit was
    0.7.4; publishing from that branch would have deleted
    ``ligandai/resources/legal.py``. Six published versions (0.3.5, 0.3.7,
    0.4.0, 0.4.1, 0.6.9, 0.7.5) still have no corresponding commit here.
    """
    import urllib.request

    try:
        with urllib.request.urlopen("https://pypi.org/pypi/ligandai/json", timeout=30) as fh:
            latest = json.load(fh)["info"]["version"]
    except Exception as exc:  # pragma: no cover - network-dependent
        pytest.skip(f"PyPI unreachable: {exc!r}. Nothing was checked.")

    assert _version_tuple(latest) <= _version_tuple(_declared_version()), (
        f"PyPI serves {latest} but this tree is {_declared_version()}. Someone "
        f"published from a tree that is not this one. Do NOT cut a release from "
        f"here until the diff is reconciled — run "
        f"`python3 scripts/check_published_superset.py` and read its output."
    )
