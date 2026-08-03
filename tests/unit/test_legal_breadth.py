# Copyright © 2026 Ligandal, Inc. All rights reserved.
"""Breadth of the legal-acceptance gate: does *every* authenticated call surface it?

`resolve_interactively()` was already built and verified (tests/unit/test_legal.py).
What this file verifies is the question that actually decides whether the epic
works for real users: **does the redirect reach the user no matter which SDK call
they made?**

Two halves:

1. **Transport coverage.** Every authenticated request in the SDK funnels through
   the single error mapper `_http._raise_for_status()` → `errors.error_from_response()`.
   Exercised here across every distinct request shape: sync/async `request()`,
   sync/async `stream_lines()`, non-JSON downloads (`expect_json=False`), multipart
   file upload, and the retry path.

2. **Swallow guards.** Regression tests for the sites that used to catch the
   mapper's typed exception and destroy it — turning "accept the terms at <url>"
   into `return False`, a `None` result, or a bogus timeout.

Plus the constraint that matters most commercially: **an already-accepted user
must be completely unaffected.** A 200 response must never touch the legal
machinery at all.

No mock/synthetic DATA — only HTTP-response fixtures via `pytest-httpx`, the
house pattern for this suite.
"""

from __future__ import annotations

import json as _json

import pytest
from pytest_httpx import HTTPXMock

from ligandai import AsyncLigandAI, LigandAI
from ligandai.errors import LigandAILegalAcceptanceRequired, WalletEmpty
from ligandai.key_wallet import KeyWallet

BASE = "http://api.ligandai.test"
ACCEPT_URL = "https://ligandai.com/legal/accept?return_to=x&channel=sdk_redirect"

# The Contract C 403 body the server emits. `message` carries the absolute URL
# in plain text — permanent requirement, because 0.8.3-pinned clients, curl and
# bare `requests` only ever surface the error string.
LEGAL_403 = {
    "error": "Legal acceptance required",
    "code": "LEGAL_ACCEPTANCE_REQUIRED",
    "needsToS": True,
    "needsEULA": True,
    "acceptanceUrl": ACCEPT_URL,
    "message": f"You must accept the Terms of Service. Accept at: {ACCEPT_URL}",
}


@pytest.fixture
def client() -> LigandAI:
    c = LigandAI(
        api_key="lgai_pro_testkey0123456789ABCDEF",
        base_url=BASE,
        max_retries=1,
        check_entitlement=False,
    )
    yield c
    c.close()


@pytest.fixture
def async_client() -> AsyncLigandAI:
    return AsyncLigandAI(
        api_key="lgai_pro_testkey0123456789ABCDEF",
        base_url=BASE,
        max_retries=1,
        check_entitlement=False,
    )


def _assert_actionable(err: LigandAILegalAcceptanceRequired) -> None:
    """The three things a user needs, on every path."""
    assert err.acceptance_url == ACCEPT_URL
    assert err.needs_tos is True and err.needs_eula is True
    # The URL is in the string too — this is what `print(e)` / a traceback / an
    # old pinned client shows, and it must appear exactly once.
    assert str(err).count(ACCEPT_URL) == 1


# ─── 1. transport coverage: every request shape hits the error mapper ──────


def test_sync_request_json_surfaces_legal(httpx_mock: HTTPXMock, client: LigandAI) -> None:
    httpx_mock.add_response(
        method="GET", url=f"{BASE}/api/anything", status_code=403, json=LEGAL_403
    )
    with pytest.raises(LigandAILegalAcceptanceRequired) as e:
        client.transport.request("GET", "/api/anything")
    _assert_actionable(e.value)


def test_sync_request_non_json_download_surfaces_legal(
    httpx_mock: HTTPXMock, client: LigandAI
) -> None:
    """`expect_json=False` is the download path (PDB/CIF/PAE bytes).

    It returns the raw response instead of a parsed body, so it would be an easy
    place to skip error mapping — `_raise_for_status` runs *before* that branch.
    """
    httpx_mock.add_response(
        method="GET", url=f"{BASE}/api/download", status_code=403, json=LEGAL_403
    )
    with pytest.raises(LigandAILegalAcceptanceRequired) as e:
        client.transport.request("GET", "/api/download", expect_json=False)
    _assert_actionable(e.value)


def test_sync_request_file_upload_surfaces_legal(
    httpx_mock: HTTPXMock, client: LigandAI
) -> None:
    """Multipart upload (`files=`) takes the same code path as a JSON POST."""
    httpx_mock.add_response(
        method="POST", url=f"{BASE}/api/upload", status_code=403, json=LEGAL_403
    )
    with pytest.raises(LigandAILegalAcceptanceRequired) as e:
        client.transport.request(
            "POST", "/api/upload", files={"file": ("t.pdb", b"ATOM", "text/plain")}
        )
    _assert_actionable(e.value)


def test_sync_stream_lines_surfaces_legal(httpx_mock: HTTPXMock, client: LigandAI) -> None:
    """SSE streaming has its own `_raise_for_status` call before iterating."""
    httpx_mock.add_response(
        method="GET", url=f"{BASE}/api/stream", status_code=403, json=LEGAL_403
    )
    with pytest.raises(LigandAILegalAcceptanceRequired) as e:
        list(client.transport.stream_lines("GET", "/api/stream"))
    _assert_actionable(e.value)


def test_retry_path_surfaces_legal(httpx_mock: HTTPXMock) -> None:
    """A retryable 429 followed by the legal 403 still yields the typed error.

    Guards against the retry wrapper (tenacity) masking the final exception.
    """
    c = LigandAI(
        api_key="lgai_pro_testkey0123456789ABCDEF",
        base_url=BASE,
        max_retries=3,
        check_entitlement=False,
    )
    httpx_mock.add_response(
        method="GET",
        url=f"{BASE}/api/flaky",
        status_code=429,
        json={"message": "slow down"},
        headers={"retry-after": "0"},
    )
    httpx_mock.add_response(
        method="GET", url=f"{BASE}/api/flaky", status_code=403, json=LEGAL_403
    )
    with pytest.raises(LigandAILegalAcceptanceRequired) as e:
        c.transport.request("GET", "/api/flaky")
    _assert_actionable(e.value)
    c.close()


@pytest.mark.asyncio
async def test_async_request_surfaces_legal(
    httpx_mock: HTTPXMock, async_client: AsyncLigandAI
) -> None:
    httpx_mock.add_response(
        method="GET", url=f"{BASE}/api/anything", status_code=403, json=LEGAL_403
    )
    with pytest.raises(LigandAILegalAcceptanceRequired) as e:
        await async_client.transport.request("GET", "/api/anything")
    _assert_actionable(e.value)
    await async_client.close()


@pytest.mark.asyncio
async def test_async_stream_lines_surfaces_legal(
    httpx_mock: HTTPXMock, async_client: AsyncLigandAI
) -> None:
    httpx_mock.add_response(
        method="GET", url=f"{BASE}/api/stream", status_code=403, json=LEGAL_403
    )
    with pytest.raises(LigandAILegalAcceptanceRequired) as e:
        async for _ in async_client.transport.stream_lines("GET", "/api/stream"):
            pass
    _assert_actionable(e.value)
    await async_client.close()


# ─── 2. swallow guards: the typed error must survive every handler ─────────


def test_jobs_cancel_does_not_swallow_legal(httpx_mock: HTTPXMock, client: LigandAI) -> None:
    """`jobs.cancel()` returns a bool. It used to return False on a legal 403 —
    the user is told cancellation was refused while a paid GPU job keeps running."""
    httpx_mock.add_response(
        method="POST", url=f"{BASE}/api/jobs/job_123/cancel", status_code=403, json=LEGAL_403
    )
    with pytest.raises(LigandAILegalAcceptanceRequired) as e:
        client.jobs.cancel("job_123")
    _assert_actionable(e.value)


def test_jobs_cancel_still_returns_false_on_ordinary_failure(
    httpx_mock: HTTPXMock, client: LigandAI
) -> None:
    """The bool contract is preserved for every non-legal error."""
    httpx_mock.add_response(
        method="POST",
        url=f"{BASE}/api/jobs/job_123/cancel",
        status_code=409,
        json={"message": "already finished"},
    )
    assert client.jobs.cancel("job_123") is False


@pytest.mark.parametrize(
    ("resource", "method", "http_method", "path", "arg"),
    [
        ("memory", "delete", "DELETE", "/api/episodic-memory/7", 7),
        ("programs", "archive", "DELETE", "/api/ptf/programs/7", 7),
        ("proteins", "delete_variant", "DELETE", "/api/protein-variants/7", 7),
        ("discovery", "delete_dataset", "DELETE", "/api/transcriptomics/datasets/7", 7),
    ],
)
def test_boolean_delete_helpers_do_not_swallow_legal(
    httpx_mock: HTTPXMock,
    client: LigandAI,
    resource: str,
    method: str,
    http_method: str,
    path: str,
    arg: int,
) -> None:
    """Every `-> bool` delete/archive helper used to convert a legal 403 to False."""
    httpx_mock.add_response(
        method=http_method, url=f"{BASE}{path}", status_code=403, json=LEGAL_403
    )
    fn = getattr(getattr(client, resource), method)
    with pytest.raises(LigandAILegalAcceptanceRequired) as e:
        fn(arg)
    _assert_actionable(e.value)


def test_boolean_delete_still_returns_false_on_ordinary_failure(
    httpx_mock: HTTPXMock, client: LigandAI
) -> None:
    httpx_mock.add_response(
        method="DELETE",
        url=f"{BASE}/api/episodic-memory/7",
        status_code=404,
        json={"message": "gone"},
    )
    assert client.memory.delete(7) is False


def test_wallet_refresh_does_not_rewrap_legal_as_wallet_empty(
    httpx_mock: HTTPXMock, client: LigandAI
) -> None:
    """The worst swallow: a legal 403 became `WalletEmpty`, whose remedy
    (`client.mint_wallet()`) 403s identically. Class, `.acceptance_url` and the
    `except LigandAILegalAcceptanceRequired` handler were all destroyed."""
    httpx_mock.add_response(
        method="POST",
        url=f"{BASE}/api/auth/scoped-key/refresh",
        status_code=403,
        json=LEGAL_403,
    )
    wallet = KeyWallet(
        {"version": 1, "wallet": [], "refresh_token": "rt_x", "scope": "fold"}
    )
    with pytest.raises(LigandAILegalAcceptanceRequired) as e:
        wallet.refresh(client)
    _assert_actionable(e.value)


def test_wallet_refresh_still_wraps_ordinary_failures(
    httpx_mock: HTTPXMock, client: LigandAI
) -> None:
    """Non-legal failures keep the existing WalletEmpty contract."""
    httpx_mock.add_response(
        method="POST",
        url=f"{BASE}/api/auth/scoped-key/refresh",
        status_code=500,
        json={"message": "boom"},
        is_reusable=True,
    )
    wallet = KeyWallet(
        {"version": 1, "wallet": [], "refresh_token": "rt_x", "scope": "fold"}
    )
    with pytest.raises(WalletEmpty):
        wallet.refresh(client)


def test_species_entitlement_does_not_silently_downgrade_on_legal(
    httpx_mock: HTTPXMock,
) -> None:
    """Entitlement fails CLOSED on error — correct for an outage, wrong for the
    legal gate. It runs on the primary generate/fold path, so swallowing it
    silently downgrades the user's organism instead of asking them to accept."""
    c = LigandAI(
        api_key="lgai_pro_testkey0123456789ABCDEF", base_url=BASE, max_retries=1
    )
    httpx_mock.add_response(
        method="GET",
        url=f"{BASE}/api/cross-species/entitlement",
        status_code=403,
        json=LEGAL_403,
    )
    with pytest.raises(LigandAILegalAcceptanceRequired) as e:
        c.entitlement()
    _assert_actionable(e.value)
    c.close()


def test_species_entitlement_still_fails_closed_on_outage(httpx_mock: HTTPXMock) -> None:
    """A 500 must still fail closed rather than granting a species."""
    c = LigandAI(
        api_key="lgai_pro_testkey0123456789ABCDEF", base_url=BASE, max_retries=1
    )
    httpx_mock.add_response(
        method="GET",
        url=f"{BASE}/api/cross-species/entitlement",
        status_code=500,
        json={"message": "down"},
        is_reusable=True,
    )
    assert c.entitlement().entitled is False
    c.close()


# ─── 3. an already-accepted user must be COMPLETELY unaffected ─────────────


def test_success_path_never_touches_legal_machinery(
    monkeypatch: pytest.MonkeyPatch, httpx_mock: HTTPXMock, client: LigandAI
) -> None:
    """Real customer constraint: an academia-tier account mid-campaign, ToS+EULA
    already accepted, 800 folds in flight. Every call returns 200. The SDK must
    not prompt, redirect, poll, block, or add a single byte of latency.

    Enforced by booby-trapping every piece of the legal machinery: if a 200
    response touches ANY of it, the test fails loudly.
    """
    tripped: list[str] = []

    def _trip(name: str):
        def _fn(*_a: object, **_kw: object) -> object:
            tripped.append(name)
            raise AssertionError(f"200 path touched legal machinery: {name}")

        return _fn

    monkeypatch.setattr(
        "ligandai.resources.legal.is_noninteractive_environment",
        _trip("is_noninteractive_environment"),
    )
    monkeypatch.setattr("ligandai.resources.legal.webbrowser.open", _trip("webbrowser.open"))

    httpx_mock.add_response(
        method="GET", url=f"{BASE}/api/anything", status_code=200, json={"ok": True}
    )
    assert client.transport.request("GET", "/api/anything") == {"ok": True}

    # Exactly one request: no status poll, no acceptance POST, no extra round-trip.
    assert len(httpx_mock.get_requests()) == 1
    assert tripped == []


def test_success_path_makes_no_acceptance_write(
    httpx_mock: HTTPXMock, client: LigandAI
) -> None:
    """⛔ The SDK must NEVER POST an acceptance the user did not click.

    Every one of the unasked users has to click for themselves. Assert that a
    normal successful call writes nothing to any acceptance endpoint.
    """
    httpx_mock.add_response(
        method="GET", url=f"{BASE}/api/anything", status_code=200, json={"ok": True}
    )
    client.transport.request("GET", "/api/anything")

    writes = [
        r
        for r in httpx_mock.get_requests()
        if r.method in {"POST", "PUT", "PATCH"}
        and ("accept" in str(r.url) or "legal" in str(r.url))
    ]
    assert writes == []


def test_accept_never_asserts_a_version_the_user_did_not_supply(
    httpx_mock: HTTPXMock, client: LigandAI
) -> None:
    """The server ignores client-supplied version strings and resolves the
    current version itself. The SDK must not invent one — `accept_current()`
    reads them from the server rather than fabricating a value."""
    httpx_mock.add_response(
        method="GET",
        url=f"{BASE}/api/legal/versions",
        json={"tos": {"version": "1.5"}, "eula": {"version": "2.1"}},
    )
    httpx_mock.add_response(
        method="POST", url=f"{BASE}/api/user/accept-tos", json={"success": True}
    )
    client.legal.accept_current()

    posts = [r for r in httpx_mock.get_requests() if r.method == "POST"]
    assert len(posts) == 1
    body = _json.loads(posts[0].content)
    # Values came from the server's own /versions response, not from the SDK.
    assert body == {"tos_version": "1.5", "eula_version": "2.1"}


def test_accept_refuses_when_server_has_no_current_version(
    httpx_mock: HTTPXMock, client: LigandAI
) -> None:
    """No version configured → refuse, rather than synthesising one."""
    httpx_mock.add_response(
        method="GET", url=f"{BASE}/api/legal/versions", json={"tos": None, "eula": None}
    )
    with pytest.raises(ValueError, match="cannot accept"):
        client.legal.accept_current()
    assert [r for r in httpx_mock.get_requests() if r.method == "POST"] == []
