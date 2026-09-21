# Copyright © 2026 Ligandal, Inc. All rights reserved.
"""The suite must not be able to measure nothing and look fine. [bd-dre-rp11j.28]

THE MEASURED DEFECT, reproduced 2026-09-21 against the tree at 3c7faf4 in a
throwaway venv with the DOCUMENTED dev install:

    $ python -m venv /tmp/v && /tmp/v/bin/pip install -e '.[dev]'
    $ /tmp/v/bin/python -m pytest tests/unit -q
    ERROR tests/unit/test_folds.py
    E   ModuleNotFoundError: No module named 'numpy'
    !!!!!!!!!!!!! Interrupted: 1 error during collection !!!!!!!!!!!!!
    1 error in 2.24s

Zero of 56 test files executed. The only number on screen was `1`. A collection
error aborts the whole run, so that output is indistinguishable from a suite
with one flaky test — and equally indistinguishable from a suite with no tests
at all. The same class without the word "Interrupted", measured in the same
session:

    $ pip install -e . pytest && pytest tests/unit -q
    1 skipped, 33 errors in 2.54s      # pytest_httpx / respx undeclared

Again ZERO tests ran out of 1,207.

The exit code was already non-zero in both cases, so an exit-code check would
not have helped anyone. What was missing is the SENTENCE: "N of M test files
contributed tests". `tests/conftest.py` now prints that census on every run,
and this file is its hard assertion — as ONE named failing test, which is why
it is a test and not another hook. The first version of the guard raised
`pytest.UsageError` during collection, which aborted the run before any test
executed: a guard that becomes the defect it guards against.
"""

from __future__ import annotations

import pathlib
import re
import subprocess
import sys

import pytest

REPO_ROOT = pathlib.Path(__file__).resolve().parents[2]
TESTS_DIR = REPO_ROOT / "tests"

#: A file may contribute zero tests ONLY by skipping at import with a reason
#: that names how to make it run. Anything else is a file nobody measured.
_ACTIONABLE_SKIP = re.compile(r"pip install|export |set [A-Z_]+=", re.IGNORECASE)


def _collect_census() -> tuple[dict[str, int], str]:
    """Run ``--collect-only`` in a subprocess and count items per file.

    A subprocess rather than an in-process pytest call: collecting the suite
    from inside itself re-imports every test module into the running session,
    and a plugin-level import error there is exactly the condition under test.
    """
    proc = subprocess.run(
        # `-rs` is load-bearing: without it `--collect-only -q` prints no skip
        # REASONS, and the classification below cannot tell an actionable
        # module-level skip from a file nobody measured.
        [
            sys.executable, "-m", "pytest", "tests", "-q", "--collect-only",
            "-rs", "-p", "no:cacheprovider",
        ],
        cwd=str(REPO_ROOT),
        capture_output=True,
        text=True,
        timeout=300,
    )
    counts: dict[str, int] = {}
    for line in proc.stdout.splitlines():
        if line.startswith("tests/") and "::" in line:
            counts[line.split("::")[0]] = counts.get(line.split("::")[0], 0) + 1
    return counts, proc.stdout + proc.stderr


def test_every_test_file_on_disk_contributes_at_least_one_test() -> None:
    """A test file that collects nothing measured nothing — say so, loudly.

    Measured 2026-09-21: all 60 ``test_*.py`` files under ``tests/`` contribute
    at least one item under the documented dev install. There is no
    hand-maintained floor to go stale — the expectation is the filesystem.
    """
    counts, raw = _collect_census()
    on_disk = sorted(
        str(p.relative_to(REPO_ROOT)) for p in TESTS_DIR.glob("**/test_*.py")
    )
    assert on_disk, "no test files found — this guard is looking in the wrong place"

    silent = [f for f in on_disk if counts.get(f, 0) == 0]
    if not silent:
        return

    # A file may legitimately contribute nothing by skipping at import, but the
    # skip has to tell the reader how to make it run.
    unexplained = []
    for f in silent:
        reason = ""
        for line in raw.splitlines():
            # pytest's own `-rs` format only. Matching any line containing
            # "skip" once matched this repo's OWN census line, which says
            # "skipped at import" and carries no remedy — so a perfectly
            # actionable skip was reported as unexplained.
            if line.startswith("SKIPPED") and f in line:
                reason = line
                break
        if not reason or not _ACTIONABLE_SKIP.search(reason):
            unexplained.append(f"{f}  ({reason or 'no skip reason reported at all'})")

    assert not unexplained, (
        f"{len(unexplained)} of {len(on_disk)} test file(s) contributed ZERO tests with no "
        f"actionable reason, so this suite measured NOTHING about them:\n"
        + "\n".join(f"    {u}" for u in unexplained)
        + "\n\nA file that fails to import contributes no items and is reported as `N "
        "errors` — a line indistinguishable from N flaky tests while the whole suite in "
        "fact never ran. If the cause is a missing dependency it belongs in the `dev` "
        "extra of pyproject.toml, not in a skip; if it is a deliberate skip, give it a "
        "reason naming the install or env var that turns it on. [bd-dre-rp11j.28]"
    )


def test_the_census_hook_is_installed_and_prints() -> None:
    """The census line is the whole remediation — assert it is actually emitted.

    A guard nobody has seen fire is unverified: this runs the suite's own
    conftest against a single file and greps for the line.
    """
    proc = subprocess.run(
        [
            sys.executable, "-m", "pytest",
            "tests/unit/test_suite_integrity_rp11j28.py::test_the_census_hook_is_installed_and_prints",
            "-q", "-p", "no:cacheprovider", "--co",
        ],
        cwd=str(REPO_ROOT),
        capture_output=True,
        text=True,
        timeout=120,
    )
    assert "test-file census:" in proc.stdout, (
        "tests/conftest.py no longer prints the test-file census. That line is the "
        "entire fix for `1 error in 2.24s` reading like one flaky test rather than "
        "0 of 56 files. stdout:\n" + proc.stdout[-3000:]
    )


def test_pytest_config_keeps_the_strictness_that_makes_silence_impossible() -> None:
    """`--strict-markers` / `--strict-config` must stay in addopts.

    Without `--strict-markers` a typo'd `@pytest.mark.integraton` silently
    marks nothing, so a test the author believed was gated by a marker runs —
    or is deselected by `-m "not integration"` — for reasons nobody can see.
    """
    text = (REPO_ROOT / "pyproject.toml").read_text(encoding="utf-8")
    addopts = re.search(r'^addopts\s*=\s*"([^"]*)"', text, re.MULTILINE)
    assert addopts, "pyproject [tool.pytest.ini_options] has no addopts"
    for flag in ("--strict-markers", "--strict-config"):
        assert flag in addopts.group(1), (
            f"{flag} was removed from addopts. It is what turns an unknown marker "
            f"from a silent no-op into an error. [bd-dre-rp11j.28]"
        )


@pytest.mark.parametrize("dep", ["numpy", "matplotlib", "pytest_httpx", "respx", "yaml"])
def test_every_import_the_suite_needs_is_declared(dep: str) -> None:
    """Each of these was, at some point, imported by a test and declared nowhere.

    numpy + matplotlib caused the collection abort this bead exists for;
    pytest_httpx + respx cause 33 of 56 files to error on a non-dev install;
    PyYAML is needed by `ligandai campaign run <spec>.yaml`, was declared in
    neither `dependencies` nor any extra, and made
    `test_cli_distinguishes_unreachable_from_unauthorized` read exit 2 where it
    expected 3. Importable here means declared, because the environment running
    this test came from pyproject.
    """
    pytest.importorskip(
        dep,
        reason=(
            f"{dep} is not importable. It is required by this test suite and is "
            f"declared in pyproject.toml (`dev` extra, or `dependencies` for pyyaml) "
            f"— an environment without it did not come from `pip install -e '.[dev]'`. "
            f"[bd-dre-rp11j.28]"
        ),
    )
    text = (REPO_ROOT / "pyproject.toml").read_text(encoding="utf-8")
    dist = {"pytest_httpx": "pytest-httpx", "yaml": "pyyaml"}.get(dep, dep)
    assert dist in text, (
        f"{dep} is importable but {dist!r} appears nowhere in pyproject.toml, so it is "
        f"present by luck. The next clean venv will not have it and the files importing "
        f"it will error at collection — which runs NONE of their tests. [bd-dre-rp11j.28]"
    )
