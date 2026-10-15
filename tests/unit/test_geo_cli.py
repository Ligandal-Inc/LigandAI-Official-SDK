# Copyright © 2026 Ligandal, Inc. All rights reserved.
"""CLI exit status, provenance and exact request fixtures; no live network."""

from __future__ import annotations

import json
from types import SimpleNamespace

import pytest

from ligandai.cli import main

BASE = "http://api.ligandai.test/api/datasets/geo"
ACCESSION = "GSE100001"
JOB = "geo-fixture-001"


@pytest.fixture
def cli_client(monkeypatch, client):
    monkeypatch.setattr("ligandai.cli._make_client", lambda args: client)
    return client


@pytest.mark.parametrize("args", [[], ["geo"], ["geo", "inspect", "../bad"],
    ["geo", "search", "muscle", "--limit", "0"], ["geo", "wait", JOB, "--timeout", "nan"]])
def test_incomplete_or_invalid_discovery_never_opens_client(monkeypatch, capsys, args):
    def no_client(_args):
        raise AssertionError("Invalid arguments must not create a client")

    monkeypatch.setattr("ligandai.cli._make_client", no_client)
    assert main(["discovery", *args]) == 2
    assert capsys.readouterr().err


def test_cli_search_preserves_full_envelope(httpx_mock, cli_client, capsys):
    payload = {"success": True, "results": [], "warnings": ["Metadata unavailable"], "provenance": {"source": "NCBI"}}
    httpx_mock.add_response(url=f"{BASE}/search?query=muscle&limit=2", json=payload)
    assert main(["discovery", "geo", "search", "muscle", "--limit", "2", "--json"]) == 0
    assert json.loads(capsys.readouterr().out) == payload


def test_cli_inspect_is_read_only_and_retains_warnings(httpx_mock, cli_client, capsys):
    from tests.unit.test_geo_discovery import inspection_payload

    payload = inspection_payload()
    httpx_mock.add_response(url=f"{BASE}/inspect/{ACCESSION}", json=payload)
    assert main(["discovery", "geo", "inspect", ACCESSION]) == 0
    assert json.loads(capsys.readouterr().out) == payload
    assert [r.method for r in httpx_mock.get_requests()] == ["GET"]


def test_cli_import_emits_actual_queued_receipt_not_completion(httpx_mock, cli_client, capsys):
    payload = {"jobId": JOB, "accession": ACCESSION, "status": "queued"}
    httpx_mock.add_response(url=f"{BASE}/import", method="POST", json=payload)
    assert main(["discovery", "geo", "import", ACCESSION, "--force"]) == 0
    assert json.loads(capsys.readouterr().out) == payload
    assert json.loads(httpx_mock.get_request().content) == {"accession": ACCESSION, "force": True}


def test_cli_import_reads_exact_selection_json(httpx_mock, cli_client, capsys, tmp_path):
    selection = {"fileNames": ["study.tar"], "parsePlan": {"schema": "transcriptomic-parse-plan/1", "inputs": [{"id": "m", "path": "study/matrix.csv", "format": "csv", "unit": {"name": "unknown"}}]}}
    filename = tmp_path / "selection.json"
    filename.write_text(json.dumps(selection))
    httpx_mock.add_response(url=f"{BASE}/import", method="POST", json={"jobId": JOB, "accession": ACCESSION, "status": "queued"})
    assert main(["discovery", "geo", "import", ACCESSION, "--selection", str(filename)]) == 0
    assert json.loads(httpx_mock.get_request().content)["selection"] == selection
    assert str(filename) not in httpx_mock.get_request().content.decode()


@pytest.mark.parametrize("content", ["[]", "null", "not json", '{"x":NaN}', " " * (1024 * 1024 + 1)])
def test_cli_invalid_selection_never_opens_client(monkeypatch, capsys, tmp_path, content):
    filename = tmp_path / "selection.json"
    filename.write_text(content)
    monkeypatch.setattr("ligandai.cli._make_client", lambda _args: pytest.fail("Invalid selection opened client"))
    assert main(["discovery", "geo", "import", ACCESSION, "--selection", str(filename)]) == 2
    assert capsys.readouterr().err


def test_cli_missing_selection_file_never_opens_client(monkeypatch, capsys, tmp_path):
    monkeypatch.setattr("ligandai.cli._make_client", lambda _args: pytest.fail("Missing selection opened client"))
    assert main(["discovery", "geo", "import", ACCESSION, "--selection", str(tmp_path / "missing.json")]) == 2
    assert capsys.readouterr().err


def test_cli_status_uses_owner_scoped_accession_query(httpx_mock, cli_client, capsys):
    payload = {"jobId": JOB, "accession": ACCESSION, "status": "completed", "datasetId": 42, "provenance": {"source": "database"}}
    httpx_mock.add_response(url=f"{BASE}/status/{JOB}?accession={ACCESSION}", json=payload)
    assert main(["discovery", "geo", "status", JOB, "--accession", ACCESSION]) == 0
    assert json.loads(capsys.readouterr().out) == payload


@pytest.mark.parametrize("status,exit_code", [("completed", 0), ("failed", 1), ("cancelled", 1)])
def test_cli_wait_exit_code_reflects_actual_terminal_status(httpx_mock, cli_client, capsys, status, exit_code):
    payload = {"jobId": JOB, "accession": ACCESSION, "status": status, "error": "fixture diagnostic"}
    httpx_mock.add_response(url=f"{BASE}/status/{JOB}", json=payload)
    assert main(["discovery", "geo", "wait", JOB]) == exit_code
    assert json.loads(capsys.readouterr().out) == payload


def test_cli_timeout_reports_last_job_without_false_success(httpx_mock, cli_client, capsys, monkeypatch):
    clock = SimpleNamespace(now=0.0)

    def sleep(delay):
        clock.now += delay

    monkeypatch.setattr("ligandai.resources.discovery.time", SimpleNamespace(monotonic=lambda: clock.now, sleep=sleep))
    payload = {"jobId": JOB, "accession": ACCESSION, "status": "processing"}
    httpx_mock.add_response(url=f"{BASE}/status/{JOB}", json=payload)
    assert main(["discovery", "geo", "wait", JOB, "--timeout", "1"]) == 4
    output = capsys.readouterr()
    assert json.loads(output.out) == {"completed": False, "reason": "polling_timeout", "jobId": JOB, "lastJob": payload}
    assert "deadline" in output.err
    assert len(httpx_mock.get_requests()) == 1


@pytest.mark.parametrize("status,code", [(401, 3), (403, 3), (402, 6), (500, 1)])
def test_cli_preserves_typed_api_failure_exit_codes(httpx_mock, cli_client, capsys, status, code):
    httpx_mock.add_response(url=f"{BASE}/status/{JOB}", status_code=status, json={"error": "fixture failure"})
    assert main(["discovery", "geo", "status", JOB]) == code
    output = capsys.readouterr()
    assert not output.out
    assert "cause:" in output.err


def test_cli_empty_import_response_does_not_report_success(httpx_mock, cli_client, capsys):
    httpx_mock.add_response(url=f"{BASE}/import", method="POST", json={})
    assert main(["discovery", "geo", "import", ACCESSION]) == 1
    assert "ValidationError" in capsys.readouterr().err
