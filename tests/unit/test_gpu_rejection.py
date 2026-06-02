# Copyright © 2026 Ligandal, Inc. All rights reserved.
"""SDK hardening: GPU type allowlist enforcement.

Verifies the public LigandAI SDK rejects every GPU string that is not
``"b200_plus"`` BEFORE issuing any HTTP request.

Triggered by the 2026-05-17 a production duplicate-submission incident: 130
duplicate fold submissions on identical record_ids burned the compute backend
compute before the server-side concurrency limiter kicked in. The hardening
adds client-side rejection so the SDK never even attempts the call.
"""

from __future__ import annotations

import httpx
import pytest
import respx

from ligandai import AsyncLigandAI, LigandAI
from ligandai._constants import (
    ALLOWED_GPU_TYPES,
    DEFAULT_GPU_TYPE,
    REJECTED_GPU_TYPES,
)
from ligandai.errors import LigandAIInvalidConfig

# Every GPU string that must raise before any HTTP request.
REJECTED_GPUS = (
    "b200_2x", "b200_4x", "b200_8x",
    "b200",
    "a100", "a100_40gb", "a100_80gb",
    "h100", "h100_80gb",
    "l4", "l40", "l40s", "t4",
    "cpu",
)


class TestRejectionAtClientFold:
    """``client.fold(...)`` top-level — every rejected GPU raises pre-flight."""

    @pytest.mark.parametrize("bad_gpu", REJECTED_GPUS)
    def test_rejects_gpu(self, client: LigandAI, bad_gpu: str) -> None:
        with respx.mock(base_url=client.base_url, assert_all_called=False) as rmock:
            posted = rmock.post("/api/folding/predict").mock(
                return_value=httpx.Response(200, json={"jobId": "should-not-reach"}),
            )
            with pytest.raises(LigandAIInvalidConfig) as ei:
                client.fold(
                    target="M" * 32,
                    peptide="ACDEFGHIK",
                    gpu=bad_gpu,
                )
            assert ei.value.field == "gpu"
            assert ei.value.value == bad_gpu
            assert not posted.called, (
                f"GPU rejection must happen before HTTP — but {bad_gpu} reached predict"
            )


class TestRejectionAtClientFoldBatch:
    """``client.fold_batch(...)`` top-level."""

    @pytest.mark.parametrize("bad_gpu", REJECTED_GPUS)
    def test_rejects_gpu(self, client: LigandAI, bad_gpu: str) -> None:
        with respx.mock(base_url=client.base_url, assert_all_called=False) as rmock:
            posted = rmock.post("/api/v1/folding/predict-batch").mock(
                return_value=httpx.Response(200, json={"batch_id": "should-not-reach"}),
            )
            with pytest.raises(LigandAIInvalidConfig) as ei:
                client.fold_batch(
                    peptides=["ACDEFGHIK"],
                    target_gene="EGFR",
                    gpu=bad_gpu,
                )
            assert ei.value.field == "gpu"
            assert ei.value.value == bad_gpu
            assert not posted.called


class TestRejectionAtPeptidesFold:
    """``client.peptides.fold(...)`` resource-level."""

    @pytest.mark.parametrize("bad_gpu", REJECTED_GPUS)
    def test_rejects_gpu(self, client: LigandAI, bad_gpu: str) -> None:
        with respx.mock(base_url=client.base_url, assert_all_called=False) as rmock:
            posted = rmock.post("/api/folding/predict").mock(
                return_value=httpx.Response(200, json={"jobId": "should-not-reach"}),
            )
            with pytest.raises(LigandAIInvalidConfig) as ei:
                client.peptides.fold(
                    sequences=["MAAAAAAAAA" * 4, "ACDE"],
                    gpu=bad_gpu,
                )
            assert ei.value.field == "gpu"
            assert ei.value.value == bad_gpu
            assert not posted.called


class TestRejectionAtPeptidesFoldBatch:
    """``client.peptides.fold_batch(...)`` resource-level."""

    @pytest.mark.parametrize("bad_gpu", REJECTED_GPUS)
    def test_rejects_gpu(self, client: LigandAI, bad_gpu: str) -> None:
        with respx.mock(base_url=client.base_url, assert_all_called=False) as rmock:
            posted = rmock.post("/api/v1/folding/predict-batch").mock(
                return_value=httpx.Response(200, json={"batch_id": "should-not-reach"}),
            )
            with pytest.raises(LigandAIInvalidConfig) as ei:
                client.peptides.fold_batch(
                    peptides=["ACDEFGHIK"],
                    target_gene="EGFR",
                    gpu=bad_gpu,
                )
            assert ei.value.field == "gpu"
            assert ei.value.value == bad_gpu
            assert not posted.called


class TestPositiveB200Plus:
    """The blessed ``b200_plus`` value flows through normally."""

    def test_b200_plus_accepted(
        self, client: LigandAI, tmp_ligandai_home,
    ) -> None:
        with respx.mock(base_url=client.base_url, assert_all_called=False) as rmock:
            rmock.get("/api/credits").mock(
                return_value=httpx.Response(200, json={
                    "balance": 1_000_000, "is_unlimited": False,
                }),
            )
            posted = rmock.post("/api/v1/folding/predict-batch").mock(
                return_value=httpx.Response(200, json={
                    "batch_id": "batch-b200plus",
                    "jobs": [{"job_id": "sub-0", "peptide_index": 0, "sequence": "ACDE"}],
                    "total_cost_credits": 400,
                    "peptide_count": 1,
                    "trajectories_per_peptide": 4,
                    "receptor": {"gene": "EGFR"},
                    "sampling_steps": 50,
                }),
            )
            job = client.fold_batch(
                peptides=["ACDE"],
                target_gene="EGFR",
                gpu="b200_plus",
                diffusion_samples=4,
                sampling_steps=50,
            )
            assert job.batch_id == "batch-b200plus"
            assert posted.called
            # GPU string must NOT be forwarded to the server body — the
            # platform's b200_plus selection is implicit.
            body = posted.calls.last.request.content.decode("utf-8")
            assert "b200_plus" not in body, (
                "SDK must strip gpu kwarg before POSTing — server picks GPU implicitly"
            )

    def test_default_is_b200_plus(
        self, client: LigandAI, tmp_ligandai_home,
    ) -> None:
        """No ``gpu=`` kwarg → defaults to b200_plus, no kwarg leaks to body."""
        with respx.mock(base_url=client.base_url, assert_all_called=False) as rmock:
            rmock.get("/api/credits").mock(
                return_value=httpx.Response(200, json={
                    "balance": 1_000_000, "is_unlimited": False,
                }),
            )
            posted = rmock.post("/api/v1/folding/predict-batch").mock(
                return_value=httpx.Response(200, json={
                    "batch_id": "batch-default",
                    "jobs": [{"job_id": "sub-0", "peptide_index": 0, "sequence": "ACDE"}],
                    "total_cost_credits": 100,
                    "peptide_count": 1,
                    "trajectories_per_peptide": 1,
                    "receptor": {"gene": "EGFR"},
                    "sampling_steps": 50,
                }),
            )
            client.fold_batch(peptides=["ACDE"], target_gene="EGFR")
            assert posted.called

    @pytest.mark.parametrize(
        "good_input,expected", [
            ("b200_plus", "b200_plus"),
            ("B200_PLUS", "b200_plus"),
            ("  b200_plus  ", "b200_plus"),
            (None, "b200_plus"),
            ("", "b200_plus"),
        ],
    )
    def test_validate_gpu_helper(self, good_input, expected) -> None:
        from ligandai._hardening import validate_gpu
        assert validate_gpu(good_input) == expected


class TestConstants:
    """The allowlist/rejectlist constants are well-formed and disjoint."""

    def test_default_is_b200_plus(self) -> None:
        assert DEFAULT_GPU_TYPE == "b200_plus"

    def test_allowed_includes_b200_plus(self) -> None:
        assert "b200_plus" in ALLOWED_GPU_TYPES

    def test_rejected_includes_multi_gpu(self) -> None:
        for v in ("b200_2x", "b200_4x", "b200_8x"):
            assert v in REJECTED_GPU_TYPES

    def test_rejected_includes_bare_b200(self) -> None:
        assert "b200" in REJECTED_GPU_TYPES

    def test_allowed_and_rejected_disjoint(self) -> None:
        assert ALLOWED_GPU_TYPES.isdisjoint(REJECTED_GPU_TYPES)


# ─── Async parity ──────────────────────────────────────────────────────────


@pytest.mark.asyncio
@pytest.mark.parametrize("bad_gpu", ["b200_2x", "b200_4x", "b200_8x", "b200", "h100"])
async def test_async_fold_rejects_gpu(
    async_client: AsyncLigandAI, bad_gpu: str,
) -> None:
    with respx.mock(base_url=async_client.base_url, assert_all_called=False) as rmock:
        posted = rmock.post("/api/folding/predict").mock(
            return_value=httpx.Response(200, json={"jobId": "should-not-reach"}),
        )
        with pytest.raises(LigandAIInvalidConfig) as ei:
            await async_client.peptides.fold(
                sequences=["MAAAAAAAAA" * 4, "ACDE"],
                gpu=bad_gpu,
            )
        assert ei.value.value == bad_gpu
        assert not posted.called


@pytest.mark.asyncio
@pytest.mark.parametrize("bad_gpu", ["b200_2x", "b200_8x", "cpu"])
async def test_async_fold_batch_rejects_gpu(
    async_client: AsyncLigandAI, bad_gpu: str,
) -> None:
    with respx.mock(base_url=async_client.base_url, assert_all_called=False) as rmock:
        posted = rmock.post("/api/v1/folding/predict-batch").mock(
            return_value=httpx.Response(200, json={"batch_id": "should-not-reach"}),
        )
        with pytest.raises(LigandAIInvalidConfig) as ei:
            await async_client.peptides.fold_batch(
                peptides=["ACDE"],
                target_gene="EGFR",
                gpu=bad_gpu,
            )
        assert ei.value.value == bad_gpu
        assert not posted.called
