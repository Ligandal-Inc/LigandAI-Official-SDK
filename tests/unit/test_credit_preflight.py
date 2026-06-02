# Copyright © 2026 Ligandal, Inc. All rights reserved.
"""SDK hardening: client-side credit pre-flight.

Verifies that ``client.fold(...)`` and ``client.fold_batch(...)`` estimate
the credit cost locally, compare against the server-reported balance, and
raise :class:`LigandAIInsufficientCredits` BEFORE any HTTP submit when the
balance is insufficient.

Superadmin / unlimited accounts skip the pre-flight (the server is the
authoritative gate for those).
"""

from __future__ import annotations

import httpx
import pytest
import respx

from ligandai import LigandAI
from ligandai._hardening import (
    estimate_fold_batch_credits,
    estimate_single_fold_credits,
)
from ligandai.errors import LigandAIInsufficientCredits


class TestCostEstimation:
    """Local cost-estimation helpers mirror the server formula."""

    def test_fold_batch_simple(self) -> None:
        # 10 peptides × 4 trajectories × 100 cr × max(1, 50/50) = 4000
        assert estimate_fold_batch_credits(
            peptide_count=10, trajectories=4, sampling_steps=50,
        ) == 4000

    def test_fold_batch_with_higher_sampling(self) -> None:
        # 10 × 4 × 100 × max(1, 100/50) = 10 × 4 × 100 × 2 = 8000
        assert estimate_fold_batch_credits(
            peptide_count=10, trajectories=4, sampling_steps=100,
        ) == 8000

    def test_fold_batch_below_50_steps_does_not_discount(self) -> None:
        # 10 × 4 × 100 × max(1, 25/50) = 10 × 4 × 100 × 1 = 4000
        # (no discount for fewer steps; floor is 1.0)
        assert estimate_fold_batch_credits(
            peptide_count=10, trajectories=4, sampling_steps=25,
        ) == 4000

    def test_single_fold(self) -> None:
        # 4 × 100 × 1 = 400
        assert estimate_single_fold_credits(
            trajectories=4, sampling_steps=50,
        ) == 400

    def test_single_fold_default_steps(self) -> None:
        # 4 × 100 × 1 = 400 (None → 50)
        assert estimate_single_fold_credits(
            trajectories=4, sampling_steps=None,
        ) == 400


class TestPreflightRejectsInsufficientBalance:
    """Pre-flight policy: the credit check uses the
    CACHED balance on ``client._credits``. Warm the cache via
    ``_ = client.credits`` (or assign directly) before submitting if you
    want the SDK to enforce. Otherwise the server's 402 remains authoritative.
    """

    def test_fold_batch_insufficient_credits(
        self, client: LigandAI, tmp_ligandai_home,
    ) -> None:
        with respx.mock(base_url=client.base_url, assert_all_called=False) as rmock:
            rmock.get("/api/credits").mock(return_value=httpx.Response(
                200, json={"balance": 100, "is_unlimited": False},
            ))
            posted = rmock.post("/api/v1/folding/predict-batch").mock(
                return_value=httpx.Response(200, json={"batch_id": "n/a"}),
            )
            # Warm the cache so the pre-flight has a real balance to check
            _ = client.credits
            # 10 × 4 × 100 = 4000 cr needed; only 100 available
            with pytest.raises(LigandAIInsufficientCredits) as ei:
                client.fold_batch(
                    peptides=["ACDE"] * 10,
                    target_gene="EGFR",
                    diffusion_samples=4,
                    sampling_steps=50,
                )
            assert ei.value.required == 4000
            assert ei.value.available == 100
            assert ei.value.shortfall == 3900
            assert not posted.called, (
                "credit pre-flight must block POST before submission"
            )

    def test_fold_batch_with_sufficient_balance(
        self, client: LigandAI, tmp_ligandai_home,
    ) -> None:
        with respx.mock(base_url=client.base_url, assert_all_called=False) as rmock:
            rmock.get("/api/credits").mock(return_value=httpx.Response(
                200, json={"balance": 10_000, "is_unlimited": False},
            ))
            posted = rmock.post("/api/v1/folding/predict-batch").mock(
                return_value=httpx.Response(200, json={
                    "batch_id": "batch-ok",
                    "jobs": [{"job_id": "sub-0", "peptide_index": 0, "sequence": "ACDE"}],
                    "total_cost_credits": 4000,
                    "peptide_count": 10,
                    "trajectories_per_peptide": 4,
                    "receptor": {"gene": "EGFR"},
                    "sampling_steps": 50,
                }),
            )
            _ = client.credits  # warm cache
            job = client.fold_batch(
                peptides=["ACDE"] * 10,
                target_gene="EGFR",
                diffusion_samples=4,
                sampling_steps=50,
            )
            assert job.batch_id == "batch-ok"
            assert posted.called

    def test_unlimited_balance_skips_preflight(
        self, client: LigandAI, tmp_ligandai_home,
    ) -> None:
        """``is_unlimited=True`` accounts can submit even with stored balance < cost."""
        with respx.mock(base_url=client.base_url, assert_all_called=False) as rmock:
            rmock.get("/api/credits").mock(return_value=httpx.Response(
                200, json={
                    "balance": 0,
                    "credits": 0,
                    "is_unlimited": True,
                },
            ))
            posted = rmock.post("/api/v1/folding/predict-batch").mock(
                return_value=httpx.Response(200, json={
                    "batch_id": "batch-unlimited",
                    "jobs": [{"job_id": "sub-0", "peptide_index": 0, "sequence": "ACDE"}],
                    "total_cost_credits": 99_999_999,
                    "peptide_count": 100,
                    "trajectories_per_peptide": 16,
                    "receptor": {"gene": "EGFR"},
                    "sampling_steps": 400,
                }),
            )
            _ = client.credits  # warm cache (is_unlimited flag will be cached)
            # Big batch — would fail pre-flight if balance check applied.
            job = client.fold_batch(
                peptides=["ACDE"] * 100,
                target_gene="EGFR",
                diffusion_samples=16,
                sampling_steps=400,
            )
            assert job.batch_id == "batch-unlimited"
            assert posted.called

    def test_no_cached_balance_skips_preflight(
        self, client: LigandAI, tmp_ligandai_home,
    ) -> None:
        """Without a warm balance cache, pre-flight is skipped and POST proceeds."""
        with respx.mock(base_url=client.base_url, assert_all_called=False) as rmock:
            posted = rmock.post("/api/v1/folding/predict-batch").mock(
                return_value=httpx.Response(200, json={
                    "batch_id": "batch-skip-preflight",
                    "jobs": [{"job_id": "sub-0", "peptide_index": 0, "sequence": "ACDE"}],
                    "total_cost_credits": 4000,
                    "peptide_count": 10,
                    "trajectories_per_peptide": 4,
                    "receptor": {"gene": "EGFR"},
                    "sampling_steps": 50,
                }),
            )
            # Do NOT warm cache — pre-flight returns None and skips.
            job = client.fold_batch(
                peptides=["ACDE"] * 10,
                target_gene="EGFR",
                diffusion_samples=4,
                sampling_steps=50,
            )
            assert job.batch_id == "batch-skip-preflight"
            assert posted.called


class TestSuperadminBypass:
    def test_superadmin_skips_preflight(self, tmp_ligandai_home) -> None:
        """Superadmin tier (lgai_sa_*) bypasses credit pre-flight regardless of balance.

        Even when the cached balance is below the estimated cost, superadmin
        accounts skip the pre-flight (the tier check returns True before the
        balance lookup happens).
        """
        with respx.mock(base_url="http://api.ligandai.test", assert_all_called=False) as rmock:
            rmock.get("/api/credits").mock(return_value=httpx.Response(
                200, json={"balance": 1, "is_unlimited": False},
            ))
            posted = rmock.post("/api/v1/folding/predict-batch").mock(
                return_value=httpx.Response(200, json={
                    "batch_id": "batch-sa",
                    "jobs": [{"job_id": "sub-0", "peptide_index": 0, "sequence": "ACDE"}],
                    "total_cost_credits": 4000,
                    "peptide_count": 10,
                    "trajectories_per_peptide": 4,
                    "receptor": {"gene": "EGFR"},
                    "sampling_steps": 50,
                }),
            )
            sa_client = LigandAI(
                api_key="lgai_sa_smoke_superadmin_token",
                base_url="http://api.ligandai.test", max_retries=1,
            )
            try:
                assert sa_client.tier == "superadmin"
                _ = sa_client.credits  # warm cache (balance=1, would normally fail)
                job = sa_client.fold_batch(
                    peptides=["ACDE"] * 10,
                    target_gene="EGFR",
                    diffusion_samples=4,
                    sampling_steps=50,
                )
                assert job.batch_id == "batch-sa"
                assert posted.called
            finally:
                sa_client.close()
