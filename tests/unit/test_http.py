# Copyright © 2026 Ligandal, Inc. All rights reserved.
"""HTTP transport — auth headers, retry, rate-limit parsing."""

from __future__ import annotations

import pytest
from pytest_httpx import HTTPXMock

from ligandai._http import HTTPTransport, parse_sse_data
from ligandai.errors import LigandAIRateLimitError, LigandAIServerError

BASE = "http://api.ligandai.test"


def test_auth_header_set() -> None:
    t = HTTPTransport(api_key="lgai_pro_secret", base_url=BASE)
    assert t._client.headers["Authorization"] == "Bearer lgai_pro_secret"
    t.close()


def test_no_auth_header_when_anonymous() -> None:
    t = HTTPTransport(api_key=None, base_url=BASE)
    assert "Authorization" not in t._client.headers
    t.close()


def test_user_agent_includes_version() -> None:
    from ligandai import __version__
    t = HTTPTransport(api_key="lgai_pro_x", base_url=BASE)
    ua = t._client.headers["User-Agent"]
    assert f"ligandai-python/{__version__}" in ua
    t.close()


def test_request_strips_none_params(httpx_mock: HTTPXMock) -> None:
    httpx_mock.add_response(url=f"{BASE}/api/test?keep=1", json={"ok": True})
    t = HTTPTransport(api_key="lgai_pro_x", base_url=BASE)
    t.request("GET", "/api/test", params={"keep": 1, "drop": None})
    t.close()


def test_retry_on_500(httpx_mock: HTTPXMock) -> None:
    httpx_mock.add_response(url=f"{BASE}/api/test", status_code=500, json={})
    httpx_mock.add_response(url=f"{BASE}/api/test", status_code=500, json={})
    httpx_mock.add_response(url=f"{BASE}/api/test", json={"ok": True})
    t = HTTPTransport(api_key="lgai_pro_x", base_url=BASE, max_retries=5)
    res = t.request("GET", "/api/test")
    assert res == {"ok": True}
    t.close()


def test_retry_exhausted_raises(httpx_mock: HTTPXMock) -> None:
    for _ in range(3):
        httpx_mock.add_response(url=f"{BASE}/api/test", status_code=503, json={})
    t = HTTPTransport(api_key="lgai_pro_x", base_url=BASE, max_retries=3)
    with pytest.raises(LigandAIServerError):
        t.request("GET", "/api/test")
    t.close()


def test_429_retries_then_raises(httpx_mock: HTTPXMock) -> None:
    httpx_mock.add_response(
        url=f"{BASE}/api/test",
        status_code=429,
        headers={"Retry-After": "0"},
        json={"message": "rate limited"},
        is_reusable=True,
    )
    t = HTTPTransport(api_key="lgai_pro_x", base_url=BASE, max_retries=2)
    with pytest.raises(LigandAIRateLimitError):
        t.request("GET", "/api/test")
    t.close()


def test_parse_sse_data_valid() -> None:
    assert parse_sse_data('data: {"foo": "bar"}') == {"foo": "bar"}


def test_parse_sse_data_event_line_returns_none() -> None:
    assert parse_sse_data("event: progress") is None


def test_parse_sse_data_done_returns_none() -> None:
    assert parse_sse_data("data: [DONE]") is None


def test_parse_sse_data_invalid_json_returns_none() -> None:
    assert parse_sse_data("data: not json") is None
