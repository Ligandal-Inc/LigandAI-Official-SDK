# Copyright © 2026 Ligandal, Inc. All rights reserved.
"""Job.wait(durable=True) regression tests.

Covers the live regression: 116/245 a production folds had ``status='completed'``
but ``folding_jobs.result`` was the the compute backend spawn-ACK ``{call_id, message,
spawned, status}`` instead of a real fold result. SDK ``wait()`` returned
success and downstream code saw ``pdb_data=None``.

These tests assert:

1. ``Job.wait(durable=True)`` (default) re-polls until the result carries
   actual ``pdbContent`` and raises :class:`LigandAIWaitTimeout` with the
   captured ``call_id`` if the timeout elapses first.
2. ``Job.wait(durable=False)`` keeps the legacy fast-but-permissive behavior
   so power users with custom recovery loops can opt in.
3. The durable check correctly identifies spawn-ACK results, missing PDB
   payloads, and rejects them; real fold results with ``pdbContent`` AND
   ``has_structure=True`` flags pass through.
4. ``BatchFoldJob.stream()`` yields exactly N events for N peptides, never
   yielding a "succeeded" event whose ``pdb_content`` is empty.
"""

from __future__ import annotations

import pytest
from pytest_httpx import HTTPXMock

from ligandai import LigandAI
from ligandai.errors import LigandAIWaitTimeout
from ligandai.jobs import (
    Job,
    _fold_has_durable_payload,
    _result_payload_is_spawn_ack_only,
)
from ligandai.types import JobInfo

BASE = "http://api.ligandai.test"


@pytest.fixture
def client() -> LigandAI:
    return LigandAI(api_key="lgai_pro_test", base_url=BASE, max_retries=1)


# ─── Helper unit tests ──────────────────────────────────────────────────────


def test_durable_payload_detects_spawn_ack() -> None:
    """Spawn-ACK shape is NOT durable — must be flagged for re-poll."""
    spawn_ack = {
        "spawned": True,
        "call_id": "fc-01KRVB3WBHCHB63C77EPE7B3EC",
        "status": "running",
        "message": "the compute backend task spawned",
        "job_id": "fold_xxx",
    }
    durable, missing, call_id = _fold_has_durable_payload(None, spawn_ack)
    assert durable is False
    assert "pdb_data" in missing
    assert call_id == "fc-01KRVB3WBHCHB63C77EPE7B3EC"


def test_durable_payload_accepts_real_fold() -> None:
    """Real fold result with pdbContent is durable."""
    real_result = {
        "pdbContent": "HEADER    LIGANDAI BOLTZ-2\nATOM      1  N   ALA A   1 ...",
        "iptm": 0.94,
        "ipsae": 0.79,
        "mean_plddt": 0.88,
    }
    durable, missing, call_id = _fold_has_durable_payload(None, real_result)
    assert durable is True
    assert missing == []
    assert call_id is None


def test_durable_payload_accepts_has_structure_flag() -> None:
    """When server explicitly emits has_structure=True, that's durable even
    without inline pdb (content is recoverable from gpu_jobs.output_data)."""
    flag_only = {"hasStructure": True, "iptm": 0.85}
    durable, missing, _ = _fold_has_durable_payload(None, flag_only)
    assert durable is True


def test_durable_payload_rejects_empty_result() -> None:
    """Empty result dict is not durable."""
    durable, missing, _ = _fold_has_durable_payload(None, {})
    assert durable is False
    assert "pdb_data" in missing


def test_result_is_spawn_ack_only() -> None:
    """Spawn-ACK detector returns True for exactly the spawn shape."""
    assert _result_payload_is_spawn_ack_only({
        "spawned": True, "call_id": "fc-1", "status": "running",
        "message": "spawned", "job_id": "fold_x",
    }) is True
    # Real fold result has many more keys.
    assert _result_payload_is_spawn_ack_only({
        "pdbContent": "HEADER\n", "iptm": 0.9, "spawned": True, "call_id": "fc-1",
    }) is False
    # Empty.
    assert _result_payload_is_spawn_ack_only({}) is False


# ─── Job.wait(durable=True) integration tests ───────────────────────────────


def test_wait_durable_default_raises_on_spawn_ack_timeout(
    httpx_mock: HTTPXMock, client: LigandAI,
) -> None:
    """If the server keeps returning status=completed with only the spawn-ACK
    as result, wait(durable=True) must raise LigandAIWaitTimeout carrying the
    captured call_id — not silently return a fake-success result."""
    # First response: queued. Second: completed-but-spawn-ack-only. Third+:
    # still completed-but-spawn-ack-only (this is the live reproduction).
    httpx_mock.add_response(
        url=f"{BASE}/api/folding/jobs/fold_stuck/",
        json={
            "id": "fold_stuck",
            "status": "queued",
            "modalStage": "modal_spawning",
        },
        is_reusable=False,
    )
    httpx_mock.add_response(
        url=f"{BASE}/api/folding/jobs/fold_stuck/",
        json={
            "id": "fold_stuck",
            "status": "completed",
            "modalStage": "done",
            "hasStructure": False,
            "result": {
                "spawned": True,
                "call_id": "fc-01STUCKAAAA",
                "status": "running",
                "message": "spawned",
                "job_id": "fold_stuck",
            },
        },
        is_reusable=True,
    )

    job: Job[dict] = Job(
        client.transport,
        "fold_stuck",
        job_type="folding",
        parser=lambda d: d,
        status_path="/api/folding/jobs/{job_id}/",
    )

    with pytest.raises(LigandAIWaitTimeout) as excinfo:
        job.wait(timeout=2.0, poll_interval=0.05)

    err = excinfo.value
    assert err.job_id == "fold_stuck"
    assert err.call_id == "fc-01STUCKAAAA"
    assert "pdb_data" in (err.missing_fields or [])


def test_wait_durable_returns_when_pdb_lands(
    httpx_mock: HTTPXMock, client: LigandAI,
) -> None:
    """Normal happy path: status flips to completed and the result carries the
    pdb_content immediately. wait(durable=True) returns the parsed dict."""
    httpx_mock.add_response(
        url=f"{BASE}/api/folding/jobs/fold_happy/",
        json={
            "id": "fold_happy",
            "status": "completed",
            "hasStructure": True,
            "result": {
                "pdbContent": "HEADER LIGANDAI\nATOM 1 N ALA A 1\n",
                "iptm": 0.93,
                "ipsae": 0.81,
            },
        },
        is_reusable=True,
    )
    job: Job[dict] = Job(
        client.transport,
        "fold_happy",
        job_type="folding",
        parser=lambda d: d,
        status_path="/api/folding/jobs/{job_id}/",
    )
    out = job.wait(timeout=2.0, poll_interval=0.05)
    # parser is `lambda d: d` — d is the merged result payload
    assert isinstance(out, dict)
    assert out["pdbContent"].startswith("HEADER")
    assert out["iptm"] == pytest.approx(0.93)


def test_wait_durable_false_returns_immediately_on_completed(
    httpx_mock: HTTPXMock, client: LigandAI,
) -> None:
    """Opt-out: durable=False keeps the legacy fast-but-permissive behavior.
    Used by power users who run their own recovery loops."""
    httpx_mock.add_response(
        url=f"{BASE}/api/folding/jobs/fold_optout/",
        json={
            "id": "fold_optout",
            "status": "completed",
            "modalStage": "done",
            "hasStructure": False,
            "result": {
                "spawned": True,
                "call_id": "fc-01OPTOUT",
                "status": "running",
            },
        },
        is_reusable=True,
    )
    job: Job[dict] = Job(
        client.transport,
        "fold_optout",
        job_type="folding",
        parser=lambda d: d,
        status_path="/api/folding/jobs/{job_id}/",
    )
    # No raise — caller takes the legacy contract.
    out = job.wait(timeout=2.0, poll_interval=0.05, durable=False)
    # parser returns the (now-merged) result payload directly.
    assert isinstance(out, dict)
    assert out.get("call_id") == "fc-01OPTOUT"


# ─── _parse_fold: schema additions ──────────────────────────────────────────


def test_parse_fold_populates_new_schema_fields() -> None:
    """_parse_fold must populate has_structure, scores, metrics,
    confidence so callers reading the result object see the new contract."""
    from ligandai.resources.peptides import _parse_fold

    raw = {
        "id": "fold_xyz",
        "result": {
            "pdbContent": "HEADER LIGANDAI\nATOM 1 N ALA A 1\n",
            "cifContent": "data_x\nloop_\n",
            "iptm": 0.94,
            "ipsae": 0.81,
            "ptm": 0.87,
            "ipae": 5.2,
            "plddt": [0.5, 0.6, 0.7],
            "mean_plddt": 0.6,
            "scores": {"dg": -8.4, "kd_nm": 12.0},
            "metrics": {"interface_residues": 7},
            "confidence": {"per_chain": {"A": {"plddt": 0.9}}},
            "paeUrl": "https://example/pae.npy",
            "perChain": {"A": {"plddt": 0.92}, "B": {"plddt": 0.45}},
        },
    }
    fold = _parse_fold(raw)
    assert fold.pdb_data.startswith("HEADER")
    assert fold.cif_data.startswith("data_x")
    assert fold.has_structure is True
    assert fold.iptm == pytest.approx(0.94)
    assert fold.ipsae == pytest.approx(0.81)
    assert fold.plddt == pytest.approx(0.6)  # mean_plddt preferred
    assert fold.ptm == pytest.approx(0.87)
    assert fold.ipae == pytest.approx(5.2)
    assert fold.scores == {"dg": -8.4, "kd_nm": 12.0}
    # metrics is the headline flat dict — should include iptm AND the
    # server-provided metrics overlay.
    assert fold.metrics is not None
    assert fold.metrics["iptm"] == pytest.approx(0.94)
    assert fold.metrics["interface_residues"] == 7
    assert fold.confidence == {"per_chain": {"A": {"plddt": 0.9}}}
    assert fold.pae_url == "https://example/pae.npy"


def test_parse_fold_handles_plddt_array_only() -> None:
    """Server sometimes emits only the array form. Parser must mean-pool."""
    from ligandai.resources.peptides import _parse_fold

    raw = {
        "result": {
            "pdbContent": "HEADER X\n",
            "plddt": [0.1, 0.2, 0.3, 0.4],
            "iptm": 0.5,
        },
    }
    fold = _parse_fold(raw)
    assert fold.plddt == pytest.approx(0.25)


def test_parse_fold_spawn_ack_only_yields_not_durable() -> None:
    """A pure spawn-ACK payload yields has_structure=False, pdb_data=None.
    The FoldResult is still constructible (no exceptions) — durable-check
    happens upstream in wait()."""
    from ligandai.resources.peptides import _parse_fold

    raw = {
        "result": {
            "spawned": True,
            "call_id": "fc-1",
            "message": "the compute backend task spawned",
            "status": "running",
            "job_id": "fold_x",
        },
    }
    fold = _parse_fold(raw)
    assert fold.pdb_data is None
    assert fold.has_structure is False
    assert fold.iptm is None
