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

import sys
import time
from typing import Any

import pytest
from pytest_httpx import HTTPXMock

from ligandai import AsyncLigandAI, LigandAI
from ligandai._headless import is_noninteractive_environment
from ligandai.errors import (
    LigandAIForbidden,
    LigandAILegalAcceptanceRequired,
    error_from_response,
)

BASE = "http://api.ligandai.test"
STATUS_URL = f"{BASE}/api/legal/status"


# ─── stream-state test hygiene ────────────────────────────────────────────


class _FakeStream:
    """Stream stand-in with a pinned ``isatty()``.

    Everything except ``isatty`` forwards to the real stream, so replacing
    ``sys.stdout`` with one of these cannot break pytest's capture machinery
    or any incidental ``write()``.
    """

    def __init__(self, tty: bool, wrapped: Any) -> None:
        self._tty = tty
        self._wrapped = wrapped

    def isatty(self) -> bool:
        return self._tty

    def __getattr__(self, name: str) -> Any:
        return getattr(self._wrapped, name)


def _pin_interactivity_inputs(
    monkeypatch: pytest.MonkeyPatch, *, stdin_tty: bool, stdout_tty: bool
) -> None:
    """Pin EVERY input to :func:`is_noninteractive_environment` explicitly.

    ⛔ Test-hygiene rule from ``docs/clickwrap_legal_acceptance.md`` §3.6: any
    test touching stream state must stub **both** streams, even though only
    stdin is consulted. Otherwise the verdict depends on how the *runner* was
    invoked — pytest swaps ``sys.stdout`` for a non-TTY object under default
    capture but not under ``pytest -s`` — rather than on the code under test.

    ``DISPLAY``/``WAYLAND_DISPLAY`` are pinned for the same reason, and this
    half matters more than it looks: on Linux the probe returns non-interactive
    whenever neither is set, which **dominates the TTY check entirely**. A test
    that leaves them unset passes vacuously on any headless box (CI, container,
    this repo's own dev host) and proves nothing about the stdin rule. Verified
    empirically: with ``DISPLAY`` unset, deleting the stdin stub from
    ``test_resolve_interactively_no_tty_raises_immediately`` left it green.
    """
    monkeypatch.delenv("CI", raising=False)
    monkeypatch.delenv("GITHUB_ACTIONS", raising=False)
    monkeypatch.setattr(sys, "stdin", _FakeStream(stdin_tty, sys.stdin))
    monkeypatch.setattr(sys, "stdout", _FakeStream(stdout_tty, sys.stdout))
    monkeypatch.setenv("DISPLAY", ":0")
    monkeypatch.delenv("WAYLAND_DISPLAY", raising=False)


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


# ─── idempotent acceptance-URL fold into `.message` ───────────────────────
#
# The server's human-readable `message` now carries the absolute acceptance
# URL itself, permanently — clients pinned to 0.8.3 and earlier, `curl` and
# bare `requests` only ever surface the error string. The client-side fold
# must therefore never double it.


def test_message_fold_is_idempotent_when_server_message_has_url() -> None:
    """Current server: URL already in `message` → appended exactly zero times."""
    url = "https://ligandai.com/legal/accept?return_to=x&channel=sdk_redirect"
    err = error_from_response(
        403,
        {
            "error": "Legal acceptance required",
            "code": "LEGAL_ACCEPTANCE_REQUIRED",
            "needsToS": True,
            "needsEULA": True,
            "acceptanceUrl": url,
            "message": f"You must accept the Terms of Service. Accept at: {url}",
        },
    )
    assert str(err).count(url) == 1
    assert "Accept at: Accept at:" not in str(err)
    assert err.acceptance_url == url


def test_message_fold_appends_when_legacy_server_message_lacks_url() -> None:
    """Legacy server: no URL in `message` → appended exactly once."""
    url = "https://ligandai.com/legal/accept?return_to=x&channel=sdk_redirect"
    err = error_from_response(
        403,
        {
            "error": "Legal acceptance required",
            "code": "LEGAL_ACCEPTANCE_REQUIRED",
            "acceptanceUrl": url,
            "message": "You must accept the Terms of Service to continue.",
        },
    )
    assert str(err).count(url) == 1
    assert str(err).endswith(f"Accept at: {url}")


def test_message_fold_uses_exact_containment_not_normalisation() -> None:
    """⛔ PARITY CONTRACT: the guard is EXACT substring containment, nothing more.

    Node's guard is `!message.includes(acceptanceUrl)`. The two mappers have
    been verified byte-identical by executing both over the same payload
    shapes, so Python must not be "smarter" here — a normalising comparison on
    one side only reintroduces the doubling on the other.

    This test pins the consequence explicitly so the behaviour is a decision on
    record rather than an accident: a near-miss spelling in the prose (here, an
    HTML-escaped `&`) does NOT count as present, and the canonical URL is
    appended. Both SDKs do this, identically.
    """
    url = "https://ligandai.com/legal/accept?return_to=x&channel=sdk_redirect"
    escaped = "https://ligandai.com/legal/accept?return_to=x&amp;channel=sdk_redirect"
    err = error_from_response(
        403,
        {
            "code": "LEGAL_ACCEPTANCE_REQUIRED",
            "acceptanceUrl": url,
            "message": f"Accept the terms at {escaped} to continue.",
        },
    )
    assert str(err).endswith(f"Accept at: {url}")


def test_message_fold_prose_url_is_never_parsed() -> None:
    """A DIFFERENT URL in the prose does not suppress the fold, and never wins.

    The structured field is always authoritative for `.acceptance_url`; the
    server's prose is never parsed for a URL. Both SDKs behave this way — the
    ambiguous case was pinned deliberately on both sides.
    """
    canonical = "https://ligandai.com/legal/accept?return_to=x&channel=sdk_redirect"
    err = error_from_response(
        403,
        {
            "code": "LEGAL_ACCEPTANCE_REQUIRED",
            "acceptanceUrl": canonical,
            "message": "Accept at https://ligandai.com/some/other/page first.",
        },
    )
    assert err.acceptance_url == canonical  # structured field wins, always
    assert str(err).endswith(f"Accept at: {canonical}")
    # The server's own prose is preserved, not stripped or rewritten.
    assert "https://ligandai.com/some/other/page" in str(err)


def test_message_fold_against_the_real_server_message() -> None:
    """The exact default message `buildLegalRequiredBody()` emits.

    Transcribed from W1's measurement of the shipped server function
    (monorepo commit 81cb845, `server/services/legal-gate.ts`) — obtained by
    executing it, not by reading the source. Server-side design: the URL is
    LAST with no trailing punctuation, so select-to-end-of-line in a terminal
    yields a working URL.

    This is the case that would double the URL if the fold were unconditional.
    """
    url = (
        "https://ligandai.com/legal/accept"
        "?return_to=%2Fapi%2Fv1%2Ffold&channel=sdk_redirect"
    )
    server_message = (
        "You must accept the Terms of Service and End User License Agreement "
        "to continue. Open this URL to review and accept: " + url
    )
    err = error_from_response(
        403,
        {
            "error": "Legal acceptance required",
            "code": "LEGAL_ACCEPTANCE_REQUIRED",
            "needsToS": True,
            "needsEULA": True,
            "acceptanceUrl": url,
            "message": server_message,
        },
    )
    # Not doubled, not rewritten, and still ends with the bare URL so
    # select-to-end-of-line keeps working through the SDK layer too.
    assert str(err) == server_message
    assert str(err).count(url) == 1
    assert str(err).endswith(url)
    assert err.acceptance_url == url


def test_message_fold_predicate_is_exactly_containment() -> None:
    """⛔ PARITY-CRITICAL: the predicate is EQUIVALENT to `url in message`.

    Not a superset, not a subset — equivalent. This is the executable statement
    of the cross-SDK rule, and it is what stops anyone from quietly adding a
    normalising step (the fragility W1 flagged): any such step makes this
    assertion fail.
    """
    from ligandai.errors import _message_already_surfaces_url

    url = "https://ligandai.com/legal/accept?return_to=%2Ffold&channel=sdk_redirect"
    messages = [
        f"prefix {url} suffix",
        url,
        f"{url}.",
        f"Open this URL to review and accept: {url}",
        f"line one\nOpen this URL: {url}",
        "no url here at all",
        url.replace("&", "&amp;"),  # near-miss: NOT containment, must append
        url.rstrip("t"),  # near-miss
        "",
    ]
    for message in messages:
        assert _message_already_surfaces_url(message, url) == (url in message), message


def test_message_fold_never_rewrites_server_wording() -> None:
    """The server's own prose survives verbatim; we only ever add a suffix."""
    url = "https://ligandai.com/legal/accept?return_to=x"
    server_message = "Your organization must accept the updated EULA before folding."
    err = error_from_response(
        403,
        {
            "code": "LEGAL_ACCEPTANCE_REQUIRED",
            "acceptanceUrl": url,
            "message": server_message,
        },
    )
    assert str(err).startswith(server_message)


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
    """Non-TTY stdin (piped/redirected), independent of CI env vars.

    stdout is deliberately pinned to **TTY** here: this doubles as the
    Node-parity regression test — non-TTY stdin + TTY stdout must still be
    non-interactive. If anyone reintroduces a stdout check or an
    "either stream is a TTY" rule, this test fails.
    """
    _pin_interactivity_inputs(monkeypatch, stdin_tty=False, stdout_tty=True)
    err = LigandAILegalAcceptanceRequired(
        "Legal acceptance required.",
        acceptance_url="https://ligandai.com/legal/accept?return_to=x",
        needs_tos=True,
        needs_eula=True,
    )
    t0 = time.monotonic()
    with pytest.raises(LigandAILegalAcceptanceRequired) as excinfo:
        client.legal.resolve_interactively(err, auto_open_browser=True, timeout=30.0)
    elapsed = time.monotonic() - t0
    assert excinfo.value is err  # same object, never reconstructed
    assert elapsed < 2.0  # fails closed immediately, never hangs
    assert httpx_mock.get_requests() == []


# ─── the interactivity probe itself: stdin only, never stdout ─────────────


def test_headless_probe_reads_stdin_not_stdout(monkeypatch: pytest.MonkeyPatch) -> None:
    """stdin is the signal; stdout is never consulted.

    The decided rule (``docs/clickwrap_legal_acceptance.md`` §3.6, "Interactivity
    probe — decided, not inherited"): **stdin only**. Python is the reference
    implementation and Node was changed to match, so this asserts the contract
    both SDKs are held to.

    Both directions are checked. The second case is the one that catches a
    regression to an "either stream" rule — TTY stdin with non-TTY stdout must
    read as interactive.
    """
    # Non-TTY stdin + TTY stdout → non-interactive (stdout must not rescue it).
    _pin_interactivity_inputs(monkeypatch, stdin_tty=False, stdout_tty=True)
    assert is_noninteractive_environment() is True

    # TTY stdin + non-TTY stdout (the `mytool | tee` shape) → interactive.
    _pin_interactivity_inputs(monkeypatch, stdin_tty=True, stdout_tty=False)
    assert is_noninteractive_environment() is False


def test_headless_probe_ci_beats_a_full_tty(monkeypatch: pytest.MonkeyPatch) -> None:
    """``CI``/``GITHUB_ACTIONS`` force non-interactive even on a real terminal.

    A CI runner can present a TTY on both streams; the env var is the
    authoritative signal that nobody is there to click.
    """
    _pin_interactivity_inputs(monkeypatch, stdin_tty=True, stdout_tty=True)
    assert is_noninteractive_environment() is False  # control

    monkeypatch.setenv("CI", "1")
    assert is_noninteractive_environment() is True

    monkeypatch.delenv("CI", raising=False)
    monkeypatch.setenv("GITHUB_ACTIONS", "true")
    assert is_noninteractive_environment() is True


def test_headless_probe_linux_requires_a_display(monkeypatch: pytest.MonkeyPatch) -> None:
    """On Linux, no ``DISPLAY``/``WAYLAND_DISPLAY`` → non-interactive even on a TTY.

    There is no X11/Wayland session for ``webbrowser.open`` to target, so the
    browser redirect cannot work regardless of terminal state.
    """
    if not sys.platform.startswith("linux"):
        pytest.skip("DISPLAY gating is Linux-only by design")
    _pin_interactivity_inputs(monkeypatch, stdin_tty=True, stdout_tty=True)
    assert is_noninteractive_environment() is False  # control: DISPLAY pinned to :0

    monkeypatch.delenv("DISPLAY", raising=False)
    monkeypatch.delenv("WAYLAND_DISPLAY", raising=False)
    assert is_noninteractive_environment() is True

    monkeypatch.setenv("WAYLAND_DISPLAY", "wayland-0")
    assert is_noninteractive_environment() is False


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
