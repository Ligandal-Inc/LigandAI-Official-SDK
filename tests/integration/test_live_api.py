# Copyright © 2025 Ligandal, Inc. All rights reserved.
"""Integration tests against a live LIGANDAI server.

Skipped unless ``LIGANDAI_TEST_API_KEY`` is set. To run against the dev server:

    LIGANDAI_TEST_API_KEY=lgai_pro_... \
    LIGANDAI_TEST_BASE_URL=http://localhost:5050 \
    pytest tests/integration/ -v
"""

from __future__ import annotations

import os

import pytest

from ligandai import LigandAI

pytestmark = pytest.mark.skipif(
    "LIGANDAI_TEST_API_KEY" not in os.environ,
    reason="LIGANDAI_TEST_API_KEY not set",
)


@pytest.fixture
def live_client() -> LigandAI:
    return LigandAI(
        api_key=os.environ["LIGANDAI_TEST_API_KEY"],
        base_url=os.environ.get("LIGANDAI_TEST_BASE_URL", "http://localhost:5050"),
        max_retries=2,
    )


@pytest.mark.integration
def test_health(live_client: LigandAI) -> None:
    res = live_client.health()
    assert isinstance(res, dict)


@pytest.mark.integration
def test_me(live_client: LigandAI) -> None:
    user = live_client.account.me()
    assert user.id


@pytest.mark.integration
def test_credits(live_client: LigandAI) -> None:
    c = live_client.account.credits()
    assert c.balance >= 0


@pytest.mark.integration
def test_search_receptordb(live_client: LigandAI) -> None:
    hits = live_client.receptors.search("EGFR", limit=3)
    assert all(h.gene or h.complex_name for h in hits)


@pytest.mark.integration
def test_tier_detected(live_client: LigandAI) -> None:
    assert live_client.tier in ("free", "academia", "pro", "enterprise", "superadmin")
