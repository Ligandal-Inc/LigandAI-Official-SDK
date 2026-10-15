# Copyright © 2026 Ligandal, Inc. All rights reserved.
"""Hermetic public GEO SDK contracts; no datasets or model jobs are submitted."""

from __future__ import annotations

import inspect
import json
from types import SimpleNamespace
from typing import Any

import pytest
from pydantic import ValidationError

from ligandai._geo import GeoImportTimeout
from ligandai.errors import LigandAIAuthError, LigandAIError
from ligandai.types import GeoInspection

BASE = "http://api.ligandai.test/api/datasets/geo"
ACCESSION = "GSE100001"
JOB = "geo-fixture-001"


def inspection_payload() -> dict[str, Any]:
    file = {
        "name": "matrix.mtx.gz", "url": f"https://ftp.ncbi.nlm.nih.gov/geo/{ACCESSION}/matrix.mtx.gz",
        "format": "10x_mtx_requires_features_and_barcodes", "supported": True,
        "container": False, "sourceAllowed": True, "sampleId": "GSM100001", "bytes": 512,
    }
    return {
        "schema": "transcriptomic-dataset-inspection/1", "accession": ACCESSION,
        "title": "Synthetic muscle aging metadata fixture", "organism": "Homo sapiens",
        "sampleCount": 2,
        "source": {
            "repository": "GEO", "url": f"https://www.ncbi.nlm.nih.gov/geo/query/acc.cgi?acc={ACCESSION}",
            "metadataSha256": "1" * 64, "metadataCompleteness": "partial", "retrievedAt": None,
        },
        "samples": [{
            "id": "GSM100001", "title": "aged donor", "source": "skeletal muscle",
            "characteristics": ["donor: donor-1", "age: 78"],
            "fields": {"donor": ["donor-1"], "age": ["78"]}, "files": [file],
        }],
        "files": [file],
        "metadataCoverage": {
            "samplesResolved": 1, "declaredSamples": 2,
            "fields": {"donor": {"samplesWithValue": 1, "distinctValues": ["donor-1"]}},
        },
        "parser": {
            "status": "inspection_required", "formats": [file["format"]],
            "requiresSelection": False, "expressionUnit": "unknown",
        },
        "warnings": ["Age alone does not establish diagnosed sarcopenia.", "Units require file validation."],
        "downloaded": False, "futureMetadata": {"retained": True},
    }


async def call(client: Any, async_client: Any, asynchronous: bool, method: str, *args: Any, **kwargs: Any) -> Any:
    resource = async_client.discovery if asynchronous else client.discovery
    result = getattr(resource, method)(*args, **kwargs)
    return await result if inspect.isawaitable(result) else result


@pytest.mark.parametrize("asynchronous", [False, True])
async def test_search_preserves_provenance_and_empty_envelope_warnings(
    httpx_mock, client, async_client, asynchronous,
):
    row = {"accession": ACCESSION, "sampleCount": 2, "provenance": {"source": "NCBI"}, "technology": "snRNA"}
    httpx_mock.add_response(url=f"{BASE}/search?query=aged+muscle&limit=3", json={
        "success": True, "query": "aged muscle", "results": [row], "warnings": ["Partial metadata"],
    })
    result = await call(client, async_client, asynchronous, "search_geo", "aged muscle", limit=3)
    assert result[0].model_dump(by_alias=True, exclude_unset=True) == row
    httpx_mock.add_response(url=f"{BASE}/search?query=unknown&limit=10", json={
        "success": True, "results": [], "warnings": ["Upstream metadata unavailable"],
        "provenance": {"source": "NCBI", "partial": True},
    })
    empty = await call(client, async_client, asynchronous, "search_geo_response", "unknown")
    assert empty.results == []
    assert empty.warnings == ["Upstream metadata unavailable"]
    assert empty.model_dump()["provenance"]["partial"] is True
    assert len(httpx_mock.get_requests()) == 2


@pytest.mark.parametrize("asynchronous", [False, True])
async def test_legacy_search_envelope_keeps_both_records_and_sources(httpx_mock, client, async_client, asynchronous):
    curated = {"accession": ACCESSION, "cellTypes": ["MuSC"], "downloaded": False}
    ncbi = {"accession": ACCESSION, "samples": [{"gsm": "GSM100001"}], "provenance": "official"}
    payload = {"success": True, "curatedSuggestions": [curated], "ncbiResults": [ncbi], "autoSelected": None}
    httpx_mock.add_response(url=f"{BASE}/search?query=muscle&limit=10", json=payload)
    result = await call(client, async_client, asynchronous, "search_geo_response", "muscle")
    assert [r.model_dump(exclude_unset=True, by_alias=True) for r in result.results] == [curated, ncbi]
    assert result.model_dump()["curatedSuggestions"] == [curated]


@pytest.mark.parametrize("payload", [None, {}, {"results": None}, {"success": False, "error": "Upstream unavailable"}])
@pytest.mark.parametrize("asynchronous", [False, True])
async def test_invalid_search_never_becomes_empty_success(httpx_mock, client, async_client, asynchronous, payload):
    httpx_mock.add_response(url=f"{BASE}/search?query=muscle&limit=10", content=json.dumps(payload), headers={"Content-Type": "application/json"})
    with pytest.raises((ValidationError, LigandAIError)):
        await call(client, async_client, asynchronous, "search_geo", "muscle")


@pytest.mark.parametrize("asynchronous", [False, True])
async def test_inspection_exact_identity_annotations_and_unknown_units(httpx_mock, client, async_client, asynchronous):
    payload = inspection_payload()
    httpx_mock.add_response(url=f"{BASE}/inspect/{ACCESSION}", json=payload)
    result = await call(client, async_client, asynchronous, "inspect_geo", f" {ACCESSION.lower()} ")
    assert isinstance(result, GeoInspection)
    assert result.downloaded is False
    assert result.parser.status == "inspection_required"
    assert result.parser.expression_unit == "unknown"
    assert result.samples[0].fields == {"donor": ["donor-1"], "age": ["78"]}
    assert "condition" not in result.samples[0].fields
    assert result.source.metadata_sha256 == "1" * 64
    assert result.model_dump(by_alias=True, exclude_unset=True) == payload
    assert [r.method for r in httpx_mock.get_requests()] == ["GET"]


@pytest.mark.parametrize("field,value", [("accession", "GSE999999"), ("downloaded", True), ("schema", "wrong-schema")])
@pytest.mark.parametrize("asynchronous", [False, True])
async def test_inspection_mismatch_fails(httpx_mock, client, async_client, asynchronous, field, value):
    payload = inspection_payload()
    payload[field] = value
    httpx_mock.add_response(url=f"{BASE}/inspect/{ACCESSION}", json=payload)
    with pytest.raises(ValueError):
        await call(client, async_client, asynchronous, "inspect_geo", ACCESSION)


@pytest.mark.parametrize("asynchronous", [False, True])
async def test_import_and_restart_status_preserve_actual_receipts(httpx_mock, client, async_client, asynchronous):
    httpx_mock.add_response(url=f"{BASE}/import", method="POST", json={
        "jobId": JOB, "accession": ACCESSION, "status": "queued", "progress": 0,
    })
    queued = await call(client, async_client, asynchronous, "import_geo", ACCESSION.lower(), force=True)
    assert queued.status == "queued"
    assert queued.dataset_id is None
    assert json.loads(httpx_mock.get_requests()[0].content) == {"accession": ACCESSION, "force": True}
    complete = {
        "jobId": JOB, "accession": ACCESSION, "status": "completed", "datasetId": 42,
        "provenance": {"source": "owner_dataset_row", "expressionUnit": "unknown"},
        "resultSummary": {"geneCount": 100, "verification": "parsed"}, "fromDb": True,
    }
    httpx_mock.add_response(url=f"{BASE}/status/{JOB}?accession={ACCESSION}", json=complete)
    status = await call(client, async_client, asynchronous, "import_status", JOB, accession=ACCESSION)
    assert status.dataset_id == 42
    assert status.model_dump(by_alias=True, exclude_unset=True) == complete


@pytest.mark.parametrize("asynchronous", [False, True])
async def test_import_explicit_selection_is_preserved_without_inferred_units(httpx_mock, client, async_client, asynchronous):
    selection = {"fileNames": ["study.tar"], "parsePlan": {
        "schema": "transcriptomic-parse-plan/1", "inputs": [{
            "id": "selected", "path": "study/matrix.csv", "format": "csv",
            "orientation": "genes_by_observations", "unit": {"name": "unknown"},
            "feature_metadata_columns": ["Length"],
        }],
    }}
    original = json.loads(json.dumps(selection))
    httpx_mock.add_response(url=f"{BASE}/import", method="POST", json={"jobId": JOB, "accession": ACCESSION, "status": "queued"})
    await call(client, async_client, asynchronous, "import_geo", ACCESSION, selection=selection)
    assert json.loads(httpx_mock.get_request().content) == {"accession": ACCESSION, "force": False, "selection": original}
    assert selection == original


@pytest.mark.parametrize("payload", [None, {}, {"jobId": JOB},
    {"jobId": " ", "accession": ACCESSION, "status": "queued"},
    {"jobId": JOB, "accession": ACCESSION, "status": " "},
    {"jobId": JOB, "accession": "GSE999999", "status": "queued"},
])
@pytest.mark.parametrize("asynchronous", [False, True])
async def test_missing_or_wrong_import_receipt_never_fabricates_queued_job(httpx_mock, client, async_client, asynchronous, payload):
    httpx_mock.add_response(url=f"{BASE}/import", method="POST", content=json.dumps(payload), headers={"Content-Type": "application/json"})
    with pytest.raises(ValueError):
        await call(client, async_client, asynchronous, "import_geo", ACCESSION)


@pytest.mark.parametrize("asynchronous", [False, True])
async def test_status_mismatched_job_and_api_auth_errors_propagate(httpx_mock, client, async_client, asynchronous):
    httpx_mock.add_response(url=f"{BASE}/status/{JOB}", json={"jobId": "other-job", "accession": ACCESSION, "status": "completed"})
    with pytest.raises(ValueError, match="jobId"):
        await call(client, async_client, asynchronous, "import_status", JOB)
    httpx_mock.add_response(url=f"{BASE}/status/{JOB}", status_code=401, json={"error": "Authentication required"})
    with pytest.raises(LigandAIAuthError):
        await call(client, async_client, asynchronous, "import_status", JOB)


@pytest.mark.parametrize("method,args,kwargs", [
    ("search_geo", (" ",), {}), ("search_geo", ("muscle",), {"limit": 51}),
    ("search_geo", ("muscle",), {"limit": True}), ("inspect_geo", ("../../private",), {}),
    ("import_geo", ("GSM100001",), {}), ("import_geo", (ACCESSION,), {"force": "yes"}),
    ("import_geo", (ACCESSION,), {"selection": []}),
    ("import_geo", (ACCESSION,), {"selection": {"value": float("nan")}}),
    ("import_geo", (ACCESSION,), {"selection": {"value": object()}}),
    ("import_status", ("../other",), {}), ("import_status", (JOB,), {"accession": "https://example.com"}),
    ("wait_for_import", (JOB,), {"timeout": float("inf")}),
    ("wait_for_import", (JOB,), {"poll_interval": 0}),
])
@pytest.mark.parametrize("asynchronous", [False, True])
async def test_invalid_inputs_make_no_request(httpx_mock, client, async_client, asynchronous, method, args, kwargs):
    with pytest.raises(ValueError):
        await call(client, async_client, asynchronous, method, *args, **kwargs)
    assert httpx_mock.get_requests() == []


@pytest.fixture
def clock(monkeypatch):
    import ligandai.resources.discovery as module

    state = SimpleNamespace(now=0.0, sleeps=[])

    def sleep(delay):
        state.sleeps.append(delay)
        state.now += delay

    async def async_sleep(delay):
        sleep(delay)

    monkeypatch.setattr(module, "time", SimpleNamespace(monotonic=lambda: state.now, sleep=sleep))
    monkeypatch.setattr(module, "asyncio", SimpleNamespace(sleep=async_sleep))
    return state


@pytest.mark.parametrize("terminal", ["completed", "failed", "cancelled", "canceled"])
@pytest.mark.parametrize("asynchronous", [False, True])
async def test_wait_returns_actual_terminal_receipt_without_resubmitting(httpx_mock, client, async_client, asynchronous, terminal, clock):
    url = f"{BASE}/status/{JOB}?accession={ACCESSION}"
    httpx_mock.add_response(url=url, json={"jobId": JOB, "accession": ACCESSION, "status": "processing"})
    httpx_mock.add_response(url=url, json={"jobId": JOB, "accession": ACCESSION, "status": terminal, "error": "actual diagnostic"})
    result = await call(client, async_client, asynchronous, "wait_for_import", JOB, accession=ACCESSION, timeout=5, poll_interval=2)
    assert result.status == terminal
    assert result.error == "actual diagnostic"
    assert clock.sleeps == [2]
    requests = httpx_mock.get_requests()
    assert [r.method for r in requests] == ["GET", "GET"]
    assert requests[0].extensions["timeout"]["read"] == 5
    assert requests[1].extensions["timeout"]["read"] == 3


@pytest.mark.parametrize("asynchronous", [False, True])
async def test_wait_deadline_retains_last_receipt_and_never_starts_late_poll(httpx_mock, client, async_client, asynchronous, clock):
    for status in ["queued", "processing"]:
        httpx_mock.add_response(url=f"{BASE}/status/{JOB}", json={"jobId": JOB, "accession": ACCESSION, "status": status})
    with pytest.raises(GeoImportTimeout) as captured:
        await call(client, async_client, asynchronous, "wait_for_import", JOB, timeout=3, poll_interval=2)
    assert isinstance(captured.value, TimeoutError)
    assert captured.value.last_job.status == "processing"
    assert clock.sleeps == [2, 1]
    assert len(httpx_mock.get_requests()) == 2


@pytest.mark.parametrize("asynchronous", [False, True])
async def test_wait_does_not_start_another_poll_after_slow_response(httpx_mock, client, async_client, asynchronous, clock):
    import httpx

    def slow_response(request):
        clock.now = 10
        return httpx.Response(200, json={"jobId": JOB, "accession": ACCESSION, "status": "processing"})

    httpx_mock.add_callback(slow_response, url=f"{BASE}/status/{JOB}")
    with pytest.raises(GeoImportTimeout):
        await call(client, async_client, asynchronous, "wait_for_import", JOB, timeout=3)
    assert clock.sleeps == []
    assert len(httpx_mock.get_requests()) == 1
