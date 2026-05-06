# Copyright © 2026 Ligandal, Inc. All rights reserved.
"""Unit tests for billing surface: get_balance, top_up, configure_auto_topup, billing_usage."""

from __future__ import annotations

import pytest
from pytest_httpx import HTTPXMock

from ligandai import LigandAI
from ligandai.types import (
    AccountBalance,
    AutoTopupConfig,
    ClientSessionUsage,
    CreditTransaction,
    TopUpResult,
)

BASE = "http://api.ligandai.test"


@pytest.fixture
def client() -> LigandAI:
    return LigandAI(api_key="lgai_pro_test123", base_url=BASE, max_retries=1)


@pytest.fixture
def free_client() -> LigandAI:
    return LigandAI(api_key="lgai_free_test123", base_url=BASE, max_retries=1)


# ---------------------------------------------------------------------------
# account.get_balance()  →  GET /api/billing/account-summary
# ---------------------------------------------------------------------------


def test_get_balance_full_response(httpx_mock: HTTPXMock, client: LigandAI) -> None:
    httpx_mock.add_response(
        url=f"{BASE}/api/billing/account-summary",
        json={
            "balance": 42000,
            "burnRate30d": 3200,
            "daysRemaining": 13.125,
            "tier": "pro",
            "autoTopupEnabled": False,
        },
    )
    bal = client.account.get_balance()
    assert isinstance(bal, AccountBalance)
    assert bal.credits == 42000
    assert bal.burn_rate_30d == 3200
    assert bal.days_remaining == pytest.approx(13.125)
    assert bal.tier == "pro"
    assert bal.auto_topup_enabled is False


def test_get_balance_minimal_response(httpx_mock: HTTPXMock, client: LigandAI) -> None:
    """Server may omit optional fields — model must tolerate sparse payload."""
    httpx_mock.add_response(
        url=f"{BASE}/api/billing/account-summary",
        json={"balance": 500},
    )
    bal = client.account.get_balance()
    assert bal.credits == 500
    assert bal.burn_rate_30d is None
    assert bal.days_remaining is None


def test_get_balance_high_balance(httpx_mock: HTTPXMock, client: LigandAI) -> None:
    httpx_mock.add_response(
        url=f"{BASE}/api/billing/account-summary",
        json={"balance": 10_000_000, "tier": "enterprise"},
    )
    bal = client.account.get_balance()
    assert bal.credits == 10_000_000
    assert bal.tier == "enterprise"


def test_get_balance_zero_balance(httpx_mock: HTTPXMock, client: LigandAI) -> None:
    httpx_mock.add_response(
        url=f"{BASE}/api/billing/account-summary",
        json={"balance": 0, "tier": "free", "autoTopupEnabled": True},
    )
    bal = client.account.get_balance()
    assert bal.credits == 0
    assert bal.auto_topup_enabled is True


def test_client_session_id_header_sent(httpx_mock: HTTPXMock) -> None:
    client = LigandAI(
        api_key="lgai_pro_test123",
        base_url=BASE,
        max_retries=1,
        client_session_id="codex-run-1",
    )
    httpx_mock.add_response(
        url=f"{BASE}/api/billing/account-summary",
        json={"balance": 1234, "tier": "pro"},
    )

    client.account.get_balance()

    request = httpx_mock.get_requests()[0]
    assert request.headers["X-LigandAI-Client-Session-Id"] == "codex-run-1"


def test_credit_session_context_tracks_delta_and_restores_header(
    httpx_mock: HTTPXMock,
    client: LigandAI,
) -> None:
    httpx_mock.add_response(
        url=f"{BASE}/api/billing/account-summary",
        json={"balance": 1000, "tier": "pro"},
    )
    httpx_mock.add_response(
        url=f"{BASE}/api/auth/user",
        json={"id": "user_1", "email": "agent@example.com"},
    )
    httpx_mock.add_response(
        url=f"{BASE}/api/billing/account-summary",
        json={"balance": 850, "tier": "pro"},
    )

    with client.session("codex-run-2") as run:
        assert client.client_session_id == "codex-run-2"
        client.account.me()

    assert client.client_session_id is None
    assert run.start_credits == 1000
    assert run.end_credits == 850
    assert run.credits_used == 150
    assert all(
        request.headers["X-LigandAI-Client-Session-Id"] == "codex-run-2"
        for request in httpx_mock.get_requests()
    )


def test_account_session_usage(httpx_mock: HTTPXMock, client: LigandAI) -> None:
    httpx_mock.add_response(
        url=f"{BASE}/api/account/session-usage?session_id=codex-run-2&period=30d",
        json={
            "client_session_id": "codex-run-2",
            "calls": [
                {
                    "id": 1,
                    "method": "POST",
                    "endpoint": "/api/ptf/parallel/generate",
                    "status_code": 200,
                    "latency_ms": 430,
                    "client_session_id": "codex-run-2",
                }
            ],
            "summary": {
                "total_calls": 1,
                "successful_calls": 1,
                "error_calls": 0,
                "credits_used": 250,
                "credit_events": 1,
            },
            "period_days": 30,
        },
    )

    usage = client.account.session_usage("codex-run-2")

    assert isinstance(usage, ClientSessionUsage)
    assert usage.client_session_id == "codex-run-2"
    assert usage.summary.total_calls == 1
    assert usage.summary.credits_used == 250
    assert usage.calls[0].endpoint == "/api/ptf/parallel/generate"


# ---------------------------------------------------------------------------
# account.billing_usage()  →  GET /api/billing/account-summary?period=...
# ---------------------------------------------------------------------------


def test_billing_usage_default_period(httpx_mock: HTTPXMock, client: LigandAI) -> None:
    httpx_mock.add_response(
        url=f"{BASE}/api/billing/account-summary?period=30d",
        json={
            "balance": 9000,
            "recent_transactions": [
                {"id": 1, "amount": -150, "operation": "generate_peptides", "description": "gen run"},
                {"id": 2, "amount": -300, "operation": "fold_structure", "description": "Boltz-2 fold"},
            ],
        },
    )
    txns = client.account.billing_usage()
    assert len(txns) == 2
    assert all(isinstance(t, CreditTransaction) for t in txns)
    assert txns[0].amount == -150
    assert txns[0].operation == "generate_peptides"
    assert txns[1].amount == -300


def test_billing_usage_7d(httpx_mock: HTTPXMock, client: LigandAI) -> None:
    httpx_mock.add_response(
        url=f"{BASE}/api/billing/account-summary?period=7d",
        json={"balance": 5000, "recentTransactions": [{"id": 99, "amount": -50, "operation": "ai_query"}]},
    )
    txns = client.account.billing_usage(period="7d")
    assert len(txns) == 1
    assert txns[0].id == 99


def test_billing_usage_90d(httpx_mock: HTTPXMock, client: LigandAI) -> None:
    httpx_mock.add_response(
        url=f"{BASE}/api/billing/account-summary?period=90d",
        json={"balance": 1000, "transactions": []},
    )
    txns = client.account.billing_usage(period="90d")
    assert txns == []


def test_billing_usage_accepts_camel_and_snake_keys(httpx_mock: HTTPXMock, client: LigandAI) -> None:
    """Server may return camelCase or snake_case transaction fields."""
    httpx_mock.add_response(
        url=f"{BASE}/api/billing/account-summary?period=30d",
        json={
            "balance": 8000,
            "recent_transactions": [
                {
                    "id": 7,
                    "amount": -200,
                    "balanceAfter": 7800,
                    "occurredAt": "2025-04-01T10:00:00Z",
                    "description": "fold run",
                }
            ],
        },
    )
    txns = client.account.billing_usage()
    assert txns[0].balance_after == 7800
    assert txns[0].occurred_at is not None


# ---------------------------------------------------------------------------
# account.top_up()  →  POST /api/billing/topup
# ---------------------------------------------------------------------------


def test_top_up_off_session_charge(httpx_mock: HTTPXMock, client: LigandAI) -> None:
    """When a payment method is on file the server charges immediately."""
    httpx_mock.add_response(
        url=f"{BASE}/api/billing/topup",
        method="POST",
        json={"success": True, "creditsAdded": 20000, "newBalance": 62000},
    )
    result = client.account.top_up(amount_usd=200)
    assert isinstance(result, TopUpResult)
    assert result.success is True
    assert result.credits_added == 20000
    assert result.new_balance == 62000
    assert result.checkout_url is None


def test_top_up_browser_checkout_flow(httpx_mock: HTTPXMock, client: LigandAI) -> None:
    """When no payment method is saved the server returns a checkout URL."""
    httpx_mock.add_response(
        url=f"{BASE}/api/billing/topup",
        method="POST",
        json={
            "success": True,
            "creditsAdded": None,
            "newBalance": None,
            "checkoutUrl": "https://checkout.stripe.com/pay/cs_test_abc123",
        },
    )
    result = client.account.top_up(amount_usd=100)
    assert result.success is True
    assert result.checkout_url == "https://checkout.stripe.com/pay/cs_test_abc123"
    assert result.credits_added is None


def test_top_up_with_saved_payment_method(httpx_mock: HTTPXMock, client: LigandAI) -> None:
    httpx_mock.add_response(
        url=f"{BASE}/api/billing/topup",
        method="POST",
        json={"success": True, "creditsAdded": 5000, "newBalance": 15000},
    )
    result = client.account.top_up(amount_usd=50, payment_method_id="pm_abc123")
    assert result.success is True
    assert result.credits_added == 5000


def test_top_up_stripe_not_configured(httpx_mock: HTTPXMock, client: LigandAI) -> None:
    """503 from server when Stripe is not configured raises LigandAIServerError."""
    from ligandai.errors import LigandAIServerError

    httpx_mock.add_response(
        url=f"{BASE}/api/billing/topup",
        method="POST",
        status_code=503,
        json={"success": False, "error": "stripe_not_configured"},
    )
    with pytest.raises(LigandAIServerError):
        client.account.top_up(amount_usd=50)


def test_top_up_small_amount(httpx_mock: HTTPXMock, client: LigandAI) -> None:
    """Minimum top-up ($5)."""
    httpx_mock.add_response(
        url=f"{BASE}/api/billing/topup",
        method="POST",
        json={"success": True, "creditsAdded": 500, "newBalance": 1500},
    )
    result = client.account.top_up(amount_usd=5)
    assert result.credits_added == 500


def test_top_up_save_card_flag(httpx_mock: HTTPXMock, client: LigandAI) -> None:
    """save_card=True should be forwarded in the request body."""
    def _check_body(request):
        import json
        body = json.loads(request.content)
        assert body.get("saveCard") is True
        return None

    httpx_mock.add_response(
        url=f"{BASE}/api/billing/topup",
        method="POST",
        json={"success": True, "creditsAdded": 2000, "newBalance": 12000},
    )
    result = client.account.top_up(amount_usd=20, save_card=True)
    assert result.success is True


# ---------------------------------------------------------------------------
# account.configure_auto_topup()  →  POST /api/billing/auto-topup/configure
# ---------------------------------------------------------------------------


def test_configure_auto_topup_enable(httpx_mock: HTTPXMock, client: LigandAI) -> None:
    httpx_mock.add_response(
        url=f"{BASE}/api/billing/auto-topup/configure",
        method="POST",
        json={
            "enabled": True,
            "thresholdCredits": 10000,
            "amountUsd": 200,
        },
    )
    cfg = client.account.configure_auto_topup(enabled=True, threshold_credits=10000, amount_usd=200)
    assert isinstance(cfg, AutoTopupConfig)
    assert cfg.enabled is True
    assert cfg.threshold_credits == 10000
    assert cfg.amount_usd == 200


def test_configure_auto_topup_disable(httpx_mock: HTTPXMock, client: LigandAI) -> None:
    httpx_mock.add_response(
        url=f"{BASE}/api/billing/auto-topup/configure",
        method="POST",
        json={"enabled": False, "thresholdCredits": None, "amountUsd": None},
    )
    cfg = client.account.configure_auto_topup(enabled=False)
    assert cfg.enabled is False
    assert cfg.threshold_credits is None


def test_configure_auto_topup_all_valid_amounts(httpx_mock: HTTPXMock, client: LigandAI) -> None:
    for usd in (50, 100, 200, 500, 1000, 2000):
        httpx_mock.add_response(
            url=f"{BASE}/api/billing/auto-topup/configure",
            method="POST",
            json={"enabled": True, "thresholdCredits": 5000, "amountUsd": usd},
        )
        cfg = client.account.configure_auto_topup(enabled=True, threshold_credits=5000, amount_usd=usd)
        assert cfg.enabled is True
        assert cfg.amount_usd == usd


def test_configure_auto_topup_default_params(httpx_mock: HTTPXMock, client: LigandAI) -> None:
    """Default threshold=10000, amount_usd=200."""
    httpx_mock.add_response(
        url=f"{BASE}/api/billing/auto-topup/configure",
        method="POST",
        json={"enabled": True, "thresholdCredits": 10000, "amountUsd": 200},
    )
    cfg = client.account.configure_auto_topup(enabled=True)
    assert cfg.threshold_credits == 10000
    assert cfg.amount_usd == 200


def test_configure_auto_topup_with_failure_count(httpx_mock: HTTPXMock, client: LigandAI) -> None:
    """Server may return failureCount — model should capture it."""
    httpx_mock.add_response(
        url=f"{BASE}/api/billing/auto-topup/configure",
        method="POST",
        json={"enabled": True, "thresholdCredits": 8000, "amountUsd": 100, "failureCount": 0},
    )
    cfg = client.account.configure_auto_topup(enabled=True, threshold_credits=8000, amount_usd=100)
    assert cfg.failure_count == 0


def test_configure_auto_topup_last_charged_at(httpx_mock: HTTPXMock, client: LigandAI) -> None:
    httpx_mock.add_response(
        url=f"{BASE}/api/billing/auto-topup/configure",
        method="POST",
        json={
            "enabled": True,
            "thresholdCredits": 5000,
            "amountUsd": 50,
            "lastChargedAt": "2025-03-15T14:22:00Z",
        },
    )
    cfg = client.account.configure_auto_topup(enabled=True, threshold_credits=5000, amount_usd=50)
    assert cfg.last_charged_at is not None


# ---------------------------------------------------------------------------
# Tier gate: top_up and configure_auto_topup should pass through for pro+
# (free tier users get server-side errors, SDK doesn't pre-block these)
# ---------------------------------------------------------------------------


def test_top_up_server_403_free_tier(httpx_mock: HTTPXMock, free_client: LigandAI) -> None:
    """Server returns 403 when free-tier user tries to top up without card — raises LigandAITierError."""
    from ligandai.errors import LigandAITierError

    httpx_mock.add_response(
        url=f"{BASE}/api/billing/topup",
        method="POST",
        status_code=403,
        json={"error": "payment_method_required"},
    )
    with pytest.raises(LigandAITierError):
        free_client.account.top_up(amount_usd=50)


def test_configure_auto_topup_server_403_free_tier(httpx_mock: HTTPXMock, free_client: LigandAI) -> None:
    """Server returns 403 when free-tier user tries to enable auto top-up — raises LigandAITierError."""
    from ligandai.errors import LigandAITierError

    httpx_mock.add_response(
        url=f"{BASE}/api/billing/auto-topup/configure",
        method="POST",
        status_code=403,
        json={"error": "tier_required"},
    )
    with pytest.raises(LigandAITierError):
        free_client.account.configure_auto_topup(enabled=True)
