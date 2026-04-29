# Copyright © 2025 Ligandal, Inc. All rights reserved.
"""Error hierarchy + response → exception mapping."""

from __future__ import annotations

import pytest

from ligandai.errors import (
    LigandAIAuthError,
    LigandAICreditError,
    LigandAIError,
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


def test_rate_limit_error_has_retry_after() -> None:
    err = LigandAIRateLimitError("slow down", retry_after=30.0)
    assert err.retry_after == 30.0


@pytest.mark.parametrize(
    "status,expected_class",
    [
        (401, LigandAIAuthError),
        (402, LigandAICreditError),
        (403, LigandAITierError),
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
    assert err.status_code == status


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
