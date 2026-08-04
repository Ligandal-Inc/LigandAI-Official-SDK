# Copyright © 2026 Ligandal, Inc. All rights reserved.
"""Server-advertised SDK version advisory.  [bd-LIGANDAI_ALPHA_V2-x4vt8.3.2]

The platform tells every client, on every response, whether it is still
``current``. An SDK build old enough to drop request parameters is how a
customer's exclusion became a silent no-op, so the check has to be automatic
(no extra round-trip, no opt-in), advisory-only (never auto-upgrades), and
quiet enough that a polling loop cannot spam.
"""

from __future__ import annotations

import warnings

import pytest
from pytest_httpx import HTTPXMock

from ligandai._http import HTTPTransport
from ligandai.errors import LigandAIDiagnosticWarning, LigandAIVersionWarning
from ligandai.server_compat import (
    COMPATIBILITY_PATH,
    SDK_LATEST_HEADER,
    SDK_MIN_SUPPORTED_HEADER,
    SDK_STATUS_HEADER,
    UPGRADE_COMMAND,
    advisory_from_headers,
    compatibility_diagnostics,
    latest_advisory,
    note_response_headers,
    reset_advisory_state,
    skip_check_enabled,
)

BASE = "http://api.ligandai.test"


@pytest.fixture(autouse=True)
def _clean_advisory_state(monkeypatch: pytest.MonkeyPatch):
    """The latch is process-wide by design — reset it around every test, and
    make sure a developer's own opt-out env var can't mask a failure."""
    monkeypatch.delenv("LIGANDAI_SKIP_VERSION_CHECK", raising=False)
    monkeypatch.delenv("LIGANDAI_SKIP_SERVER_VERSION_CHECK", raising=False)
    reset_advisory_state()
    yield
    reset_advisory_state()


@pytest.fixture
def transport() -> HTTPTransport:
    t = HTTPTransport(api_key="lgai_pro_test", base_url=BASE, max_retries=1)
    yield t
    t.close()


# ---------------------------------------------------------------------------
# advisory_from_headers — pure, no side effects
# ---------------------------------------------------------------------------


def test_absent_headers_are_a_no_op() -> None:
    """An older server sends nothing. Nothing must happen."""
    assert advisory_from_headers({}) is None
    assert advisory_from_headers(None) is None
    assert advisory_from_headers({"content-type": "application/json"}) is None


def test_current_status_says_nothing() -> None:
    assert advisory_from_headers({SDK_STATUS_HEADER: "current"}) is None


def test_outdated_status_yields_sdk_outdated_warning() -> None:
    d = advisory_from_headers(
        {
            SDK_STATUS_HEADER: "outdated",
            SDK_LATEST_HEADER: "0.12.0",
            SDK_MIN_SUPPORTED_HEADER: "0.9.0",
        },
        installed="0.10.0",
    )
    assert d is not None
    assert d.code == "sdk_outdated"
    assert d.severity == "warning"
    assert d.stage == "client"
    assert d.details["installed"] == "0.10.0"
    assert d.details["latest"] == "0.12.0"
    assert d.details["min_supported"] == "0.9.0"
    assert d.details["package"] == "ligandai"
    assert d.details["upgrade_command"] == UPGRADE_COMMAND
    # The actionable part must be in the message the user actually reads.
    assert UPGRADE_COMMAND in d.message
    assert "0.12.0" in d.message


def test_unsupported_status_is_an_error_severity() -> None:
    d = advisory_from_headers(
        {SDK_STATUS_HEADER: "unsupported", SDK_MIN_SUPPORTED_HEADER: "0.9.0"},
        installed="0.4.0",
    )
    assert d is not None
    assert d.code == "sdk_unsupported"
    assert d.severity == "error"
    assert UPGRADE_COMMAND in d.message


def test_unknown_status_is_informational_not_a_warning() -> None:
    d = advisory_from_headers({SDK_STATUS_HEADER: "unknown"}, installed="0.11.0")
    assert d is not None
    assert d.code == "sdk_version_unknown"
    assert d.severity == "info"


def test_unrecognized_status_still_surfaces_and_keeps_the_raw_value() -> None:
    """A status this build doesn't know means the server is newer than we are —
    which is the upgrade case. The raw value is preserved, never dropped."""
    d = advisory_from_headers({SDK_STATUS_HEADER: "quarantined"}, installed="0.11.0")
    assert d is not None
    assert d.code == "sdk_outdated"
    assert d.details["server_status"] == "quarantined"
    assert "quarantined" in d.message


def test_header_lookup_is_case_insensitive_for_plain_dicts() -> None:
    d = advisory_from_headers({"X-LigandAI-SDK-Status": "outdated"}, installed="0.1.0")
    assert d is not None
    assert d.code == "sdk_outdated"


# ---------------------------------------------------------------------------
# note_response_headers — warning channel, latch, opt-out
# ---------------------------------------------------------------------------


def test_note_headers_warns_once_per_process() -> None:
    headers = {SDK_STATUS_HEADER: "outdated", SDK_LATEST_HEADER: "9.9.9"}
    with pytest.warns(LigandAIVersionWarning, match=r"out of date"):
        first = note_response_headers(headers)
    assert first is not None
    assert latest_advisory() is first

    # A polling loop must not re-warn.
    with warnings.catch_warnings():
        warnings.simplefilter("error", LigandAIVersionWarning)
        assert note_response_headers(headers) is None


def test_version_warning_is_catchable_as_a_diagnostic_warning() -> None:
    """One filter silences (or escalates) every platform diagnostic."""
    assert issubclass(LigandAIVersionWarning, LigandAIDiagnosticWarning)
    with pytest.warns(LigandAIDiagnosticWarning):
        note_response_headers({SDK_STATUS_HEADER: "unsupported"})


def test_info_severity_does_not_warn() -> None:
    with warnings.catch_warnings():
        warnings.simplefilter("error", LigandAIVersionWarning)
        d = note_response_headers({SDK_STATUS_HEADER: "unknown"})
    assert d is not None
    assert d.severity == "info"


@pytest.mark.parametrize(
    "var", ["LIGANDAI_SKIP_VERSION_CHECK", "LIGANDAI_SKIP_SERVER_VERSION_CHECK"]
)
def test_opt_out_env_vars(monkeypatch: pytest.MonkeyPatch, var: str) -> None:
    monkeypatch.setenv(var, "1")
    assert skip_check_enabled() is True
    with warnings.catch_warnings():
        warnings.simplefilter("error", LigandAIVersionWarning)
        assert note_response_headers({SDK_STATUS_HEADER: "unsupported"}) is None


def test_note_headers_never_raises_on_garbage() -> None:
    class Exploding:
        def get(self, _name: str) -> str:
            raise RuntimeError("boom")

        def items(self) -> list[tuple[str, str]]:
            raise RuntimeError("boom")

    assert note_response_headers(Exploding()) is None  # type: ignore[arg-type]


# ---------------------------------------------------------------------------
# Wired into the single response funnel — no extra round-trip
# ---------------------------------------------------------------------------


def test_advisory_rides_a_normal_2xx_response(
    httpx_mock: HTTPXMock, transport: HTTPTransport
) -> None:
    httpx_mock.add_response(
        url=f"{BASE}/api/healthz",
        json={"ok": True},
        headers={
            "X-LigandAI-SDK-Status": "outdated",
            "X-LigandAI-SDK-Latest": "1.0.0",
            "X-LigandAI-SDK-Min-Supported": "0.9.0",
        },
    )
    with pytest.warns(LigandAIVersionWarning, match=r"out of date"):
        body = transport.request("GET", "/api/healthz")
    assert body == {"ok": True}
    advisory = latest_advisory()
    assert advisory is not None
    assert advisory.details["latest"] == "1.0.0"


def test_advisory_also_rides_an_error_response(
    httpx_mock: HTTPXMock, transport: HTTPTransport
) -> None:
    """An outdated client is MOST worth telling about when its request just
    failed — so the hook sits before the is_success early return."""
    from ligandai.errors import LigandAIError

    httpx_mock.add_response(
        url=f"{BASE}/api/healthz",
        status_code=400,
        json={"error": "bad request"},
        headers={"X-LigandAI-SDK-Status": "unsupported"},
    )
    with pytest.warns(LigandAIVersionWarning), pytest.raises(LigandAIError):
        transport.request("GET", "/api/healthz")


def test_no_headers_means_no_warning_at_all(
    httpx_mock: HTTPXMock, transport: HTTPTransport
) -> None:
    httpx_mock.add_response(url=f"{BASE}/api/healthz", json={"ok": True})
    with warnings.catch_warnings():
        warnings.simplefilter("error", LigandAIVersionWarning)
        transport.request("GET", "/api/healthz")
    assert latest_advisory() is None


# ---------------------------------------------------------------------------
# Explicit GET /api/sdk/compatibility
# ---------------------------------------------------------------------------


def test_compatibility_path_is_the_documented_route() -> None:
    assert COMPATIBILITY_PATH == "/api/sdk/compatibility"


def test_compatibility_diagnostics_from_header_shaped_body() -> None:
    diags = compatibility_diagnostics(
        {"status": "unsupported", "latest": "1.0.0", "min_supported": "0.9.0"},
        installed="0.2.0",
    )
    assert [d.code for d in diags] == ["sdk_unsupported"]


def test_compatibility_diagnostics_from_canonical_body() -> None:
    diags = compatibility_diagnostics(
        {"diagnostics": [{"code": "sdk_outdated", "message": "upgrade", "severity": "warning"}]}
    )
    assert [d.code for d in diags] == ["sdk_outdated"]


def test_compatibility_diagnostics_empty_for_current_client() -> None:
    assert compatibility_diagnostics({"status": "current"}) == []
    assert compatibility_diagnostics(None) == []


def test_client_check_compatibility_tolerates_a_missing_endpoint(
    httpx_mock: HTTPXMock, monkeypatch: pytest.MonkeyPatch
) -> None:
    from ligandai import LigandAI

    # Constructing a client runs the separate PyPI update nag; keep it off (the
    # explicit endpoint call under test does not consult the env var).
    monkeypatch.setenv("LIGANDAI_SKIP_VERSION_CHECK", "1")
    httpx_mock.add_response(
        url=f"{BASE}{COMPATIBILITY_PATH}", status_code=404, json={"error": "not found"}
    )
    client = LigandAI(api_key="lgai_pro_test", base_url=BASE)
    try:
        assert client.check_compatibility() == []
    finally:
        client.close()


def test_client_check_compatibility_returns_diagnostics(
    httpx_mock: HTTPXMock, monkeypatch: pytest.MonkeyPatch
) -> None:
    from ligandai import LigandAI

    monkeypatch.setenv("LIGANDAI_SKIP_VERSION_CHECK", "1")
    httpx_mock.add_response(
        url=f"{BASE}{COMPATIBILITY_PATH}",
        json={"status": "outdated", "latest": "2.0.0", "min_supported": "1.0.0"},
    )
    client = LigandAI(api_key="lgai_pro_test", base_url=BASE)
    try:
        diags = client.check_compatibility()
    finally:
        client.close()
    assert [d.code for d in diags] == ["sdk_outdated"]
    assert diags[0].details["upgrade_command"] == UPGRADE_COMMAND


def test_compatibility_body_camelcase_is_tolerated() -> None:
    """The endpoint is being built in parallel; a compatibility check that
    silently no-ops on a casing mismatch is worse than useless."""
    diags = compatibility_diagnostics(
        {"sdkStatus": "unsupported", "latestVersion": "1.0.0", "minSupported": "0.9.0"},
        installed="0.2.0",
    )
    assert [d.code for d in diags] == ["sdk_unsupported"]
    assert diags[0].details["latest"] == "1.0.0"
    assert diags[0].details["min_supported"] == "0.9.0"
