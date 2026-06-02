# Copyright © 2026 Ligandal, Inc. All rights reserved.
"""SDK hardening: client-side tier concurrency caps.

Verifies the SDK refuses to submit when the local in-flight count reaches
``TIER_GPU_SLOTS[client.tier]`` — even before the network call. Releasing
an in-flight slot (via ``mark_completed``) frees a slot for new submission.
"""

from __future__ import annotations

import itertools

import httpx
import pytest
import respx

from ligandai import LigandAI
from ligandai._constants import TIER_GPU_SLOTS
from ligandai._dedupe import compute_submission_hash
from ligandai._hardening import (
    build_fold_params_for_hash,
    receptor_seq_for_hash,
)
from ligandai.errors import LigandAIConcurrencyLimit


def _batch_response_factory():
    counter = itertools.count(1)

    def make_response(request):
        bid = f"batch-{next(counter)}"
        return httpx.Response(
            200,
            json={
                "batch_id": bid,
                "jobs": [
                    {"job_id": f"sub-{bid}-0", "peptide_index": 0, "sequence": "ACDE"},
                ],
                "total_cost_credits": 100,
                "peptide_count": 1,
                "trajectories_per_peptide": 1,
                "receptor": {"gene": "X"},
                "sampling_steps": 50,
            },
        )

    return make_response


class TestBasicTierCap:
    """basic tier — TIER_GPU_SLOTS['basic'] == 4."""

    def test_cap_blocks_fifth_distinct_submit(self, tmp_ligandai_home) -> None:
        with respx.mock(base_url="http://api.ligandai.test", assert_all_called=False) as rmock:
            rmock.get("/api/credits").mock(return_value=httpx.Response(
                200, json={"balance": 1_000_000, "is_unlimited": False},
            ))
            rmock.post("/api/v1/folding/predict-batch").mock(
                side_effect=_batch_response_factory(),
            )
            c = LigandAI(
                api_key="lgai_basic_smoketier",
                base_url="http://api.ligandai.test", max_retries=1,
            )
            try:
                assert c.tier == "basic"
                cap = TIER_GPU_SLOTS["basic"]
                assert cap == 4

                for gene in ["EGFR", "HER2", "TNF", "IL6R"]:
                    c.fold_batch(
                        peptides=["ACDE"], target_gene=gene,
                        diffusion_samples=1, sampling_steps=50,
                    )

                with pytest.raises(LigandAIConcurrencyLimit) as ei:
                    c.fold_batch(
                        peptides=["ACDE"], target_gene="VEGFA",
                        diffusion_samples=1, sampling_steps=50,
                    )
                assert ei.value.in_flight == 4
                assert ei.value.limit == 4
            finally:
                c.close()

    def test_release_slot_allows_new_submit(self, tmp_ligandai_home) -> None:
        with respx.mock(base_url="http://api.ligandai.test", assert_all_called=False) as rmock:
            rmock.get("/api/credits").mock(return_value=httpx.Response(
                200, json={"balance": 1_000_000, "is_unlimited": False},
            ))
            posted = rmock.post("/api/v1/folding/predict-batch").mock(
                side_effect=_batch_response_factory(),
            )
            c = LigandAI(
                api_key="lgai_basic_release_test",
                base_url="http://api.ligandai.test", max_retries=1,
            )
            try:
                for gene in ["EGFR", "HER2", "TNF", "IL6R"]:
                    c.fold_batch(
                        peptides=["ACDE"], target_gene=gene,
                        diffusion_samples=1, sampling_steps=50,
                    )
                assert posted.call_count == 4

                # Release one slot by completing EGFR.
                rec = receptor_seq_for_hash(
                    target_gene="EGFR", receptor_sequence=None, receptor_pdb=None,
                )
                h = compute_submission_hash(
                    peptide_seq=["ACDE"], receptor_seq=rec, gpu="b200_plus",
                    params=build_fold_params_for_hash(
                        target_gene="EGFR",
                        diffusion_samples=1, sampling_steps=50,
                        recycling_steps=None, step_scale=None,
                        msa_enabled=None, glycosylation=None, template_mode=False,
                        extra={"kind": "fold_batch", "receptor_name": None},
                    ),
                )
                c.submitted_set.mark_completed(h, c.api_key_hash, actual_credits=100)

                # Slot is free — a NEW distinct submit succeeds.
                c.fold_batch(
                    peptides=["ACDE"], target_gene="VEGFA",
                    diffusion_samples=1, sampling_steps=50,
                )
                assert posted.call_count == 5
            finally:
                c.close()


class TestFreeTierCap:
    """free tier — TIER_GPU_SLOTS['free'] == 1."""

    def test_second_distinct_submit_blocked(self, tmp_ligandai_home) -> None:
        with respx.mock(base_url="http://api.ligandai.test", assert_all_called=False) as rmock:
            rmock.get("/api/credits").mock(return_value=httpx.Response(
                200, json={"balance": 1_000_000, "is_unlimited": False},
            ))
            rmock.post("/api/v1/folding/predict-batch").mock(
                side_effect=_batch_response_factory(),
            )
            c = LigandAI(
                api_key="lgai_free_concur_test",
                base_url="http://api.ligandai.test", max_retries=1,
            )
            try:
                assert c.tier == "free"
                assert TIER_GPU_SLOTS["free"] == 1
                c.fold_batch(
                    peptides=["ACDE"], target_gene="EGFR",
                    diffusion_samples=1, sampling_steps=50,
                )
                with pytest.raises(LigandAIConcurrencyLimit) as ei:
                    c.fold_batch(
                        peptides=["ACDE"], target_gene="HER2",
                        diffusion_samples=1, sampling_steps=50,
                    )
                assert ei.value.in_flight == 1
                assert ei.value.limit == 1
            finally:
                c.close()


class TestProTierAndConstants:
    def test_pro_tier_cap_is_25(self) -> None:
        assert TIER_GPU_SLOTS["pro"] == 25

    def test_superadmin_cap_matches_enterprise(self) -> None:
        assert TIER_GPU_SLOTS["superadmin"] == TIER_GPU_SLOTS["enterprise"]
        # superadmin per-account cap == 50.
        assert TIER_GPU_SLOTS["superadmin"] == 50

    def test_in_flight_via_client_property(self, tmp_ligandai_home) -> None:
        with respx.mock(base_url="http://api.ligandai.test", assert_all_called=False) as rmock:
            rmock.get("/api/credits").mock(return_value=httpx.Response(
                200, json={"balance": 1_000_000, "is_unlimited": False},
            ))
            rmock.post("/api/v1/folding/predict-batch").mock(
                side_effect=_batch_response_factory(),
            )
            c = LigandAI(
                api_key="lgai_pro_inflight_test",
                base_url="http://api.ligandai.test", max_retries=1,
            )
            try:
                assert c.max_concurrent_gpu_slots == 25
                # No submits yet → 0 in flight.
                assert c.submitted_set.count_in_flight(c.api_key_hash) == 0
                c.fold_batch(
                    peptides=["ACDE"], target_gene="EGFR",
                    diffusion_samples=1, sampling_steps=50,
                )
                assert c.submitted_set.count_in_flight(c.api_key_hash) == 1
            finally:
                c.close()
