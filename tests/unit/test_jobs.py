# Copyright © 2025 Ligandal, Inc. All rights reserved.
"""Job lifecycle: poll, wait, cancel, stream."""

from __future__ import annotations

import pytest
from pytest_httpx import HTTPXMock

from ligandai import LigandAI
from ligandai.errors import LigandAIJobError, LigandAITimeoutError
from ligandai.jobs import Job

BASE = "http://api.ligandai.test"


@pytest.fixture
def client() -> LigandAI:
    return LigandAI(api_key="lgai_pro_test", base_url=BASE, max_retries=1)


def test_job_initial_state(client: LigandAI) -> None:
    job: Job[dict] = Job(
        client.transport,
        "abc",
        job_type="generation",
        parser=lambda d: d,
        status_path="/api/ptf/parallel/{job_id}/status",
    )
    assert job.id == "abc"
    assert job.type == "generation"
    assert job.status == "queued"
    assert not job.is_terminal


def test_job_refresh_updates_status(httpx_mock: HTTPXMock, client: LigandAI) -> None:
    httpx_mock.add_response(
        url=f"{BASE}/api/ptf/parallel/jobx/status",
        json={"id": "jobx", "status": "running", "progress": 42.0},
    )
    job: Job[dict] = Job(
        client.transport,
        "jobx",
        job_type="generation",
        parser=lambda d: d,
        status_path="/api/ptf/parallel/{job_id}/status",
    )
    job.refresh()
    assert job.status == "running"
    assert job.progress == 42.0


def test_job_wait_returns_result(httpx_mock: HTTPXMock, client: LigandAI) -> None:
    httpx_mock.add_response(
        url=f"{BASE}/api/ptf/parallel/jobx/status",
        json={
            "id": "jobx",
            "status": "complete",
            "progress": 100.0,
            "result": {"peptides": [{"sequence": "AAA"}]},
        },
        is_reusable=True,
    )
    job: Job[dict] = Job(
        client.transport,
        "jobx",
        job_type="generation",
        parser=lambda d: d,
        status_path="/api/ptf/parallel/{job_id}/status",
    )
    result = job.wait(timeout=5, poll_interval=0.01)
    assert result == {"peptides": [{"sequence": "AAA"}]}
    assert job.succeeded


def test_job_failed_raises(httpx_mock: HTTPXMock, client: LigandAI) -> None:
    httpx_mock.add_response(
        url=f"{BASE}/api/ptf/parallel/jobx/status",
        json={
            "id": "jobx",
            "status": "failed",
            "errorMessage": "GPU OOM",
        },
        is_reusable=True,
    )
    job: Job[dict] = Job(
        client.transport,
        "jobx",
        job_type="generation",
        parser=lambda d: d,
        status_path="/api/ptf/parallel/{job_id}/status",
    )
    with pytest.raises(LigandAIJobError) as exc_info:
        job.wait(timeout=5, poll_interval=0.01)
    assert exc_info.value.job_id == "jobx"
    assert exc_info.value.job_status == "failed"
    assert "GPU OOM" in exc_info.value.message


def test_job_timeout(httpx_mock: HTTPXMock, client: LigandAI) -> None:
    """Job that never completes triggers timeout."""
    httpx_mock.add_response(
        url=f"{BASE}/api/ptf/parallel/jobx/status",
        json={"id": "jobx", "status": "running", "progress": 0.0},
        is_reusable=True,
    )
    job: Job[dict] = Job(
        client.transport,
        "jobx",
        job_type="generation",
        parser=lambda d: d,
        status_path="/api/ptf/parallel/{job_id}/status",
    )
    with pytest.raises(LigandAITimeoutError):
        job.wait(timeout=0.05, poll_interval=0.01)


def test_job_cancel(httpx_mock: HTTPXMock, client: LigandAI) -> None:
    httpx_mock.add_response(
        url=f"{BASE}/api/ptf/parallel/jobx/cancel",
        method="POST",
        json={"cancelled": True},
    )
    job: Job[dict] = Job(
        client.transport,
        "jobx",
        job_type="generation",
        parser=lambda d: d,
        status_path="/api/ptf/parallel/{job_id}/status",
        cancel_path="/api/ptf/parallel/{job_id}/cancel",
    )
    assert job.cancel() is True


def test_job_progress_callback(httpx_mock: HTTPXMock, client: LigandAI) -> None:
    httpx_mock.add_response(
        url=f"{BASE}/api/ptf/parallel/jobx/status",
        json={"id": "jobx", "status": "complete", "progress": 100.0, "result": {}},
        is_reusable=True,
    )
    progress_log = []
    job: Job[dict] = Job(
        client.transport,
        "jobx",
        job_type="generation",
        parser=lambda d: d,
        status_path="/api/ptf/parallel/{job_id}/status",
    )
    job.wait(
        timeout=5,
        poll_interval=0.01,
        on_progress=lambda info: progress_log.append(info.status),
    )
    assert progress_log
