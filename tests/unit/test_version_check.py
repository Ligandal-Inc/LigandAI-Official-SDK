# Copyright © 2026 Ligandal, Inc. All rights reserved.
"""Version reminder behavior."""

from __future__ import annotations

import pytest
from pytest_httpx import HTTPXMock

import ligandai.client as client_module
import ligandai.version_check as version_check
from ligandai import LigandAI
from ligandai.version_check import get_latest_pypi_version, get_update_notice, is_outdated

PYPI_PROJECT = "https://pypi.org/pypi/ligandai/json"
PYPI_101 = "https://pypi.org/pypi/ligandai/1.0.1/json"
PYPI_036 = "https://pypi.org/pypi/ligandai/0.3.6/json"


def _sdk_payload(version: str) -> dict:
    return {
        "info": {
            "version": version,
            "project_urls": {
                "Repository": "https://github.com/ligandal/ligandai-python-sdk",
            },
        },
        "urls": [{"filename": f"ligandai-{version}.tar.gz", "yanked": False}],
    }


def _wrong_payload(version: str) -> dict:
    return {
        "info": {
            "version": version,
            "project_urls": {
                "Repository": "https://github.com/ligandal/ligandai-python",
            },
        },
        "urls": [{"filename": f"ligandai-{version}.tar.gz", "yanked": False}],
    }


def test_version_helpers_identify_outdated_releases() -> None:
    assert is_outdated("0.3.5", "0.3.6")
    assert get_update_notice("0.3.5", "0.3.6") is not None
    assert get_update_notice("0.3.6", "0.3.6") is None


def test_latest_version_uses_current_project_metadata(
    httpx_mock: HTTPXMock,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("LIGANDAI_SKIP_VERSION_CHECK", raising=False)
    httpx_mock.add_response(url=PYPI_PROJECT, json={**_sdk_payload("0.3.6"), "releases": {}})

    assert get_latest_pypi_version() == "0.3.6"


def test_latest_version_ignores_wrong_package_metadata(
    httpx_mock: HTTPXMock,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("LIGANDAI_SKIP_VERSION_CHECK", raising=False)
    httpx_mock.add_response(
        url=PYPI_PROJECT,
        json={
            **_wrong_payload("1.0.1"),
            "releases": {"1.0.1": [{}], "0.3.6": [{}]},
        },
    )
    httpx_mock.add_response(url=PYPI_101, json=_wrong_payload("1.0.1"))
    httpx_mock.add_response(url=PYPI_036, json=_sdk_payload("0.3.6"))

    assert get_latest_pypi_version() == "0.3.6"


def test_latest_version_ignores_yanked_files(
    httpx_mock: HTTPXMock,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("LIGANDAI_SKIP_VERSION_CHECK", raising=False)
    yanked = _sdk_payload("0.3.7")
    yanked["urls"] = [{"filename": "ligandai-0.3.7.tar.gz", "yanked": True}]
    httpx_mock.add_response(
        url=PYPI_PROJECT,
        json={**yanked, "releases": {"0.3.7": [{}], "0.3.6": [{}]}},
    )
    httpx_mock.add_response(url="https://pypi.org/pypi/ligandai/0.3.7/json", json=yanked)
    httpx_mock.add_response(url=PYPI_036, json=_sdk_payload("0.3.6"))

    assert get_latest_pypi_version() == "0.3.6"


def test_client_constructor_emits_update_notice_once(monkeypatch: pytest.MonkeyPatch) -> None:
    calls: list[None] = []

    def fake_emit() -> None:
        calls.append(None)

    monkeypatch.setattr(client_module, "emit_update_notice", fake_emit)

    c = LigandAI(api_key="lgai_pro_x", base_url="http://api.ligandai.test")
    c.close()

    assert calls == [None]


def test_emit_update_notice_warns_once(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(version_check, "_VERSION_NOTICE_CACHE", None)
    monkeypatch.setattr(version_check, "_VERSION_NOTICE_CHECKED", False)
    monkeypatch.setattr(version_check, "_VERSION_NOTICE_EMITTED", False)
    monkeypatch.setattr(version_check, "get_update_notice", lambda: "upgrade now")

    with pytest.warns(UserWarning, match="upgrade now"):
        assert version_check.emit_update_notice() == "upgrade now"
    assert version_check.emit_update_notice() == "upgrade now"
