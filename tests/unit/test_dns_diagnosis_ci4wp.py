# Copyright © 2026 Ligandal, Inc. All rights reserved.
"""[bd-LIGANDAI_ALPHA_V2-ci4wp] A non-resolving, non-published host must say so.

OBSERVED 2026-08-22: `~/.ligandai/credentials.json` carries
`server_url = https://api.ligandai.com`, which does not resolve (gaierror -5).
The SDK's own `DEFAULT_BASE_URL` is correct and `client.py` documents that
`api.ligandai.com` was never a published host — but nothing validated the value
the client was HANDED, so the failure surfaced as a bare transport error and
sent people looking at the network instead of at the base URL.

⛔ THE FIRST VERSION OF THE GUARD EXEMPTED SUBDOMAINS, and `api.ligandai.com`
IS a subdomain of `ligandai.com` — so it skipped the exact case it was written
for. The hint fired for `localhost` and not for the real bug. That is why the
negative cases below are as load-bearing as the positive one.
"""
from __future__ import annotations

import httpx
import pytest

from ligandai._constants import DEFAULT_BASE_URL
from ligandai._http import _wrap_transport_error

_HINT = "not the published API host"

_DNS = httpx.ConnectError("[Errno -5] No address associated with hostname")
_TIMEOUT = httpx.ConnectTimeout("timed out")


def _msg(exc: httpx.TransportError, url: str) -> str:
    return str(_wrap_transport_error(exc, "get", url))


def test_non_published_host_that_does_not_resolve_is_diagnosed() -> None:
    """The real bug: api.ligandai.com is a SUBDOMAIN and must still be caught."""
    m = _msg(_DNS, "https://api.ligandai.com/api/auth/user")
    assert _HINT in m
    assert DEFAULT_BASE_URL in m, "the message must name the correct default"
    assert "credentials.json" in m, "it must name the known cause"


def test_published_host_dns_failure_is_NOT_blamed_on_the_url() -> None:
    """A real outage on the right host must not be misdiagnosed as a bad URL."""
    assert _HINT not in _msg(_DNS, f"{DEFAULT_BASE_URL}/api/auth/user")


def test_timeout_is_not_a_dns_problem() -> None:
    """A connect timeout says nothing about name resolution."""
    assert _HINT not in _msg(_TIMEOUT, "https://api.ligandai.com/api/auth/user")


def test_local_dev_typo_is_diagnosed() -> None:
    """A dev pointing at a host that does not resolve gets the same help."""
    assert _HINT in _msg(_DNS, "http://localhost-typo:8000/api/x")


@pytest.mark.parametrize(
    "text",
    [
        "Name or service not known",
        "nodename nor servname provided",
        "Temporary failure in name resolution",
        "getaddrinfo failed",
    ],
)
def test_dns_phrasings_are_all_recognised(text: str) -> None:
    """Resolver phrasing differs by platform; all of them are DNS failures."""
    assert _HINT in _msg(httpx.ConnectError(text), "https://api.ligandai.com/x")


def test_the_guard_never_rewrites_the_callers_url() -> None:
    """Diagnose, never redirect.

    Silently sending a request to a different host than the caller named would
    be worse than failing — it would make a misconfiguration invisible.
    """
    m = _msg(_DNS, "https://api.ligandai.com/api/auth/user")
    assert "api.ligandai.com" in m, "the host the caller actually used must appear"
