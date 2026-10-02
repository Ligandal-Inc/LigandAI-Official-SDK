# Copyright © 2026 Ligandal, Inc. All rights reserved.
"""A key without a known prefix, ``ligandai legal``, and POST retries on a refused connection.

Found by running PUBLISHED ligandai 0.16.0 against production blue (loopback agent
token) on 2026-10-02. [bd-dre-3imtv.4]

* a key whose prefix this SDK does not map to a tier (a service/agent token, an
  on-prem prefix) was refused CLIENT-SIDE by every tier-gated call
  (``peptides.generate`` raised "requires free tier or higher") although
  ``_validate_api_key_shape`` documents that tier detection fails OPEN there;
* ``ligandai legal status`` / ``accept`` treated ``needsReAcceptance`` -- a DICT
  ``{"tos": false, "eula": false}`` -- as truthy;
* a POST that failed to CONNECT (nothing sent) was never retried, so a generate /
  fold submitted during a server restart died with ``Connection refused``.
"""

from __future__ import annotations

import argparse
from types import SimpleNamespace
from typing import Any

import httpx
import pytest
from pytest_httpx import HTTPXMock

from ligandai import LigandAI
from ligandai import cli as ligandai_cli
from ligandai._http import HTTPTransport
from ligandai.errors import LigandAIConnectionError, LigandAITierError

BASE = "http://api.ligandai.test"

# GET /api/legal/status on blue, 2026-10-02, for an account that has accepted
# everything at the current versions.
LEGAL_ALL_ACCEPTED = {
    "tosAccepted": True,
    "eulaAccepted": True,
    "userAcceptedVersions": {"tos": "1.6", "eula": "1.2"},
    "currentVersions": {"tos": "1.6", "eula": "1.2"},
    "needsReAcceptance": {"tos": False, "eula": False},
    "materialChange": False,
}


def _mk(key: str = "lgai_pro_testkey0123456789ABCDEF") -> LigandAI:
    return LigandAI(api_key=key, base_url=BASE, max_retries=1)


# ----------------------------------------------- unrecognised-prefix key gating


def test_unrecognised_prefix_key_defers_tier_gating_to_the_server() -> None:
    with pytest.warns(Warning):  # the shape warning is expected and unchanged
        c = LigandAI(api_key="agent-token-without-the-lgai-prefix", base_url=BASE)
    assert c.tier is None
    # A key WAS supplied; the tier is unknown, not "below free".
    assert c.feature_allowed("generate_peptides")
    assert c.feature_allowed("transport_vasculome")
    c._require_feature("generate_peptides")  # no raise
    c._require_feature("payload_optimization")  # no raise: the server decides


def test_anonymous_client_is_still_below_every_tier(monkeypatch: pytest.MonkeyPatch) -> None:
    for var in ("LIGANDAI_API_KEY", "LIGANDAL_API_KEY", "LIGANDAI_TEST_API_KEY"):
        monkeypatch.delenv(var, raising=False)
    c = LigandAI(base_url=BASE)
    assert c.tier is None
    assert not c.feature_allowed("generate_peptides")
    with pytest.raises(LigandAITierError):
        c._require_feature("generate_peptides")


def test_recognised_tiers_are_still_gated_client_side() -> None:
    free = LigandAI(api_key="lgai_free_x", base_url=BASE)
    assert not free.feature_allowed("advanced_guidance")
    assert free.feature_allowed("generate_peptides")


def test_generate_reaches_the_wire_for_an_unrecognised_prefix(httpx_mock: HTTPXMock) -> None:
    httpx_mock.add_response(
        url=f"{BASE}/api/ptf/parallel/generate", method="POST",
        json={"sessionId": "gen_1", "status": "queued"},
    )
    with pytest.warns(Warning):
        c = LigandAI(api_key="agent-token-without-the-lgai-prefix", base_url=BASE, max_retries=1)
    job = c.peptides.generate(gene="EGFR", num_peptides=5, auto_fold=False)
    assert job.session_id == "gen_1"


# --------------------------------------------------------- CLI legal commands


@pytest.mark.parametrize(
    "flag,expected",
    [
        ({"tos": False, "eula": False}, False),  # what the server sends when current
        ({"tos": True, "eula": False}, True),
        ({"tos": False, "eula": True}, True),
        ({}, False),
        (False, False),
        (True, True),  # a bare bool from an older server
        (None, False),
    ],
)
def test_needs_reacceptance_reads_the_dict_values(flag: Any, expected: bool) -> None:
    assert ligandai_cli._needs_reacceptance({"needsReAcceptance": flag}) is expected


def _patch_cli_client(monkeypatch: pytest.MonkeyPatch, status: dict[str, Any]) -> list[str]:
    calls: list[str] = []

    def _resolve(*_a: Any, **_k: Any) -> None:  # must not be reached
        calls.append("resolve_interactively")

    legal = SimpleNamespace(status=lambda: status, resolve_interactively=_resolve)
    monkeypatch.setattr(ligandai_cli, "_make_client", lambda _args: SimpleNamespace(legal=legal))
    return calls


def test_legal_status_does_not_claim_a_newer_version_when_all_current(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    _patch_cli_client(monkeypatch, LEGAL_ALL_ACCEPTED)
    assert ligandai_cli.cmd_legal_status(argparse.Namespace(json=False)) == 0
    out = capsys.readouterr().out
    assert "accepted" in out
    assert "newer version" not in out


def test_legal_status_does_flag_a_newer_version(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    status = {**LEGAL_ALL_ACCEPTED, "needsReAcceptance": {"tos": True, "eula": False}}
    _patch_cli_client(monkeypatch, status)
    ligandai_cli.cmd_legal_status(argparse.Namespace(json=False))
    assert "newer version" in capsys.readouterr().out


def test_legal_accept_exits_early_when_nothing_needs_accepting(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    calls = _patch_cli_client(monkeypatch, LEGAL_ALL_ACCEPTED)
    assert ligandai_cli.cmd_legal_accept(argparse.Namespace(no_browser=True, timeout=1)) == 0
    assert "Already accepted" in capsys.readouterr().out
    assert calls == []  # the browser/poll flow was NOT started


# ------------------------------------------------- transport: connect retries


def _fast(t: HTTPTransport) -> HTTPTransport:
    t._wait = lambda _retry_state: 0.0  # type: ignore[assignment]
    return t


def test_post_is_retried_when_the_connection_was_refused(httpx_mock: HTTPXMock) -> None:
    httpx_mock.add_exception(httpx.ConnectError("[Errno 111] Connection refused"))
    httpx_mock.add_exception(httpx.ConnectError("[Errno 111] Connection refused"))
    httpx_mock.add_response(url=f"{BASE}/api/test", method="POST", json={"ok": True})
    t = _fast(HTTPTransport(api_key="lgai_pro_x", base_url=BASE, max_retries=5))
    assert t.request("POST", "/api/test", json={"a": 1}) == {"ok": True}
    assert len(httpx_mock.get_requests()) == 3
    t.close()


def test_post_connect_timeout_is_retried(httpx_mock: HTTPXMock) -> None:
    httpx_mock.add_exception(httpx.ConnectTimeout("handshake timed out"))
    httpx_mock.add_response(url=f"{BASE}/api/test", method="POST", json={"ok": True})
    t = _fast(HTTPTransport(api_key="lgai_pro_x", base_url=BASE, max_retries=3))
    assert t.request("POST", "/api/test") == {"ok": True}
    t.close()


def test_post_refused_forever_raises_connection_error_after_max_attempts(
    httpx_mock: HTTPXMock,
) -> None:
    for _ in range(3):
        httpx_mock.add_exception(httpx.ConnectError("refused"))
    t = _fast(HTTPTransport(api_key="lgai_pro_x", base_url=BASE, max_retries=3))
    with pytest.raises(LigandAIConnectionError):
        t.request("POST", "/api/test")
    assert len(httpx_mock.get_requests()) == 3
    t.close()


def test_post_read_timeout_is_not_retried_the_request_may_have_been_applied(
    httpx_mock: HTTPXMock,
) -> None:
    httpx_mock.add_exception(httpx.ReadTimeout("no response"))
    t = _fast(HTTPTransport(api_key="lgai_pro_x", base_url=BASE, max_retries=5))
    with pytest.raises(LigandAIConnectionError):
        t.request("POST", "/api/test")
    assert len(httpx_mock.get_requests()) == 1  # one send, no duplicate submission
    t.close()


def test_post_5xx_is_still_not_retried_without_an_idempotency_key(httpx_mock: HTTPXMock) -> None:
    httpx_mock.add_response(url=f"{BASE}/api/test", method="POST", status_code=503, json={})
    t = _fast(HTTPTransport(api_key="lgai_pro_x", base_url=BASE, max_retries=5))
    with pytest.raises(Exception):
        t.request("POST", "/api/test")
    assert len(httpx_mock.get_requests()) == 1
    t.close()


async def test_async_post_is_retried_when_the_connection_was_refused(
    httpx_mock: HTTPXMock,
) -> None:
    from ligandai._http import AsyncHTTPTransport

    httpx_mock.add_exception(httpx.ConnectError("refused"))
    httpx_mock.add_response(url=f"{BASE}/api/test", method="POST", json={"ok": True})
    t = AsyncHTTPTransport(api_key="lgai_pro_x", base_url=BASE, max_retries=3)
    t._wait = lambda _retry_state: 0.0  # type: ignore[assignment]
    assert await t.request("POST", "/api/test") == {"ok": True}
    await t.close()
