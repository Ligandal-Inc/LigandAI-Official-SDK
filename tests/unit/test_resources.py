# Copyright © 2025 Ligandal, Inc. All rights reserved.
"""Resource methods against a mocked HTTP server."""

from __future__ import annotations

import pytest
from pytest_httpx import HTTPXMock

from ligandai import LigandAI
from ligandai.errors import LigandAIAuthError, LigandAICreditError, LigandAITierError
from ligandai.types import BivalentTarget, LinkerConfig, ResidueRange, TargetGroup

BASE = "http://api.ligandai.test"


@pytest.fixture
def client() -> LigandAI:
    return LigandAI(api_key="lgai_pro_test123", base_url=BASE, max_retries=1)


# -- Account ---------------------------------------------------------------


def test_account_me(httpx_mock: HTTPXMock, client: LigandAI) -> None:
    httpx_mock.add_response(
        url=f"{BASE}/api/auth/user",
        json={"id": "u_1", "email": "test@ligandal.com", "subscriptionTier": "pro"},
    )
    user = client.account.me()
    assert user.id == "u_1"
    assert user.email == "test@ligandal.com"
    assert user.subscription_tier == "pro"


def test_account_credits(httpx_mock: HTTPXMock, client: LigandAI) -> None:
    httpx_mock.add_response(
        url=f"{BASE}/api/user-credits",
        json={"balance": 12345, "monthlyAllocation": 5000},
    )
    credits = client.account.credits()
    assert credits.balance == 12345
    assert credits.monthly_allocation == 5000


# -- Receptors -------------------------------------------------------------


def test_receptors_search(httpx_mock: HTTPXMock, client: LigandAI) -> None:
    httpx_mock.add_response(
        url=f"{BASE}/api/receptordb/search?query=EGFR&limit=10",
        json=[
            {"id": "c1", "complexName": "EGFR-EGF", "gene": "EGFR", "oligomericState": "monomer"},
            {"id": "c2", "complexName": "EGFR dimer", "gene": "EGFR", "oligomericState": "dimer"},
        ],
    )
    hits = client.receptors.search("EGFR")
    assert len(hits) == 2
    assert hits[0].complex_name == "EGFR-EGF"
    assert hits[1].oligomeric_state == "dimer"


def test_receptors_list_with_pagination(httpx_mock: HTTPXMock, client: LigandAI) -> None:
    httpx_mock.add_response(
        url=f"{BASE}/api/receptordb/complexes?offset=0&limit=2&sort_by=name&sort_order=asc",
        json={"complexes": [{"id": "c1"}, {"id": "c2"}], "total": 100, "offset": 0, "limit": 2},
    )
    page = client.receptors.list(offset=0, limit=2)
    assert len(page.complexes) == 2
    assert page.total == 100
    assert page.has_more


# -- Discovery -------------------------------------------------------------


def test_discovery_tissue_markers(httpx_mock: HTTPXMock, client: LigandAI) -> None:
    httpx_mock.add_response(
        url=f"{BASE}/api/transcriptomics/top-markers",
        method="POST",
        json={
            "top": [
                {"gene": "ASGR1", "si": 250.5, "rank": 1, "receptor": True},
                {"gene": "ALB", "si": 180.0, "rank": 2, "receptor": False},
            ],
            "total": 2,
        },
    )
    res = client.discovery.tissue_markers(target_tissues=["Liver"], top_n=2)
    assert len(res.top) == 2
    assert res.top[0].gene == "ASGR1"
    assert res.top[0].si == 250.5


def test_discovery_compare_groups(httpx_mock: HTTPXMock, client: LigandAI) -> None:
    httpx_mock.add_response(
        url=f"{BASE}/api/transcriptomics/compare-groups",
        method="POST",
        json={"targetGroup": "Liver", "referenceGroups": [], "mode": "compare", "results": []},
    )
    target = TargetGroup(name="Liver", samples=["LIVER_001"])
    res = client.discovery.compare_groups(target_group=target)
    assert res.target_group == "Liver"


# -- Tier gating -----------------------------------------------------------


def test_tier_gating_blocks_transport_vasculome_for_pro() -> None:
    """Pro tier cannot call transport vasculome — raises before HTTP."""
    c = LigandAI(api_key="lgai_pro_x", base_url=BASE)
    with pytest.raises(LigandAITierError) as exc_info:
        c.discovery.transport_vasculome(modality="monovalent")
    assert exc_info.value.required_tier == "enterprise"


def test_tier_gating_allows_for_enterprise(httpx_mock: HTTPXMock) -> None:
    httpx_mock.add_response(
        url=f"{BASE}/api/transport-vasculome/query",
        method="POST",
        json=[{"gene": "TFRC", "score": 0.92}, {"gene": "INSR", "score": 0.85}],
    )
    c = LigandAI(api_key="lgai_ent_x", base_url=BASE, max_retries=1)
    res = c.discovery.transport_vasculome(modality="monovalent")
    assert len(res) == 2
    assert res[0].gene == "TFRC"


# -- Errors --------------------------------------------------------------


def test_401_raises_auth_error(httpx_mock: HTTPXMock, client: LigandAI) -> None:
    httpx_mock.add_response(
        url=f"{BASE}/api/auth/user",
        status_code=401,
        json={"error": "invalid", "code": "E001", "message": "Bad key"},
    )
    with pytest.raises(LigandAIAuthError) as exc_info:
        client.account.me()
    assert exc_info.value.code == "E001"
    assert exc_info.value.status_code == 401


def test_402_raises_credit_error(httpx_mock: HTTPXMock, client: LigandAI) -> None:
    httpx_mock.add_response(
        url=f"{BASE}/api/transcriptomics/top-markers",
        method="POST",
        status_code=402,
        json={"code": "E004", "message": "low", "required": 500, "available": 50},
    )
    with pytest.raises(LigandAICreditError) as exc_info:
        client.discovery.tissue_markers(target_tissues=["Liver"])
    assert exc_info.value.required == 500
    assert exc_info.value.available == 50


def test_403_raises_tier_error_with_fields(httpx_mock: HTTPXMock, client: LigandAI) -> None:
    httpx_mock.add_response(
        url=f"{BASE}/api/auth/user",
        status_code=403,
        json={
            "error": "tier",
            "code": "E002",
            "currentTier": "pro",
            "requiredTier": "enterprise",
        },
    )
    with pytest.raises(LigandAITierError) as exc_info:
        client.account.me()
    assert exc_info.value.current_tier == "pro"
    assert exc_info.value.required_tier == "enterprise"


# -- Bivalent type wrappers ----------------------------------------------


def test_bivalent_pydantic_targets_serialize(httpx_mock: HTTPXMock) -> None:
    httpx_mock.add_response(
        url=f"{BASE}/api/ligandforge/bivalent/run1",
        method="POST",
        json={"session_id": "biv_abc", "status": "run1_queued"},
    )
    c = LigandAI(api_key="lgai_pro_x", base_url=BASE, max_retries=1)
    session = c.bivalent.start(
        target1=BivalentTarget(gene="PDCD1"),
        target2=BivalentTarget(gene="CD274"),
        linker=LinkerConfig(position="C", length_min=8, length_max=20),
        binder_length_min=15,
        binder_length_max=40,
        num_designs=100,
    )
    assert session.id == "biv_abc"


def test_residue_range_dump_format() -> None:
    r = ResidueRange(chain="A", start=300, end=320, label="binding pocket")
    assert r.range == "A:300-320"
    dumped = r.model_dump(by_alias=True)
    assert dumped["chain"] == "A"
    assert dumped["start"] == 300
    assert dumped["end"] == 320
