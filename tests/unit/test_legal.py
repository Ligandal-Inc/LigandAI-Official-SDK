# Copyright © 2026 Ligandal, Inc. All rights reserved.
"""Legal-acceptance 403 mapping + SDK-side browser-redirect/resume flow.

Covers dre-0audd.8 / docs/clickwrap_legal_acceptance.md §3.6 (Contract C):

- Both server 403 shapes (Contract C and the legacy `terms_not_accepted`/
  `E014` payload) map to `LigandAILegalAcceptanceRequired` with
  `.acceptance_url` populated.
- `Legal.resolve_interactively()` / `AsyncLegal.resolve_interactively()`
  never hang a headless/CI process — they raise immediately.
- A successful poll retries the caller's original request exactly once.

No mock/synthetic DATA — only HTTP transport mocking via `pytest-httpx`,
which is the house pattern for this suite (see tests/unit/test_jobs.py,
tests/unit/test_errors.py).
"""

from __future__ import annotations

import time

import pytest
from pytest_httpx import HTTPXMock

from ligandai import AsyncLigandAI, LigandAI
from ligandai.errors import (
    LigandAIForbidden,
    LigandAILegalAcceptanceRequired,
    error_from_response,
)

BASE = "http://api.ligandai.test"
STATUS_URL = f"{BASE}/api/legal/status"


# ─── error_from_response: both server shapes ──────────────────────────────


def test_error_from_response_legal_contract_c_shape() -> None:
    """New Contract C shape: explicit code + acceptanceUrl + needs* flags."""
    err = error_from_response(
        403,
        {
            "error": "Legal acceptance required",
            "code": "LEGAL_ACCEPTANCE_REQUIRED",
            "needsToS": True,
            "needsEULA": False,
            "acceptanceUrl": "https://ligandai.com/legal/accept?return_to=x&channel=sdk_redirect",
            "message": "You must accept the Terms of Service to continue.",
        },
    )
    assert isinstance(err, LigandAILegalAcceptanceRequired)
    assert isinstance(err, LigandAIForbidden)  # existing except-Forbidden handlers keep working
    assert err.acceptance_url == (
        "https://ligandai.com/legal/accept?return_to=x&channel=sdk_redirect"
    )
    assert err.needs_tos is True
    assert err.needs_eula is False
    assert err.status_code == 403
    assert "https://ligandai.com/legal/accept" in str(err)


def test_error_from_response_legal_legacy_shape() -> None:
    """Legacy shape from server/middleware/api-key-validator.ts:281 and
    server/enterprise-api-routes.ts:1343,1451 — no needs* flags, `action_url`
    instead of `acceptanceUrl`. Both documents are assumed outstanding since
    the legacy gate never distinguished which one was missing."""
    err = error_from_response(
        403,
        {
            "error": "terms_not_accepted",
            "code": "E014",
            "action_url": "https://ligandai.com/terms",
        },
    )
    assert isinstance(err, LigandAILegalAcceptanceRequired)
    assert err.acceptance_url == "https://ligandai.com/terms"
    assert err.needs_tos is True
    assert err.needs_eula is True
    assert "https://ligandai.com/terms" in str(err)


def test_error_from_response_legal_code_e014_without_error_string() -> None:
    """`code == 'E014'` alone (no matching `error` string) still routes here."""
    err = error_from_response(403, {"code": "E014", "message": "nope"})
    assert isinstance(err, LigandAILegalAcceptanceRequired)


def test_error_from_response_generic_403_is_not_legal() -> None:
    """Unrelated 403s (pilot allowlist, ownership, etc.) must NOT be
    reclassified as legal-acceptance errors — regression guard for the
    detection added ahead of the generic Forbidden fallthrough."""
    err = error_from_response(403, {"message": "x", "code": "E001"})
    assert type(err) is LigandAIForbidden
    assert not isinstance(err, LigandAILegalAcceptanceRequired)

    err2 = error_from_response(
        403, {"error": "AutoResearch is pilot-only", "error_code": "pilot_restricted"}
    )
    assert type(err2) is LigandAIForbidden
    assert not isinstance(err2, LigandAILegalAcceptanceRequired)


# ─── full HTTP round-trip through the transport ───────────────────────────


@pytest.fixture
def client() -> LigandAI:
    c = LigandAI(api_key="lgai_pro_testkey0123456789ABCDEF", base_url=BASE, max_retries=1)
    yield c
    c.close()


def test_transport_maps_contract_c_403(httpx_mock: HTTPXMock, client: LigandAI) -> None:
    httpx_mock.add_response(
        method="GET",
        url=f"{BASE}/api/protected/example",
        status_code=403,
        json={
            "error": "Legal acceptance required",
            "code": "LEGAL_ACCEPTANCE_REQUIRED",
            "needsToS": True,
            "needsEULA": True,
            "acceptanceUrl": "https://ligandai.com/legal/accept?return_to=x&channel=sdk_redirect",
            "message": "You must accept the Terms of Service and End User License Agreement.",
        },
    )
    with pytest.raises(LigandAILegalAcceptanceRequired) as excinfo:
        client.transport.request("GET", "/api/protected/example")
    assert excinfo.value.acceptance_url == (
        "https://ligandai.com/legal/accept?return_to=x&channel=sdk_redirect"
    )


def test_transport_maps_legacy_403(httpx_mock: HTTPXMock, client: LigandAI) -> None:
    httpx_mock.add_response(
        method="GET",
        url=f"{BASE}/api/protected/example",
        status_code=403,
        json={
            "error": "terms_not_accepted",
            "code": "E014",
            "action_url": "https://ligandai.com/terms",
        },
    )
    with pytest.raises(LigandAILegalAcceptanceRequired) as excinfo:
        client.transport.request("GET", "/api/protected/example")
    assert excinfo.value.acceptance_url == "https://ligandai.com/terms"


# ─── resolve_interactively: headless/CI must never hang ───────────────────


def test_resolve_interactively_headless_ci_raises_immediately(
    monkeypatch: pytest.MonkeyPatch, httpx_mock: HTTPXMock, client: LigandAI
) -> None:
    """`CI=1` alone must be sufficient to refuse the browser/poll flow."""
    monkeypatch.setenv("CI", "1")
    # Build the error the way production code actually would — via
    # error_from_response() off a real 403 payload — so the "message
    # contains the URL" assertion below tests the real integration, not a
    # hand-typed message.
    err = error_from_response(
        403,
        {
            "error": "Legal acceptance required",
            "code": "LEGAL_ACCEPTANCE_REQUIRED",
            "needsToS": True,
            "needsEULA": True,
            "acceptanceUrl": "https://ligandai.com/legal/accept?return_to=x&channel=sdk_redirect",
            "message": "You must accept the Terms of Service and End User License Agreement.",
        },
    )
    t0 = time.monotonic()
    with pytest.raises(LigandAILegalAcceptanceRequired) as excinfo:
        client.legal.resolve_interactively(err, auto_open_browser=True, timeout=30.0)
    elapsed = time.monotonic() - t0

    assert excinfo.value is err  # same object — acceptance_url still attached
    assert "https://ligandai.com/legal/accept" in str(excinfo.value)
    # Never hangs: must return in well under the 30s timeout given above.
    assert elapsed < 2.0
    # No HTTP call should have been attempted at all (no /api/legal/status
    # poll, no request of any kind) — nothing registered on httpx_mock, so
    # any stray call would have raised from inside resolve_interactively
    # instead of quietly succeeding.
    assert httpx_mock.get_requests() == []


def test_resolve_interactively_no_tty_raises_immediately(
    monkeypatch: pytest.MonkeyPatch, httpx_mock: HTTPXMock, client: LigandAI
) -> None:
    """Non-TTY stdin (piped/redirected), independent of CI env vars."""
    monkeypatch.delenv("CI", raising=False)
    monkeypatch.delenv("GITHUB_ACTIONS", raising=False)
    monkeypatch.setattr("sys.stdin.isatty", lambda: False)
    err = LigandAILegalAcceptanceRequired(
        "Legal acceptance required.",
        acceptance_url="https://ligandai.com/legal/accept?return_to=x",
        needs_tos=True,
        needs_eula=True,
    )
    with pytest.raises(LigandAILegalAcceptanceRequired):
        client.legal.resolve_interactively(err, auto_open_browser=True, timeout=30.0)
    assert httpx_mock.get_requests() == []


def test_resolve_interactively_browser_open_returns_false_raises_immediately(
    monkeypatch: pytest.MonkeyPatch, httpx_mock: HTTPXMock, client: LigandAI
) -> None:
    """`webbrowser.open` returning False (no browser found) is treated the
    same as headless — fails fast rather than polling forever."""
    monkeypatch.setattr(
        "ligandai.resources.legal.is_noninteractive_environment", lambda: False
    )
    monkeypatch.setattr("ligandai.resources.legal.webbrowser.open", lambda url: False)
    err = LigandAILegalAcceptanceRequired(
        "Legal acceptance required.",
        acceptance_url="https://ligandai.com/legal/accept?return_to=x",
        needs_tos=True,
        needs_eula=True,
    )
    with pytest.raises(LigandAILegalAcceptanceRequired) as excinfo:
        client.legal.resolve_interactively(err, auto_open_browser=True, timeout=30.0)
    assert excinfo.value is err
    assert httpx_mock.get_requests() == []


def test_resolve_interactively_auto_open_browser_defaults_off(
    monkeypatch: pytest.MonkeyPatch, httpx_mock: HTTPXMock, client: LigandAI
) -> None:
    """auto_open_browser is opt-in: with it left False, webbrowser.open must
    never be called, even in a fully interactive environment."""
    monkeypatch.setattr(
        "ligandai.resources.legal.is_noninteractive_environment", lambda: False
    )
    opened = {"called": False}

    def _fake_open(url: str) -> bool:
        opened["called"] = True
        return True

    monkeypatch.setattr("ligandai.resources.legal.webbrowser.open", _fake_open)
    httpx_mock.add_response(
        method="GET", url=STATUS_URL, json={"tosAccepted": True, "eulaAccepted": True}
    )
    err = LigandAILegalAcceptanceRequired(
        "Legal acceptance required.",
        acceptance_url="https://ligandai.com/legal/accept?return_to=x",
        needs_tos=True,
        needs_eula=True,
    )
    result = client.legal.resolve_interactively(err, poll_interval=0.01, timeout=5.0)
    assert result is True
    assert opened["called"] is False


# ─── resolve_interactively: successful poll retries the request once ─────


def test_resolve_interactively_successful_poll_retries_once(
    monkeypatch: pytest.MonkeyPatch, httpx_mock: HTTPXMock, client: LigandAI
) -> None:
    monkeypatch.setattr(
        "ligandai.resources.legal.is_noninteractive_environment", lambda: False
    )
    httpx_mock.add_response(
        method="GET", url=STATUS_URL, json={"tosAccepted": False, "eulaAccepted": False}
    )
    httpx_mock.add_response(
        method="GET", url=STATUS_URL, json={"tosAccepted": True, "eulaAccepted": True}
    )
    err = LigandAILegalAcceptanceRequired(
        "Legal acceptance required.",
        acceptance_url="https://ligandai.com/legal/accept?return_to=x",
        needs_tos=True,
        needs_eula=True,
    )
    calls = {"n": 0}

    def _retry() -> str:
        calls["n"] += 1
        return "resumed-ok"

    result = client.legal.resolve_interactively(
        err, retry=_retry, auto_open_browser=False, poll_interval=0.01, timeout=5.0
    )
    assert result == "resumed-ok"
    assert calls["n"] == 1
    # Exactly the two status polls registered above — proves the poll loop
    # stopped as soon as acceptance was satisfied rather than continuing.
    status_calls = [r for r in httpx_mock.get_requests() if str(r.url) == STATUS_URL]
    assert len(status_calls) == 2


def test_resolve_interactively_without_retry_returns_true(
    monkeypatch: pytest.MonkeyPatch, httpx_mock: HTTPXMock, client: LigandAI
) -> None:
    """No `retry` callable: just confirms acceptance and returns True."""
    monkeypatch.setattr(
        "ligandai.resources.legal.is_noninteractive_environment", lambda: False
    )
    httpx_mock.add_response(
        method="GET", url=STATUS_URL, json={"tosAccepted": True, "eulaAccepted": True}
    )
    err = LigandAILegalAcceptanceRequired(
        "Legal acceptance required.",
        acceptance_url="https://ligandai.com/legal/accept?return_to=x",
        needs_tos=True,
        needs_eula=True,
    )
    assert client.legal.resolve_interactively(err, poll_interval=0.01, timeout=5.0) is True


def test_resolve_interactively_timeout_raises_and_never_retries(
    monkeypatch: pytest.MonkeyPatch, httpx_mock: HTTPXMock, client: LigandAI
) -> None:
    monkeypatch.setattr(
        "ligandai.resources.legal.is_noninteractive_environment", lambda: False
    )
    httpx_mock.add_response(
        method="GET",
        url=STATUS_URL,
        json={"tosAccepted": False, "eulaAccepted": False},
        is_reusable=True,
    )
    err = LigandAILegalAcceptanceRequired(
        "Legal acceptance required.",
        acceptance_url="https://ligandai.com/legal/accept?return_to=x",
        needs_tos=True,
        needs_eula=True,
    )
    calls = {"n": 0}

    def _retry() -> str:
        calls["n"] += 1
        return "should-not-happen"

    t0 = time.monotonic()
    with pytest.raises(LigandAILegalAcceptanceRequired) as excinfo:
        client.legal.resolve_interactively(
            err, retry=_retry, poll_interval=0.02, timeout=0.1
        )
    elapsed = time.monotonic() - t0

    assert excinfo.value is err
    assert calls["n"] == 0
    assert elapsed < 2.0  # bounded by the 0.1s timeout, not left to hang


# ─── async sibling parity ──────────────────────────────────────────────────


@pytest.fixture
def async_client() -> AsyncLigandAI:
    c = AsyncLigandAI(api_key="lgai_pro_testkey0123456789ABCDEF", base_url=BASE, max_retries=1)
    yield c


@pytest.mark.asyncio
async def test_async_resolve_interactively_headless_raises_immediately(
    monkeypatch: pytest.MonkeyPatch, httpx_mock: HTTPXMock, async_client: AsyncLigandAI
) -> None:
    monkeypatch.setenv("CI", "1")
    err = LigandAILegalAcceptanceRequired(
        "Legal acceptance required.",
        acceptance_url="https://ligandai.com/legal/accept?return_to=x",
        needs_tos=True,
        needs_eula=True,
    )
    with pytest.raises(LigandAILegalAcceptanceRequired) as excinfo:
        await async_client.legal.resolve_interactively(
            err, auto_open_browser=True, timeout=30.0
        )
    assert excinfo.value is err
    assert httpx_mock.get_requests() == []
    await async_client.close()


@pytest.mark.asyncio
async def test_async_resolve_interactively_successful_poll_retries_once(
    monkeypatch: pytest.MonkeyPatch, httpx_mock: HTTPXMock, async_client: AsyncLigandAI
) -> None:
    monkeypatch.setattr(
        "ligandai.resources.legal.is_noninteractive_environment", lambda: False
    )
    httpx_mock.add_response(
        method="GET", url=STATUS_URL, json={"tosAccepted": False, "eulaAccepted": False}
    )
    httpx_mock.add_response(
        method="GET", url=STATUS_URL, json={"tosAccepted": True, "eulaAccepted": True}
    )
    err = LigandAILegalAcceptanceRequired(
        "Legal acceptance required.",
        acceptance_url="https://ligandai.com/legal/accept?return_to=x",
        needs_tos=True,
        needs_eula=True,
    )
    calls = {"n": 0}

    async def _retry() -> str:
        calls["n"] += 1
        return "resumed-ok"

    result = await async_client.legal.resolve_interactively(
        err, retry=_retry, poll_interval=0.01, timeout=5.0
    )
    assert result == "resumed-ok"
    assert calls["n"] == 1
    await async_client.close()
