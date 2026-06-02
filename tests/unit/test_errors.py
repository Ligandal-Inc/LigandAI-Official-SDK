# Copyright © 2026 Ligandal, Inc. All rights reserved.
"""Error hierarchy + response → exception mapping."""

from __future__ import annotations

import pytest

from ligandai.errors import (
    LigandAIAuthError,
    LigandAICreditError,
    LigandAIError,
    LigandAIForbidden,
    LigandAINotFoundError,
    LigandAIRateLimitError,
    LigandAIServerError,
    LigandAITierError,
    LigandAIValidationError,
    error_from_response,
)


def test_inheritance() -> None:
    assert issubclass(LigandAIAuthError, LigandAIError)
    assert issubclass(LigandAITierError, LigandAIError)
    assert issubclass(LigandAIRateLimitError, LigandAIError)
    assert issubclass(LigandAICreditError, LigandAIError)
    assert issubclass(LigandAINotFoundError, LigandAIError)
    assert issubclass(LigandAIServerError, LigandAIError)
    assert issubclass(LigandAIValidationError, LigandAIError)


def test_error_attributes() -> None:
    err = LigandAIError(
        "boom",
        code="E007",
        status_code=500,
        request_id="req_abc",
        response={"foo": "bar"},
    )
    assert err.message == "boom"
    assert err.code == "E007"
    assert err.status_code == 500
    assert err.request_id == "req_abc"
    assert err.response == {"foo": "bar"}


def test_tier_error_has_tier_fields() -> None:
    err = LigandAITierError(
        "need pro",
        current_tier="free",
        required_tier="pro",
    )
    assert err.current_tier == "free"
    assert err.required_tier == "pro"


def test_credit_error_has_balance_fields() -> None:
    err = LigandAICreditError(
        "low credits",
        required=1000,
        available=50,
    )
    assert err.required == 1000
    assert err.available == 50
    # shortfall derived when required+available known and the
    # server didn't send an explicit shortfall.
    assert err.shortfall == 950
    # Other recovery fields default to None when not supplied.
    assert err.recovery_url is None
    assert err.top_up_usd is None
    assert err.upgrade_url is None


def test_credit_error_carries_recovery_metadata() -> None:
    """graceful credit-exhaustion UX surface."""
    err = LigandAICreditError(
        "Insufficient credits",
        required=10_000,
        available=200,
        shortfall=9_800,
        recovery_url="/account/billing?recovery=insufficient_credits&source=fold_batch&topup=99",
        top_up_usd=99,
        upgrade_url="https://ligandai.com/pricing",
    )
    assert err.shortfall == 9_800
    assert err.recovery_url.startswith("/account/billing")
    assert err.top_up_usd == 99
    assert err.upgrade_url == "https://ligandai.com/pricing"


def test_credit_error_shortfall_never_negative() -> None:
    """If somehow available > required, derived shortfall should clamp to 0."""
    err = LigandAICreditError("weird", required=10, available=999)
    assert err.shortfall == 0


def test_rate_limit_error_has_retry_after() -> None:
    err = LigandAIRateLimitError("slow down", retry_after=30.0)
    assert err.retry_after == 30.0


@pytest.mark.parametrize(
    "status,expected_class",
    [
        (401, LigandAIAuthError),
        (402, LigandAICreditError),
        # 403 with no tier indicators in the payload is a generic Forbidden
        # (pilot allowlist, ownership check, legal acceptance, payment method
        # required). 0.3.7 stopped lying about tiers on every 403.
        (403, LigandAIForbidden),
        (404, LigandAINotFoundError),
        (400, LigandAIValidationError),
        (422, LigandAIValidationError),
        (429, LigandAIRateLimitError),
        (500, LigandAIServerError),
        (502, LigandAIServerError),
        (503, LigandAIServerError),
        (504, LigandAIServerError),
    ],
)
def test_error_from_response_maps_status(status: int, expected_class: type) -> None:
    err = error_from_response(status, {"message": "x", "code": "E001"})
    assert isinstance(err, expected_class)


def test_error_from_response_403_with_tier_fields_is_tier_error() -> None:
    """403 with `requiredTier` or `tier_required` is still a tier error."""
    err = error_from_response(
        403,
        {"error": "Subscription required", "requiredTier": "pro", "currentTier": "basic"},
    )
    assert isinstance(err, LigandAITierError)


def test_error_from_response_403_pilot_restricted_is_forbidden() -> None:
    """403 with `error_code: pilot_restricted` is Forbidden, not Tier."""
    err = error_from_response(
        403,
        {"error": "AutoResearch is pilot-only", "error_code": "pilot_restricted"},
    )
    assert isinstance(err, LigandAIForbidden)
    assert not isinstance(err, LigandAITierError)
    assert err.reason == "pilot_restricted"
    assert err.status_code == 403


def test_error_from_response_extracts_tier_fields() -> None:
    err = error_from_response(
        403,
        {
            "error": "insufficient tier",
            "code": "E002",
            "currentTier": "free",
            "requiredTier": "pro",
        },
    )
    assert isinstance(err, LigandAITierError)
    assert err.current_tier == "free"
    assert err.required_tier == "pro"


def test_error_from_response_extracts_credit_fields() -> None:
    err = error_from_response(
        402,
        {"message": "low credits", "code": "E004", "required": 500, "available": 25},
    )
    assert isinstance(err, LigandAICreditError)
    assert err.required == 500
    assert err.available == 25


def test_error_from_response_402_structured_insufficient_credits() -> None:
    """server returns structured INSUFFICIENT_CREDITS payload
    matching the platform + the patched /v1/folding/predict-batch
    response. The SDK wires every field onto LigandAICreditError so callers
    can render a top-off CTA.
    """
    err = error_from_response(
        402,
        {
            "error": "Insufficient credits",
            "code": "INSUFFICIENT_CREDITS",
            "message": "Insufficient credits to dispatch this batch. Top up to continue.",
            "creditsRequired": 10_000,
            "creditsAvailable": 200,
            "shortfall": 9_800,
            "topup": 99,
            "recoveryUrl": "/account/billing?recovery=insufficient_credits&source=fold_batch&topup=99",
            "upgradeUrl": "https://ligandai.com/pricing",
        },
    )
    assert isinstance(err, LigandAICreditError)
    assert err.required == 10_000
    assert err.available == 200
    assert err.shortfall == 9_800
    assert err.top_up_usd == 99
    assert err.recovery_url is not None
    assert "/account/billing" in err.recovery_url
    assert err.upgrade_url == "https://ligandai.com/pricing"
    # status + code preserved on the base class
    assert err.status_code == 402
    assert err.code == "INSUFFICIENT_CREDITS"


def test_error_from_response_402_legacy_shape_still_works() -> None:
    """Pre-patch /v1/folding/predict-batch sent only `required` + message.
    The SDK derives shortfall from required+available when the server omits it.
    """
    err = error_from_response(
        402,
        {
            "error": "Insufficient credits",
            "required": 5_000,
            "available": 100,
            "message": "Purchase more credits at ligandai.com/credits",
        },
    )
    assert isinstance(err, LigandAICreditError)
    assert err.shortfall == 4_900  # derived
    assert err.recovery_url is None
    assert err.top_up_usd is None


def test_error_from_response_with_retry_after() -> None:
    err = error_from_response(
        429, {"message": "rate limited"}, retry_after=15.0
    )
    assert isinstance(err, LigandAIRateLimitError)
    assert err.retry_after == 15.0


def test_error_from_response_unknown_status_falls_back() -> None:
    err = error_from_response(418, {"message": "I'm a teapot"})
    assert isinstance(err, LigandAIError)
    assert not isinstance(err, LigandAITierError)
    assert err.status_code == 418


def test_error_from_response_no_payload() -> None:
    err = error_from_response(500, None)
    assert isinstance(err, LigandAIServerError)
    assert "500" in err.message


def test_error_repr_formatting() -> None:
    err = LigandAIError("boom", code="E001", status_code=500, request_id="req_xyz")
    rep = repr(err)
    assert "LigandAIError" in rep
    assert "code='E001'" in rep
    assert "status_code=500" in rep
    assert "request_id='req_xyz'" in rep
