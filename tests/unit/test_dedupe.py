# Copyright © 2026 Ligandal, Inc. All rights reserved.
"""SDK hardening: local fold/fold_batch dedupe.

Verifies that identical fold submissions within the 24h window return the
cached :class:`Job` handle instead of re-submitting, and that
``force_resubmit=True`` bypasses the cache.
"""

from __future__ import annotations

import itertools
import pathlib
import tempfile

import httpx
import pytest
import respx

from ligandai import LigandAI
from ligandai._dedupe import (
    SubmittedSet,
    compute_api_key_hash,
    compute_submission_hash,
)


def _batch_response_factory():
    """Return a respx side-effect that mints unique batch_ids per call."""
    counter = itertools.count(1)

    def make_response(request):
        bid = f"batch-{next(counter)}"
        return httpx.Response(
            200,
            json={
                "batch_id": bid,
                "jobs": [
                    {"job_id": f"sub-{bid}-0", "peptide_index": 0, "sequence": "X"},
                ],
                "total_cost_credits": 400,
                "peptide_count": 1,
                "trajectories_per_peptide": 4,
                "receptor": {"gene": "EGFR"},
                "sampling_steps": 50,
            },
        )

    return make_response


class TestSubmittedSetUnit:
    """Direct unit tests of the SubmittedSet primitives."""

    def test_fresh_lookup_miss(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            ss = SubmittedSet(pathlib.Path(td) / "sub.db")
            h = SubmittedSet.compute_hash(
                peptide_seq="ACDE", receptor_seq="MQRSTV",
                gpu="b200_plus", params={"x": 1},
            )
            k = SubmittedSet.hash_api_key("lgai_pro_test")
            assert ss.lookup(h, k) is None

    def test_record_and_lookup(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            ss = SubmittedSet(pathlib.Path(td) / "sub.db")
            h = SubmittedSet.compute_hash(
                peptide_seq="ACDE", receptor_seq="MQRSTV",
                gpu="b200_plus", params={"x": 1},
            )
            k = SubmittedSet.hash_api_key("lgai_pro_test")
            ss.record_submission(h, k, gpu="b200_plus", kind="fold")
            row = ss.lookup(h, k)
            assert row is not None
            assert row["status"] == "submitted"

    def test_failed_is_eligible_for_retry(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            ss = SubmittedSet(pathlib.Path(td) / "sub.db")
            h = SubmittedSet.compute_hash(
                peptide_seq="ACDE", receptor_seq="X",
                gpu="b200_plus", params={},
            )
            k = SubmittedSet.hash_api_key("lgai_pro_test")
            ss.record_submission(h, k, gpu="b200_plus", kind="fold")
            ss.mark_failed(h, k, reason="net")
            assert ss.lookup(h, k) is None
            # Retry works — re-record succeeds.
            ss.record_submission(h, k, gpu="b200_plus", kind="fold")
            assert ss.lookup(h, k)["status"] == "submitted"

    def test_completed_does_not_count_in_flight(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            ss = SubmittedSet(pathlib.Path(td) / "sub.db")
            h = SubmittedSet.compute_hash(
                peptide_seq="ACDE", receptor_seq="X",
                gpu="b200_plus", params={},
            )
            k = SubmittedSet.hash_api_key("lgai_pro_test")
            ss.record_submission(h, k, gpu="b200_plus", kind="fold")
            assert ss.count_in_flight(k) == 1
            ss.mark_completed(h, k, actual_credits=100)
            assert ss.count_in_flight(k) == 0

    def test_hash_is_order_independent_for_peptide_list(self) -> None:
        h1 = compute_submission_hash(
            peptide_seq=["ACDE", "PQRS"], receptor_seq="X",
            gpu="b200_plus", params={"k": 1},
        )
        h2 = compute_submission_hash(
            peptide_seq=["PQRS", "ACDE"], receptor_seq="X",
            gpu="b200_plus", params={"k": 1},
        )
        h3 = compute_submission_hash(
            peptide_seq=["acde", "pqrs", "acde"],  # case + duplicate
            receptor_seq="X", gpu="b200_plus", params={"k": 1},
        )
        assert h1 == h2 == h3

    def test_hash_changes_with_params(self) -> None:
        h1 = compute_submission_hash(
            peptide_seq="ACDE", receptor_seq="X",
            gpu="b200_plus", params={"diffusion_samples": 4},
        )
        h2 = compute_submission_hash(
            peptide_seq="ACDE", receptor_seq="X",
            gpu="b200_plus", params={"diffusion_samples": 8},
        )
        assert h1 != h2

    def test_api_key_hash_is_truncated_sha256(self) -> None:
        h = compute_api_key_hash("lgai_pro_secrettoken")
        assert len(h) == 16
        assert all(c in "0123456789abcdef" for c in h)
        # Empty / None
        assert compute_api_key_hash(None) == ""
        assert compute_api_key_hash("") == ""


class TestDedupeInClient:
    """End-to-end: second identical fold_batch returns cached BatchFoldJob."""

    def test_second_identical_call_uses_cache(
        self, client: LigandAI, tmp_ligandai_home,
    ) -> None:
        with respx.mock(base_url=client.base_url, assert_all_called=False) as rmock:
            rmock.get("/api/credits").mock(return_value=httpx.Response(
                200, json={"balance": 1_000_000, "is_unlimited": False},
            ))
            posted = rmock.post("/api/v1/folding/predict-batch").mock(
                side_effect=_batch_response_factory(),
            )
            j1 = client.fold_batch(
                ["ACDE"], target_gene="EGFR",
                diffusion_samples=4, sampling_steps=50,
            )
            j2 = client.fold_batch(
                ["ACDE"], target_gene="EGFR",
                diffusion_samples=4, sampling_steps=50,
            )
            assert j1.batch_id == j2.batch_id
            assert posted.call_count == 1, (
                f"expected 1 POST (dedupe hit), got {posted.call_count}"
            )

    def test_force_resubmit_bypasses_cache(
        self, client: LigandAI, tmp_ligandai_home,
    ) -> None:
        with respx.mock(base_url=client.base_url, assert_all_called=False) as rmock:
            rmock.get("/api/credits").mock(return_value=httpx.Response(
                200, json={"balance": 1_000_000, "is_unlimited": False},
            ))
            posted = rmock.post("/api/v1/folding/predict-batch").mock(
                side_effect=_batch_response_factory(),
            )
            client.fold_batch(
                ["ACDE"], target_gene="EGFR",
                diffusion_samples=4, sampling_steps=50,
            )
            client.fold_batch(
                ["ACDE"], target_gene="EGFR",
                diffusion_samples=4, sampling_steps=50,
                force_resubmit=True,
            )
            assert posted.call_count == 2

    def test_different_gene_does_not_dedupe(
        self, client: LigandAI, tmp_ligandai_home,
    ) -> None:
        with respx.mock(base_url=client.base_url, assert_all_called=False) as rmock:
            rmock.get("/api/credits").mock(return_value=httpx.Response(
                200, json={"balance": 1_000_000, "is_unlimited": False},
            ))
            posted = rmock.post("/api/v1/folding/predict-batch").mock(
                side_effect=_batch_response_factory(),
            )
            client.fold_batch(["ACDE"], target_gene="EGFR")
            client.fold_batch(["ACDE"], target_gene="HER2")
            assert posted.call_count == 2

    def test_reordered_peptide_list_dedupes(
        self, client: LigandAI, tmp_ligandai_home,
    ) -> None:
        with respx.mock(base_url=client.base_url, assert_all_called=False) as rmock:
            rmock.get("/api/credits").mock(return_value=httpx.Response(
                200, json={"balance": 1_000_000, "is_unlimited": False},
            ))
            posted = rmock.post("/api/v1/folding/predict-batch").mock(
                side_effect=_batch_response_factory(),
            )
            client.fold_batch(["ACDE", "PQRS"], target_gene="EGFR")
            client.fold_batch(["PQRS", "ACDE"], target_gene="EGFR")
            assert posted.call_count == 1, (
                "peptide-list reorder should NOT change submission identity"
            )

    def test_failed_submission_does_not_block_retry(
        self, client: LigandAI, tmp_ligandai_home,
    ) -> None:
        with respx.mock(base_url=client.base_url, assert_all_called=False) as rmock:
            rmock.get("/api/credits").mock(return_value=httpx.Response(
                200, json={"balance": 1_000_000, "is_unlimited": False},
            ))
            # First call succeeds and records.
            posted = rmock.post("/api/v1/folding/predict-batch").mock(
                side_effect=_batch_response_factory(),
            )
            client.fold_batch(["ACDE"], target_gene="EGFR")
            assert posted.call_count == 1
            # Mark it failed manually (simulates an upstream rollback).
            from ligandai._hardening import (
                build_fold_params_for_hash,
                receptor_seq_for_hash,
            )
            rec = receptor_seq_for_hash(
                target_gene="EGFR", receptor_sequence=None, receptor_pdb=None,
            )
            sub_hash = compute_submission_hash(
                peptide_seq=["ACDE"], receptor_seq=rec, gpu="b200_plus",
                params=build_fold_params_for_hash(
                    target_gene="EGFR",
                    diffusion_samples=1, sampling_steps=50,
                    recycling_steps=None, step_scale=None,
                    msa_enabled=None, glycosylation=None, template_mode=False,
                    # use_potentials defaults True on fold_batch (bd-j2kc5), so the
                    # recorded submission hash carries it — mirror that here.
                    extra={"kind": "fold_batch", "receptor_name": None, "use_potentials": True},
                ),
            )
            client.submitted_set.mark_failed(
                sub_hash, client.api_key_hash, reason="simulated",
            )
            # Retry should re-POST.
            client.fold_batch(["ACDE"], target_gene="EGFR")
            assert posted.call_count == 2
