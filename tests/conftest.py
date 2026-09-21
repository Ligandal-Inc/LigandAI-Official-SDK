# Copyright © 2026 Ligandal, Inc. All rights reserved.
"""Shared pytest fixtures."""

from __future__ import annotations

import os
import pathlib
from collections.abc import Iterator
from typing import Any

import pytest

from ligandai import AsyncLigandAI, LigandAI

TEST_BASE_URL = "http://api.ligandai.test"
TEST_API_KEY = "lgai_pro_testkey0123456789ABCDEF"


def pytest_configure() -> None:
    os.environ.setdefault("LIGANDAI_SKIP_VERSION_CHECK", "1")
    # matplotlib defaults to an interactive backend, which BLOCKS on a headless
    # box: tests/unit/test_fold_charts.py hangs forever rather than failing, and
    # takes the whole unit suite with it. Set before any test imports pyplot.
    os.environ.setdefault("MPLBACKEND", "Agg")


@pytest.fixture
def base_url() -> str:
    return TEST_BASE_URL


@pytest.fixture
def api_key() -> str:
    return TEST_API_KEY


@pytest.fixture
def client(api_key: str, base_url: str) -> Iterator[LigandAI]:
    c = LigandAI(api_key=api_key, base_url=base_url, max_retries=1)
    yield c
    c.close()


@pytest.fixture
async def async_client(api_key: str, base_url: str):
    c = AsyncLigandAI(api_key=api_key, base_url=base_url, max_retries=1)
    yield c
    await c.close()


@pytest.fixture(autouse=True)
def tmp_ligandai_home(tmp_path, monkeypatch):
    """Redirect ``~/.ligandai/`` to a per-test tempdir.

    Auto-used across every test in this suite so the SDK's local dedupe DB
    (``~/.ligandai/submitted.db``) + credit ledger never touch the real user
    home — each test gets a clean tempdir.

    Added 2026-05-17 for the SDK hardening test suite.
    """
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setattr(pathlib.Path, "home", lambda: pathlib.Path(tmp_path))
    yield tmp_path


def _has_live_api() -> bool:
    return bool(os.environ.get("LIGANDAI_TEST_API_KEY"))


def integration_only(fn):
    """Decorator: skip unless LIGANDAI_TEST_API_KEY is set."""
    return pytest.mark.skipif(
        not _has_live_api(), reason="LIGANDAI_TEST_API_KEY not set"
    )(pytest.mark.integration(fn))


# ═══════════════════════════════════════════════════════════════════════════
# ⛔ A COLLECTION ERROR MUST NOT BE ABLE TO MASQUERADE AS A PASSING RUN.
# [bd-dre-rp11j.28]
#
# THE MEASURED DEFECT, reproduced 2026-09-21 on the tree at 3c7faf4:
#
#     $ pip install -e '.[dev]' && pytest tests/unit -q
#     ERROR tests/unit/test_folds.py
#     !!!!!! Interrupted: 1 error during collection !!!!!!
#     1 error in 2.24s
#
# `1 error in 2.24s` is what a suite that ran NOTHING looks like. 56 test files
# and 1,133 tests never executed, and the only number on screen was a `1` —
# which reads as one flaky test, not as a total non-run. The same shape appears
# without the word "Interrupted": `pip install -e . pytest` alone yields
# `1 skipped, 33 errors` — again ZERO of 1,207 tests executed, again with no
# line saying so.
#
# THE EXIT CODE WAS NEVER THE PROBLEM — it was already non-zero. What was
# missing is a sentence naming how many test FILES actually contributed tests.
# So this prints that census on EVERY run, and says in words that an erroring
# file runs none of its tests.
#
# ⛔ IT DELIBERATELY DOES NOT ABORT, and the first version of it did. Raising
# `pytest.UsageError` from `pytest_collection_finish` stopped the run before
# any test executed — turning a suite with one unimportable file into a suite
# that measured nothing, which is the exact failure being guarded against. A
# guard must not be able to become the defect.
#
# The hard assertion lives in
# tests/unit/test_suite_integrity_rp11j28.py, where it fails as one named,
# non-aborting test.
# ═══════════════════════════════════════════════════════════════════════════

_REPO_ROOT = pathlib.Path(__file__).resolve().parents[1]
_TESTS_DIR = _REPO_ROOT / "tests"

#: path -> ("skipped" | "error", longrepr) for files that produced a
#: collect-time report instead of tests. Populated by pytest_collectreport.
_COLLECT_OUTCOMES: dict[pathlib.Path, str] = {}

#: Files that contributed >=1 collected test item this run.
_COLLECTED_FILES: set[pathlib.Path] = set()


def selection_is_filtered(config: pytest.Config) -> bool:
    """Whether the caller deliberately narrowed the run below whole files.

    A census that scolds a deliberately narrowed run is a census people learn
    to ignore, so `-k` / `-m` / `--deselect` / `--lf` runs are left alone.
    """
    opt = config.option
    return bool(
        getattr(opt, "keyword", "")
        or getattr(opt, "markexpr", "")
        or getattr(opt, "deselect", None)
        or getattr(opt, "lf", False)
        or getattr(opt, "failedfirst", False)
        or getattr(opt, "last_failed", False)
    )


def expected_test_files(config: pytest.Config) -> set[pathlib.Path]:
    """Every ``test_*.py`` under the paths this invocation actually asked for.

    Read off the disk at run time rather than from a hand-maintained count, so
    the expectation cannot go stale as files are added.
    """
    raw = [a for a in config.args if not a.startswith("-")]
    targets = [pathlib.Path(a.split("::")[0]) for a in raw] or [_TESTS_DIR]

    expected: set[pathlib.Path] = set()
    for target in targets:
        path = target if target.is_absolute() else (config.rootpath / target)
        if path.is_dir():
            expected.update(p.resolve() for p in path.glob("**/test_*.py"))
        elif path.is_file() and path.name.startswith("test_"):
            expected.add(path.resolve())
    return {p for p in expected if _TESTS_DIR in p.parents}


def pytest_collection_modifyitems(
    session: pytest.Session, config: pytest.Config, items: list[Any]
) -> None:
    """Record which files actually contributed at least one test."""
    for item in items:
        try:
            _COLLECTED_FILES.add(pathlib.Path(str(item.fspath)).resolve())
        except Exception:  # pragma: no cover - defensive
            continue


def pytest_collectreport(report: Any) -> None:
    """Record files that answered collection with a skip or an error."""
    path = getattr(report, "fspath", None)
    if not path:
        return
    try:
        resolved = pathlib.Path(str(path)).resolve()
    except Exception:  # pragma: no cover - defensive
        return
    if report.outcome == "skipped":
        _COLLECT_OUTCOMES[resolved] = "skipped at import"
    elif report.outcome == "failed":
        _COLLECT_OUTCOMES[resolved] = "ERRORED at import — ran none of its tests"


def pytest_terminal_summary(terminalreporter: Any) -> None:
    """State how many test FILES ran. Never let `1 error` be the whole story."""
    config = terminalreporter.config
    if selection_is_filtered(config):
        return
    expected = expected_test_files(config)
    if not expected:
        return

    missing = sorted(expected - _COLLECTED_FILES)
    errors = len(terminalreporter.stats.get("error", []))

    terminalreporter.write_line(
        f"test-file census: {len(expected) - len(missing)} of {len(expected)} test files "
        f"under tests/ contributed tests"
        + (f"; {len(missing)} contributed NOTHING" if missing else "")
    )
    for path in missing:
        why = _COLLECT_OUTCOMES.get(path, "collected no tests and reported nothing")
        terminalreporter.write_line(
            f"    ⛔ {path.relative_to(_REPO_ROOT)} — {why}"
        )
    if missing:
        terminalreporter.write_line(
            "    A file that contributes no tests is not a passing file: NONE of its "
            "tests ran. `N error` is not `N failing tests`. A missing dependency "
            "belongs in the `dev` extra of pyproject.toml. [bd-dre-rp11j.28]"
        )
    elif errors:
        terminalreporter.write_line(
            f"    ⛔ {errors} setup/teardown error(s) — an erroring node runs none of "
            f"its tests. [bd-dre-rp11j.28]"
        )
