# Copyright © 2026 Ligandal, Inc. All rights reserved.
"""Client construction, tier detection, and feature gating."""

from __future__ import annotations

import os

import pytest
from pytest_httpx import HTTPXMock

from ligandai import AsyncLigandAI, LigandAI
from ligandai.errors import LigandAITierError

BASE = "http://api.ligandai.test"


def test_construct_with_explicit_key() -> None:
    c = LigandAI(api_key="lgai_pro_test")
    assert c.api_key == "lgai_pro_test"
    assert c.tier == "pro"


def test_construct_from_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("LIGANDAI_API_KEY", "lgai_ent_envkey")
    c = LigandAI()
    assert c.tier == "enterprise"
    assert c.api_key == "lgai_ent_envkey"


def test_anonymous_client() -> None:
    c = LigandAI()
    # If no env var either
    if "LIGANDAI_API_KEY" not in os.environ and "LIGANDAI_TEST_API_KEY" not in os.environ:
        assert c.tier is None
        assert c.api_key is None


def test_unknown_prefix_yields_none_tier() -> None:
    c = LigandAI(api_key="not_a_valid_prefix")
    assert c.tier is None


@pytest.mark.parametrize(
    "key,expected",
    [
        ("lgai_free_abc", "free"),
        ("lgai_basic_abc", "basic"),
        ("lgai_edu_abc", "academia"),
        ("lgai_pro_abc", "pro"),
        ("lgai_ent_abc", "enterprise"),
        ("lgai_sa_abc", "superadmin"),
    ],
)
def test_tier_prefix_detection(key: str, expected: str) -> None:
    c = LigandAI(api_key=key)
    assert c.tier == expected


def test_rate_limit_per_minute_by_tier() -> None:
    assert LigandAI(api_key="lgai_free_x").rate_limit_per_minute == 10
    assert LigandAI(api_key="lgai_basic_x").rate_limit_per_minute == 20
    assert LigandAI(api_key="lgai_edu_x").rate_limit_per_minute == 30
    assert LigandAI(api_key="lgai_pro_x").rate_limit_per_minute == 60
    assert LigandAI(api_key="lgai_ent_x").rate_limit_per_minute == 300


def test_max_peptides_per_generation_by_tier() -> None:
    assert LigandAI(api_key="lgai_free_x").max_peptides_per_generation == 10
    assert LigandAI(api_key="lgai_basic_x").max_peptides_per_generation == 100
    assert LigandAI(api_key="lgai_edu_x").max_peptides_per_generation == 300
    assert LigandAI(api_key="lgai_pro_x").max_peptides_per_generation == 300
    assert LigandAI(api_key="lgai_ent_x").max_peptides_per_generation == 1000


def test_folding_gpu_caps_by_tier() -> None:
    assert LigandAI(api_key="lgai_free_x").max_concurrent_gpu_slots == 1
    assert LigandAI(api_key="lgai_basic_x").max_concurrent_gpu_slots == 4
    assert LigandAI(api_key="lgai_edu_x").max_concurrent_gpu_slots == 16
    assert LigandAI(api_key="lgai_pro_x").max_concurrent_gpu_slots == 25
    assert LigandAI(api_key="lgai_ent_x").max_concurrent_gpu_slots == 50


def test_target_and_fold_caps_by_tier() -> None:
    assert LigandAI(api_key="lgai_free_x").max_targets_per_generation == 3
    assert LigandAI(api_key="lgai_basic_x").max_targets_per_generation is None
    assert LigandAI(api_key="lgai_free_x").max_folds_per_generation == 10
    assert LigandAI(api_key="lgai_pro_x").max_folds_per_generation is None


def test_feature_allowed_by_tier() -> None:
    free = LigandAI(api_key="lgai_free_x")
    assert free.feature_allowed("search_receptors")
    assert free.feature_allowed("generate_peptides")
    assert free.feature_allowed("predict_structure")
    assert not free.feature_allowed("advanced_guidance")
    assert not free.feature_allowed("transport_vasculome")

    basic = LigandAI(api_key="lgai_basic_x")
    assert basic.feature_allowed("generate_peptides")
    assert basic.feature_allowed("predict_structure")
    assert not basic.feature_allowed("transcriptomics_analysis")
    assert not basic.feature_allowed("transport_vasculome")

    edu = LigandAI(api_key="lgai_edu_x")
    assert edu.feature_allowed("generate_peptides")
    assert edu.feature_allowed("advanced_guidance")
    assert not edu.feature_allowed("transport_vasculome")
    assert not edu.feature_allowed("batch_operations")

    ent = LigandAI(api_key="lgai_ent_x")
    assert ent.feature_allowed("transport_vasculome")
    assert ent.feature_allowed("batch_operations")

    sa = LigandAI(api_key="lgai_sa_x")
    assert sa.feature_allowed("batch_operations")
    assert sa.feature_allowed("anything_unknown")


def test_unknown_feature_passes_client_side() -> None:
    """Unknown features pass — server gates them."""
    c = LigandAI(api_key="lgai_free_x")
    assert c.feature_allowed("future_feature_xyz")


def test_require_feature_raises_for_low_tier() -> None:
    free = LigandAI(api_key="lgai_free_x")
    with pytest.raises(LigandAITierError) as exc_info:
        free._require_feature("advanced_guidance")
    assert exc_info.value.required_tier == "academia"
    assert exc_info.value.current_tier == "free"


def test_require_feature_passes_for_sufficient_tier() -> None:
    pro = LigandAI(api_key="lgai_pro_x")
    pro._require_feature("generate_peptides")  # no raise


def test_repr_hides_api_key() -> None:
    c = LigandAI(api_key="lgai_pro_secret_token_xyz")
    rep = repr(c)
    assert "secret_token" not in rep
    assert "tier='pro'" in rep


def test_resource_namespaces_present() -> None:
    c = LigandAI(api_key="lgai_pro_x")
    for name in (
        "account", "receptors", "structures", "proteins", "discovery",
        "diseases", "peptides", "bivalent", "synthesis", "memory",
        "programs", "charts", "reports", "jobs",
    ):
        assert hasattr(c, name), f"client missing namespace: {name}"


def test_context_manager() -> None:
    with LigandAI(api_key="lgai_pro_x") as c:
        assert c.tier == "pro"


def test_top_level_generate_uses_production_generation_endpoint(
    httpx_mock: HTTPXMock,
) -> None:
    c = LigandAI(api_key="lgai_pro_x", base_url=BASE, max_retries=1)
    httpx_mock.add_response(
        url=f"{BASE}/api/ptf/parallel/generate",
        method="POST",
        json={"sessionId": "gen_1", "status": "queued"},
    )

    job = c.generate("EGFR", n_samples=7, auto_fold=False)

    assert job.session_id == "gen_1"
    request = httpx_mock.get_request()
    assert request is not None
    assert str(request.url) == f"{BASE}/api/ptf/parallel/generate"
    assert "/api/workers/" not in str(request.url)
    body = request.read().decode()
    assert '"gene":"EGFR"' in body
    assert '"peptidesPerTarget":7' in body


def test_top_level_generate_rejects_unmounted_worker_methods() -> None:
    c = LigandAI(api_key="lgai_pro_x", base_url=BASE, max_retries=1)
    with pytest.raises(NotImplementedError, match="worker routes are not mounted"):
        c.generate("EGFR", method="bindcraft")


def test_top_level_generate_rejects_raw_sequence_target() -> None:
    c = LigandAI(api_key="lgai_pro_x", base_url=BASE, max_retries=1)
    with pytest.raises(ValueError, match="target=<sequence>"):
        c.generate("M" * 35)


def test_top_level_fold_uses_production_folding_endpoint(
    httpx_mock: HTTPXMock,
) -> None:
    c = LigandAI(api_key="lgai_pro_x", base_url=BASE, max_retries=1)
    httpx_mock.add_response(
        url=f"{BASE}/api/folding/predict",
        method="POST",
        json={"jobId": "fold_1", "status": "queued"},
    )

    job = c.fold("M" * 35, "ACDEFGHIK", sampling_steps=15)

    assert job.id == "fold_1"
    request = httpx_mock.get_request()
    assert request is not None
    assert str(request.url) == f"{BASE}/api/folding/predict"
    assert "/api/workers/" not in str(request.url)
    body = request.read().decode()
    assert '"chainId":"A"' in body
    assert '"chainId":"B"' in body
    assert '"samplingSteps":15' in body


def test_top_level_fold_accepts_sequences_keyword(httpx_mock: HTTPXMock) -> None:
    c = LigandAI(api_key="lgai_pro_x", base_url=BASE, max_retries=1)
    httpx_mock.add_response(
        url=f"{BASE}/api/folding/predict",
        method="POST",
        json={"jobId": "fold_2"},
    )

    job = c.fold(sequences=["ACDEFGHIK"], target_gene="EGFR")

    assert job.id == "fold_2"
    request = httpx_mock.get_request()
    assert request is not None
    assert str(request.url) == f"{BASE}/api/folding/predict"
    assert "/api/workers/" not in str(request.url)


@pytest.mark.asyncio
async def test_async_construct() -> None:
    async with AsyncLigandAI(api_key="lgai_pro_x") as c:
        assert c.tier == "pro"
