# Copyright © 2026 Ligandal, Inc. All rights reserved.
"""First-run ergonomics fixes (bd-LIGANDAI_ALPHA_V2-9huj4).

Covers the cold-install / first-PD-L1-run friction:
  1. CostEstimate tolerates the live (nested-breakdown) /api/billing/estimate dict.
  2. Parallel-generation jobs can be reattached by id and resumed (peptides.reattach),
     and jobs.get() falls back to the parallel status path for session_parallel_* ids.
  5. list-typed resource methods tolerate bare-list OR wrapped-dict server responses.

(Issue 3 — the socks extra — is packaging-only; issue 4 — credits display — lives in
test_client.py.)
"""

from __future__ import annotations

import re

import pytest
from pytest_httpx import HTTPXMock

from ligandai import LigandAI
from ligandai.errors import LigandAIError, LigandAINotFoundError
from ligandai.jobs import Job
from ligandai.types import CostEstimate, JobInfo

BASE = "http://api.ligandai.test"


@pytest.fixture
def client() -> LigandAI:
    return LigandAI(api_key="lgai_pro_test", base_url=BASE, max_retries=1)


# ── Issue 1: CostEstimate tolerates the richer server dict ────────────────────

# Exact shape emitted by server/sdk-alias-routes.ts GET /api/billing/estimate:
# breakdown carries nested `params` / `rates` objects, not only int totals.
_ESTIMATE_PAYLOAD = {
    "credits": 30000,
    "cost_usd": 300.0,
    "breakdown": {
        "generation": 30000,
        "folding": 0,
        "scoring": 0,
        "params": {
            "num_peptides": 300,
            "auto_fold": True,
            "fold_top_n": None,
            "fold_trajectories": 4,
            "sampling_steps": 50,
        },
        "rates": {
            "credits_per_peptide": 100,
            "credits_per_fold_per_trajectory": 100,
            "fold_step_multiplier": 1,
            "credits_per_peptide_score": 25,
            "free_score_count": 300,
        },
    },
}


def test_cost_estimate_parses_nested_breakdown_model() -> None:
    """The model itself must not raise on nested breakdown objects (the 0.7.x bug)."""
    ce = CostEstimate.model_validate(_ESTIMATE_PAYLOAD)
    assert ce.credits == 30000
    assert ce.cost_usd == 300.0
    assert isinstance(ce.breakdown, dict)
    assert ce.breakdown["params"]["num_peptides"] == 300
    assert ce.breakdown["rates"]["credits_per_peptide"] == 100


def test_estimate_cost_endpoint_does_not_raise(httpx_mock: HTTPXMock, client: LigandAI) -> None:
    httpx_mock.add_response(
        url=re.compile(rf"{re.escape(BASE)}/api/billing/estimate.*"),
        method="GET",
        json=_ESTIMATE_PAYLOAD,
    )
    ce = client.peptides.estimate_cost(num_peptides=300, auto_fold=True, fold_trajectories=4)
    assert isinstance(ce, CostEstimate)
    assert ce.credits == 30000
    assert ce.breakdown["generation"] == 30000


def test_cost_estimate_tolerates_partial_payload() -> None:
    """A restructured / partial payload parses (defaults) instead of raising, and
    preserves the unknown keys (base extra='allow')."""
    ce = CostEstimate.model_validate({"estimatedCredits": 999})
    assert ce.credits == 0
    assert ce.cost_usd == 0.0
    assert ce.model_extra.get("estimatedCredits") == 999


def test_cost_estimate_camelcase_alias_still_works() -> None:
    ce = CostEstimate.model_validate({"credits": 5, "costUsd": 0.05})
    assert ce.cost_usd == 0.05


# ── Issue 2: reattach a parallel-generation job by id ─────────────────────────


def test_reattach_returns_waitable_job_bound_to_parallel_paths(client: LigandAI) -> None:
    job = client.peptides.reattach("session_parallel_123_abc")
    assert isinstance(job, Job)
    assert job.id == "session_parallel_123_abc"
    assert job.type == "generation"
    assert job._status_path == "/api/ptf/parallel/{job_id}/status"
    assert job._cancel_path == "/api/ptf/parallel/{job_id}/cancel"


def test_reattach_empty_id_raises(client: LigandAI) -> None:
    with pytest.raises(LigandAIError):
        client.peptides.reattach("")


def test_reattach_wait_resolves_generation_result(httpx_mock: HTTPXMock, client: LigandAI) -> None:
    sid = "session_parallel_999_xyz"
    httpx_mock.add_response(
        url=f"{BASE}/api/ptf/parallel/{sid}/status",
        json={
            "sessionId": sid,
            "status": "complete",
            "progress": 100.0,
            "result": {"peptides": [{"sequence": "ACDEFGHIK"}, {"sequence": "MNPQRSTVW"}]},
        },
        is_reusable=True,
    )
    result = client.peptides.reattach(sid).wait(timeout=5, poll_interval=0.01)
    assert len(result.peptides) == 2
    assert result.peptides[0].sequence == "ACDEFGHIK"


# ── Issue 2: jobs.get() parallel-gen fallback ─────────────────────────────────


def test_jobs_get_falls_back_to_parallel_status_on_404(httpx_mock: HTTPXMock, client: LigandAI) -> None:
    sid = "session_parallel_42_def"
    httpx_mock.add_response(
        url=f"{BASE}/api/jobs/{sid}",
        method="GET",
        status_code=404,
        json={"error": "Job not found"},
    )
    httpx_mock.add_response(
        url=f"{BASE}/api/ptf/parallel/{sid}/status",
        method="GET",
        json={"sessionId": sid, "status": "running", "totalFolded": 12},
    )
    info = client.jobs.get(sid)
    assert isinstance(info, JobInfo)
    assert info.id == sid  # sessionId mapped onto JobInfo.id
    assert info.status == "running"
    assert info.type == "generation"
    assert (info.result or {}).get("totalFolded") == 12


def test_jobs_get_reraises_404_for_non_parallel_id(httpx_mock: HTTPXMock, client: LigandAI) -> None:
    httpx_mock.add_response(
        url=f"{BASE}/api/jobs/ligandforge-abc",
        method="GET",
        status_code=404,
        json={"error": "Job not found"},
    )
    with pytest.raises(LigandAINotFoundError):
        client.jobs.get("ligandforge-abc")


# ── Issue 5: list methods tolerate bare-list OR wrapped-dict responses ────────


def test_list_isoforms_wrapped_dict(httpx_mock: HTTPXMock, client: LigandAI) -> None:
    httpx_mock.add_response(
        url=f"{BASE}/api/structure/EGFR/isoforms",
        json={"isoforms": [{"id": "P00533-1", "is_canonical": True}]},
    )
    out = client.structures.list_isoforms("EGFR")
    assert isinstance(out, list)
    assert out[0]["id"] == "P00533-1"


def test_list_isoforms_bare_list(httpx_mock: HTTPXMock, client: LigandAI) -> None:
    """Server returns the list directly — must not raise AttributeError."""
    httpx_mock.add_response(
        url=f"{BASE}/api/structure/EGFR/isoforms",
        json=[{"id": "P00533-2"}],
    )
    out = client.structures.list_isoforms("EGFR")
    assert isinstance(out, list)
    assert out[0]["id"] == "P00533-2"


def test_list_species_bare_list(httpx_mock: HTTPXMock, client: LigandAI) -> None:
    httpx_mock.add_response(
        url=f"{BASE}/api/structure/EGFR/species",
        json=[{"taxid": 9606, "species": "human"}],
    )
    out = client.structures.list_species("EGFR")
    assert isinstance(out, list)
    assert out[0]["taxid"] == 9606


def test_list_uaa_palette_wrapped_and_bare(httpx_mock: HTTPXMock, client: LigandAI) -> None:
    # wrapped
    httpx_mock.add_response(
        url=f"{BASE}/api/v1/linker_modifications/uaa_palette",
        json={"palette": [{"uaa": "AzF"}]},
    )
    out = client.linker_modifications.list_uaa_palette()
    assert isinstance(out, list) and out[0]["uaa"] == "AzF"

    # bare list (must not raise)
    httpx_mock.add_response(
        url=f"{BASE}/api/v1/linker_modifications/uaa_palette",
        json=[{"uaa": "pAcF"}],
    )
    out2 = client.linker_modifications.list_uaa_palette()
    assert isinstance(out2, list) and out2[0]["uaa"] == "pAcF"
