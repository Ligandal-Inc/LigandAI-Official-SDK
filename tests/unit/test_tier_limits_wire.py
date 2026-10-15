"""Preserve the live tier-limit wire contract without inventing missing quotas."""
import pytest

from ligandai.types import TierLimits


# Redacted policy response recorded from GET /api/user-tier-limits, 2026-09-30.
RECORDED_LIMITS = {
    "tier": "superadmin",
    "gpuSlots": 50,
    "peptidesPerMonth": 10000000,
    "monthlyCredits": 999999999,
    "synthesisAccess": True,
    "exportAccess": True,
}


@pytest.mark.parametrize("name", ["gpuSlots", "maxConcurrentGpuSlots", "max_concurrent_gpu_slots"])
def test_gpu_slots_accepts_server_canonical_and_python_names(name):
    limits = TierLimits.model_validate({"tier": "enterprise", name: 50})
    assert limits.max_concurrent_gpu_slots == 50
    assert limits.model_dump(by_alias=True)["maxConcurrentGpuSlots"] == 50


@pytest.mark.parametrize("payload, expected", [({}, None), ({"gpuSlots": None}, None), ({"gpuSlots": 0}, 0)])
def test_missing_null_and_zero_are_distinct(payload, expected):
    limits = TierLimits.model_validate({"tier": "enterprise", **payload})
    assert limits.max_concurrent_gpu_slots == expected


def test_canonical_name_takes_precedence_including_explicit_null():
    limits = TierLimits.model_validate({"tier": "enterprise", "maxConcurrentGpuSlots": None, "gpuSlots": 50})
    assert limits.max_concurrent_gpu_slots is None


def test_monthly_allowance_does_not_become_per_generation_quota():
    limits = TierLimits.model_validate(RECORDED_LIMITS)
    assert limits.max_concurrent_gpu_slots == 50
    assert limits.max_peptides_per_generation is None
    assert limits.max_folds_per_target is None
    assert limits.max_concurrent_targets is None
    assert limits.rate_limit_per_minute is None
    assert limits.model_extra["peptidesPerMonth"] == RECORDED_LIMITS["peptidesPerMonth"]


def test_sync_account_returns_typed_gpu_slots(client, httpx_mock, base_url):
    httpx_mock.add_response(method="GET", url=base_url + "/api/user-tier-limits", json=RECORDED_LIMITS)
    assert client.account.tier_limits().max_concurrent_gpu_slots == 50


@pytest.mark.asyncio
async def test_async_account_returns_typed_gpu_slots(async_client, httpx_mock, base_url):
    httpx_mock.add_response(method="GET", url=base_url + "/api/user-tier-limits", json=RECORDED_LIMITS)
    assert (await async_client.account.tier_limits()).max_concurrent_gpu_slots == 50
