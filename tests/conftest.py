# Copyright © 2026 Ligandal, Inc. All rights reserved.
"""Shared pytest fixtures."""

from __future__ import annotations

import os
import pathlib
from collections.abc import Iterator

import pytest

from ligandai import AsyncLigandAI, LigandAI

TEST_BASE_URL = "http://api.ligandai.test"
TEST_API_KEY = "lgai_pro_testkey0123456789ABCDEF"


def pytest_configure() -> None:
    os.environ.setdefault("LIGANDAI_SKIP_VERSION_CHECK", "1")


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
